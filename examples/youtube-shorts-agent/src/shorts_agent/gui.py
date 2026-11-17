"""Desktop app: a small window around the command line tool.

The work runs in a child process (`python -m shorts_agent.cli`), so the window
stays responsive, a crash cannot take it down, and "Abbrechen" can stop
everything including ffmpeg.
"""

from __future__ import annotations

import os
import queue
import shutil
import signal
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path

from .gui_logic import (
    APP_NAME, IS_WINDOWS, LANGS, LAYOUTS, MODES, NO_WINDOW, Settings, build_cli_args, child_python,
    default_out_dir, friendly_error, details_text, key_problem, load_api_key, open_path, parse_line, save_api_key, short_label, upload_text,
)


# --------------------------------------------------------------------------- #
# Window
# --------------------------------------------------------------------------- #
def main() -> None:
    import tkinter as tk
    from tkinter import colorchooser, filedialog, messagebox, ttk

    if IS_WINDOWS:  # crisp text on high-DPI screens
        try:
            import ctypes

            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except (AttributeError, OSError):
            pass

    settings = Settings.load()
    root = tk.Tk()
    root.title(APP_NAME)
    root.minsize(760, 640)
    style = ttk.Style(root)
    style.configure("Title.TLabel", font=("Segoe UI", 16, "bold"))
    style.configure("Big.TButton", font=("Segoe UI", 11, "bold"), padding=(18, 8))

    events: queue.Queue = queue.Queue()
    state: dict = {"proc": None, "cancelled": False, "shorts": [], "summary": None, "error": None, "note": None}

    url_var = tk.StringVar()
    key_var = tk.StringVar(value=load_api_key())
    remember_var = tk.BooleanVar(value=bool(key_var.get()))
    n_var = tk.IntVar(value=settings.highlights)
    mode_var = tk.StringVar(value=settings.mode)
    lang_var = tk.StringVar(value=settings.language)
    layout_var = tk.StringVar(value=settings.layout)
    len_var = tk.IntVar(value=settings.max_len)
    upper_var = tk.BooleanVar(value=settings.uppercase)
    hook_var = tk.BooleanVar(value=settings.hook)
    teaser_var = tk.BooleanVar(value=settings.teaser)
    out_var = tk.StringVar(value=settings.out_dir or str(default_out_dir()))
    status_var = tk.StringVar(value="Bereit. Füge einen YouTube-Link ein und klicke auf „Shorts erstellen“.")
    color = {"value": settings.color}

    frame = ttk.Frame(root, padding=16)
    frame.pack(fill="both", expand=True)
    frame.columnconfigure(1, weight=1)

    ttk.Label(frame, text=APP_NAME, style="Title.TLabel").grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 12))

    ttk.Label(frame, text="YouTube-Link").grid(row=1, column=0, sticky="w")
    url_entry = ttk.Entry(frame, textvariable=url_var)
    url_entry.grid(row=1, column=1, sticky="ew", padx=8)

    def paste() -> None:
        try:
            url_var.set(root.clipboard_get().strip())
        except tk.TclError:
            pass

    ttk.Button(frame, text="Einfügen", command=paste).grid(row=1, column=2, sticky="ew")

    ttk.Label(frame, text="API-Key").grid(row=2, column=0, sticky="w", pady=(8, 0))
    ttk.Entry(frame, textvariable=key_var, show="•").grid(row=2, column=1, sticky="ew", padx=8, pady=(8, 0))
    key_row = ttk.Frame(frame)
    key_row.grid(row=2, column=2, sticky="w", pady=(8, 0))
    ttk.Checkbutton(key_row, text="merken", variable=remember_var).pack(side="left")
    hint = ttk.Label(frame, text="Optional – ohne Key gibt es eine einfache Auswahl.  Key holen ↗",
                     foreground="#2563eb", cursor="hand2")
    hint.grid(row=3, column=1, sticky="w", padx=8)
    hint.bind("<Button-1>", lambda _e: webbrowser.open("https://console.anthropic.com/settings/keys"))

    opts = ttk.LabelFrame(frame, text="Einstellungen", padding=10)
    opts.grid(row=4, column=0, columnspan=3, sticky="ew", pady=12)
    for c in (1, 3):
        opts.columnconfigure(c, weight=1)

    def combo(r: int, c: int, label: str, var: tk.StringVar, values) -> None:
        ttk.Label(opts, text=label).grid(row=r, column=c, sticky="w", padx=(0 if c == 0 else 16, 6), pady=3)
        ttk.Combobox(opts, textvariable=var, values=list(values), state="readonly", width=24).grid(
            row=r, column=c + 1, sticky="ew", pady=3)

    combo(0, 0, "Art", mode_var, MODES)
    combo(0, 2, "Sprache der Texte", lang_var, LANGS)
    combo(1, 0, "Bildausschnitt", layout_var, LAYOUTS)
    ttk.Label(opts, text="Anzahl Highlights").grid(row=1, column=2, sticky="w", padx=(16, 6))
    ttk.Spinbox(opts, from_=0, to=10, textvariable=n_var, width=6).grid(row=1, column=3, sticky="w")
    ttk.Label(opts, text="Max. Länge (Sek.)").grid(row=2, column=0, sticky="w", pady=3)
    ttk.Spinbox(opts, from_=15, to=60, textvariable=len_var, width=6).grid(row=2, column=1, sticky="w")
    color_row = ttk.Frame(opts)
    color_row.grid(row=2, column=2, columnspan=2, sticky="w", padx=(16, 0))
    swatch = tk.Label(color_row, width=3, background=color["value"], relief="solid", borderwidth=1)

    def pick_color() -> None:
        chosen = colorchooser.askcolor(color["value"], title="Farbe des gesprochenen Worts")[1]
        if chosen:
            color["value"] = chosen.upper()
            swatch.configure(background=chosen)

    ttk.Button(color_row, text="Untertitel-Farbe …", command=pick_color).pack(side="left")
    swatch.pack(side="left", padx=6, ipady=2)
    ttk.Checkbutton(opts, text="GROSSBUCHSTABEN", variable=upper_var).grid(row=3, column=0, columnspan=2, sticky="w")
    ttk.Checkbutton(opts, text="Catchphrase am Anfang", variable=hook_var).grid(
        row=3, column=2, columnspan=2, sticky="w", padx=(16, 0))
    ttk.Checkbutton(opts, text="Spannender Teaser vorneweg", variable=teaser_var).grid(
        row=4, column=2, columnspan=2, sticky="w", padx=(16, 0))

    ttk.Label(frame, text="Speichern in").grid(row=5, column=0, sticky="w")
    ttk.Entry(frame, textvariable=out_var).grid(row=5, column=1, sticky="ew", padx=8)

    def choose_dir() -> None:
        chosen = filedialog.askdirectory(initialdir=out_var.get() or str(Path.home()))
        if chosen:
            out_var.set(chosen)

    ttk.Button(frame, text="Ändern …", command=choose_dir).grid(row=5, column=2, sticky="ew")

    run_row = ttk.Frame(frame)
    run_row.grid(row=6, column=0, columnspan=3, sticky="ew", pady=(14, 4))
    run_row.columnconfigure(2, weight=1)
    start_btn = ttk.Button(run_row, text="▶  Shorts erstellen", style="Big.TButton")
    start_btn.grid(row=0, column=0)
    cancel_btn = ttk.Button(run_row, text="Abbrechen", state="disabled")
    cancel_btn.grid(row=0, column=1, padx=8)
    bar = ttk.Progressbar(run_row, maximum=100)
    bar.grid(row=0, column=2, sticky="ew", padx=(8, 0))
    ttk.Label(frame, textvariable=status_var, wraplength=700).grid(row=7, column=0, columnspan=3, sticky="w")

    tabs = ttk.Notebook(frame)
    tabs.grid(row=8, column=0, columnspan=3, sticky="nsew", pady=(10, 0))
    frame.rowconfigure(8, weight=1)

    res_tab = ttk.Frame(tabs, padding=8)
    res_tab.columnconfigure(0, weight=2)
    res_tab.columnconfigure(1, weight=3)
    res_tab.rowconfigure(0, weight=1)
    results = tk.Listbox(res_tab, height=6, width=34, activestyle="none", font=("Segoe UI", 10),
                         exportselection=False)
    results.grid(row=0, column=0, sticky="nsew")
    details = tk.Text(res_tab, wrap="word", height=6, font=("Segoe UI", 10), relief="flat", padx=8, pady=6,
                      state="disabled", background="#f8fafc")
    details.grid(row=0, column=1, sticky="nsew", padx=(8, 0))
    res_buttons = ttk.Frame(res_tab)
    res_buttons.grid(row=1, column=0, columnspan=2, sticky="w", pady=(8, 0))

    def selected_video() -> Path | None:
        sel = results.curselection()
        return Path(state["shorts"][sel[0]]) if sel else None

    def show_details(_e=None) -> None:
        video = selected_video()
        details.configure(state="normal")
        details.delete("1.0", "end")
        details.insert("end", details_text(video) if video else "Wähle links ein Short aus.")
        details.configure(state="disabled")

    def copy_upload() -> None:
        video = selected_video()
        if not video:
            messagebox.showinfo(APP_NAME, "Wähle zuerst links ein Short aus.")
            return
        root.clipboard_clear()
        root.clipboard_append(upload_text(video))
        status_var.set("Titel, Beschreibung und Hashtags kopiert – beim Hochladen einfach einfügen.")

    results.bind("<<ListboxSelect>>", show_details)

    def open_selected(_e=None) -> None:
        sel = results.curselection()
        if sel:
            open_path(Path(state["shorts"][sel[0]]))

    def open_folder() -> None:
        target = Path(state["summary"]).parent if state["summary"] else Path(out_var.get())
        target.mkdir(parents=True, exist_ok=True)
        open_path(target)

    results.bind("<Double-Button-1>", open_selected)
    ttk.Button(res_buttons, text="Video abspielen", command=open_selected).pack(side="left")
    ttk.Button(res_buttons, text="Titel & Text kopieren", command=copy_upload).pack(side="left", padx=(8, 0))
    ttk.Button(res_buttons, text="Ordner öffnen", command=open_folder).pack(side="left", padx=8)
    ttk.Label(res_tab, text="Doppelklick spielt ein Short ab.", foreground="#6b7280").grid(
        row=1, column=1, sticky="e", pady=(8, 0))

    def text_tab() -> tk.Text:
        holder = ttk.Frame(tabs)
        widget = tk.Text(holder, wrap="word", height=10, font=("Segoe UI", 10), relief="flat", padx=8, pady=6)
        scroll = ttk.Scrollbar(holder, command=widget.yview)
        widget.configure(yscrollcommand=scroll.set, state="disabled")
        scroll.pack(side="right", fill="y")
        widget.pack(fill="both", expand=True)
        widget.holder = holder  # type: ignore[attr-defined]
        return widget

    summary_text, log_text = text_tab(), text_tab()
    tabs.add(res_tab, text="Shorts")
    tabs.add(summary_text.holder, text="Zusammenfassung")  # type: ignore[attr-defined]
    tabs.add(log_text.holder, text="Protokoll")  # type: ignore[attr-defined]

    def set_text(widget: tk.Text, text: str, append: bool = False) -> None:
        widget.configure(state="normal")
        if not append:
            widget.delete("1.0", "end")
        widget.insert("end", text)
        if append:
            widget.see("end")
        widget.configure(state="disabled")

    def current_settings() -> Settings:
        def as_int(var, default: int) -> int:
            try:
                return int(var.get())
            except (tk.TclError, ValueError):
                return default

        return Settings(
            highlights=as_int(n_var, 3), mode=mode_var.get(), language=lang_var.get(), layout=layout_var.get(),
            max_len=as_int(len_var, 60), color=color["value"], uppercase=upper_var.get(), hook=hook_var.get(), teaser=teaser_var.get(),
            out_dir=out_var.get(), last_update_check=settings.last_update_check,
        )

    def reader(proc: subprocess.Popen) -> None:
        assert proc.stdout is not None
        for line in proc.stdout:
            events.put(("line", line))
        events.put(("exit", proc.wait()))

    def start() -> None:
        source = url_var.get().strip().strip('"')
        if not source:
            messagebox.showinfo(APP_NAME, "Bitte zuerst einen YouTube-Link einfügen.")
            return
        if not shutil.which("ffmpeg"):
            messagebox.showerror(APP_NAME, friendly_error("ffmpeg not found"))
            return
        s = current_settings()
        s.save()
        settings.__dict__.update(s.__dict__)
        key = key_var.get().strip()
        if problem := key_problem(key):
            go_on = messagebox.askyesno(
                APP_NAME,
                f"{problem}\n\nDu bekommst ihn auf console.anthropic.com → API Keys.\n\n"
                "Ohne Key weitermachen (einfache Auswahl)?",
            )
            if not go_on:
                return
            key = ""
            key_var.set("")
        if IS_WINDOWS:
            save_api_key(key if remember_var.get() else "")
        out_dir = Path(s.out_dir).expanduser()
        out_dir.mkdir(parents=True, exist_ok=True)

        env = dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1")
        env.pop("ANTHROPIC_API_KEY", None)
        if key:
            env["ANTHROPIC_API_KEY"] = key
        cmd = [child_python(), *build_cli_args(source, s, out_dir, bool(key))]
        state.update(cancelled=False, shorts=[], summary=None, error=None, note=None)
        results.delete(0, "end")
        show_details()
        set_text(summary_text, "")
        set_text(log_text, "")
        bar["value"] = 2
        status_var.set("Starte …")
        start_btn.configure(state="disabled")
        cancel_btn.configure(state="normal")
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, text=True,
            encoding="utf-8", errors="replace", env=env, creationflags=NO_WINDOW,
            start_new_session=not IS_WINDOWS,
        )
        state["proc"] = proc
        threading.Thread(target=reader, args=(proc,), daemon=True).start()

    def cancel() -> None:
        proc = state["proc"]
        if not proc or proc.poll() is not None:
            return
        state["cancelled"] = True
        status_var.set("Breche ab …")
        if IS_WINDOWS:  # also stops ffmpeg / yt-dlp children
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True, creationflags=NO_WINDOW)
        else:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except OSError:
                proc.terminate()

    def finish(code: int) -> None:
        state["proc"] = None
        start_btn.configure(state="normal")
        cancel_btn.configure(state="disabled")
        if state["cancelled"]:
            bar["value"] = 0
            status_var.set("Abgebrochen.")
            return
        if code == 0:
            bar["value"] = 100
            n = len(state["shorts"])
            status_var.set(f"Fertig! {n} Short{'s' if n != 1 else ''} erstellt."
                           + (f"  {state['note']}" if state["note"] else ""))
            if state["summary"] and Path(state["summary"]).exists():
                set_text(summary_text, Path(state["summary"]).read_text(encoding="utf-8"))
            tabs.select(0 if n else 1)
            if n:
                results.selection_clear(0, "end")
                results.selection_set(0)
                show_details()
            root.bell()
        else:
            bar["value"] = 0
            status_var.set("Fehlgeschlagen – Details im Reiter „Protokoll“.")
            tabs.select(2)
            messagebox.showerror(APP_NAME, friendly_error(state["error"] or f"Unbekannter Fehler (Code {code})"))

    def pump() -> None:
        try:
            while True:
                kind, payload = events.get_nowait()
                if kind == "exit":
                    finish(payload)
                    continue
                for ev in parse_line(payload):
                    if ev.kind == "log":
                        set_text(log_text, ev.text + "\n", append=True)
                    elif ev.kind == "status":
                        status_var.set(ev.text)
                    elif ev.kind == "short":
                        state["shorts"].append(ev.text)
                        results.insert("end", f"  {len(state['shorts'])}.  {short_label(Path(ev.text))}")
                    elif ev.kind == "summary":
                        state["summary"] = ev.text
                    elif ev.kind == "error":
                        state["error"] = ev.text
                    elif ev.kind == "note":
                        state["note"] = state["note"] or ev.text
                    elif ev.kind == "keyerror":
                        state["note"] = ev.text + " Bitte Key prüfen."
                        status_var.set(ev.text)
                    if ev.progress is not None:
                        bar["value"] = max(bar["value"], ev.progress)
        except queue.Empty:
            pass
        root.after(100, pump)

    def update_yt_dlp() -> None:
        """YouTube changes often; keep the downloader fresh (at most once a day, venv only)."""
        if time.time() - settings.last_update_check < 86400 or sys.prefix == sys.base_prefix:
            return
        done = threading.Event()
        start_btn.configure(state="disabled")
        status_var.set("Suche nach Updates für den YouTube-Downloader …")

        def work() -> None:
            try:
                subprocess.run([child_python(), "-m", "pip", "install", "-q", "--disable-pip-version-check",
                                "--upgrade", "yt-dlp"], capture_output=True, creationflags=NO_WINDOW, timeout=180)
            except (OSError, subprocess.SubprocessError):
                pass
            done.set()

        def wait() -> None:
            if not done.is_set():
                root.after(200, wait)
                return
            settings.last_update_check = time.time()
            current = current_settings()
            current.last_update_check = settings.last_update_check
            current.save()
            start_btn.configure(state="normal")
            status_var.set("Bereit. Füge einen YouTube-Link ein und klicke auf „Shorts erstellen“.")

        threading.Thread(target=work, daemon=True).start()
        wait()

    def on_close() -> None:
        if state["proc"] and state["proc"].poll() is None:
            if not messagebox.askyesno(APP_NAME, "Es läuft noch ein Auftrag. Abbrechen und schließen?"):
                return
            cancel()
        current_settings().save()
        root.destroy()

    start_btn.configure(command=start)
    cancel_btn.configure(command=cancel)
    url_entry.bind("<Return>", lambda _e: start())
    root.protocol("WM_DELETE_WINDOW", on_close)
    url_entry.focus_set()
    update_yt_dlp()
    root.after(400, pump)
    root.mainloop()


if __name__ == "__main__":
    main()
