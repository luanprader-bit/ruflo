"""Tk-free parts of the desktop app: settings, CLI arguments, output parsing."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

APP_NAME = "YouTube Shorts Agent"
IS_WINDOWS = sys.platform == "win32"
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

MODES = {"Recap + Highlights": "both", "Nur Highlights": "highlights", "Nur Recap": "recap"}
LANGS = {"Wie im Video": None, "Deutsch": "Deutsch", "English": "English"}
LAYOUTS = {"Automatisch": "auto", "Auf Gesicht zuschneiden": "crop", "Ganzes Bild + Unschärfe": "blur"}


# --------------------------------------------------------------------------- #
# Settings (no secrets in here)
# --------------------------------------------------------------------------- #
def _config_dir() -> Path:
    base = os.environ.get("APPDATA") if IS_WINDOWS else os.environ.get("XDG_CONFIG_HOME")
    return Path(base or Path.home() / ".config") / "shorts-agent"


def default_out_dir() -> Path:
    videos = Path.home() / "Videos"
    return (videos if videos.is_dir() else Path.home()) / "YouTube Shorts"


@dataclass
class Settings:
    highlights: int = 3
    mode: str = "Recap + Highlights"
    language: str = "Wie im Video"
    layout: str = "Automatisch"
    max_len: int = 58
    color: str = "#FFE500"
    uppercase: bool = True
    hook: bool = True
    out_dir: str = ""
    last_update_check: float = 0.0

    @classmethod
    def load(cls) -> "Settings":
        try:
            data = json.loads((_config_dir() / "settings.json").read_text(encoding="utf-8"))
            known = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
            return cls(**known)
        except (OSError, ValueError, TypeError):
            return cls()

    def save(self) -> None:
        try:
            _config_dir().mkdir(parents=True, exist_ok=True)
            (_config_dir() / "settings.json").write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")
        except OSError:
            pass


# --------------------------------------------------------------------------- #
# API key: environment, else the Windows user environment in the registry
# --------------------------------------------------------------------------- #
def load_api_key() -> str:
    key = os.environ.get("ANTHROPIC_API_KEY", "")
    if key or not IS_WINDOWS:
        return key
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as reg:
            return str(winreg.QueryValueEx(reg, "ANTHROPIC_API_KEY")[0])
    except OSError:
        return ""


def save_api_key(key: str) -> bool:
    """Persist for this Windows user (same place `setx` writes to)."""
    if not IS_WINDOWS:
        return False
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_SET_VALUE) as reg:
            if key:
                winreg.SetValueEx(reg, "ANTHROPIC_API_KEY", 0, winreg.REG_SZ, key)
            else:
                try:
                    winreg.DeleteValue(reg, "ANTHROPIC_API_KEY")
                except FileNotFoundError:
                    pass
        return True
    except OSError:
        return False


# --------------------------------------------------------------------------- #
# Pure helpers (unit-tested)
# --------------------------------------------------------------------------- #
def child_python() -> str:
    """python.exe next to pythonw.exe, so the child has real stdio."""
    exe = Path(sys.executable)
    if exe.name.lower() == "pythonw.exe" and (exe.parent / "python.exe").exists():
        return str(exe.parent / "python.exe")
    return str(exe)


def build_cli_args(source: str, s: Settings, out_dir: Path, has_key: bool) -> list[str]:
    args = [
        "-m", "shorts_agent.cli", source,
        "-o", str(out_dir),
        "-n", str(max(0, int(s.highlights))),
        "--mode", MODES.get(s.mode, "both"),
        "--layout", LAYOUTS.get(s.layout, "auto"),
        "--max-len", str(max(15, min(60, int(s.max_len)))),
        "--min-len", str(min(20, max(8, int(s.max_len) - 15))),
        "--highlight-color", s.color,
    ]
    if lang := LANGS.get(s.language):
        args += ["--output-lang", lang]
    if not s.uppercase:
        args.append("--no-uppercase")
    if not s.hook:
        args.append("--no-hook")
    if not has_key:
        args += ["--analyzer", "heuristic"]
    return args


@dataclass
class Event:
    kind: str  # "status" | "log" | "short" | "summary" | "error" | "note"
    text: str = ""
    progress: float | None = None


_RENDER = re.compile(r"⑤ Rendering (\d+)/(\d+)\s+(.*?)\s+\(")
_DOWNLOAD = re.compile(r"\[download\]\s+([\d.]+)%")
_WHISPER = re.compile(r"(\d+:\d\d(?::\d\d)?) / (\S+) transcribed")


def parse_line(line: str) -> list[Event]:
    """Translate one line of CLI output into UI events."""
    line = line.rstrip()
    if not line.strip():
        return []
    if m := _DOWNLOAD.search(line):
        pct = float(m.group(1))
        events = [Event("status", f"Lade Video herunter … {pct:.0f} %")]
        return events + ([Event("log", line)] if pct >= 100 else [])
    out = [Event("log", line)]
    if line.startswith("① "):
        out.append(Event("status", "Lade Video-Infos …", 5))
    elif line.startswith("② "):
        out.append(Event("status", "Hole Untertitel …", 10))
    elif "Transcribing with Whisper" in line:
        out.append(Event("status", "Spracherkennung läuft – das kann einige Minuten dauern …", 25))
    elif m := _WHISPER.search(line):
        out.append(Event("status", f"Spracherkennung: {m.group(1)} von {m.group(2)} …"))
    elif line.startswith("③ "):
        out.append(Event("status", "Video wird analysiert …", 45))
    elif line.startswith("④ "):
        out.append(Event("status", "Lade Video herunter …", 55))
    elif m := _RENDER.search(line):
        i, n = int(m.group(1)), int(m.group(2))
        out.append(Event("status", f"Schneide Short {i} von {n}: {m.group(3)}", 60 + 38 * (i - 1) / n))
    elif line.startswith("Short:"):
        out.append(Event("short", line.split(":", 1)[1].strip()))
    elif line.startswith("Summary:"):
        out.append(Event("summary", line.split(":", 1)[1].strip(), 100))
    elif line.startswith("Error:"):
        out.append(Event("error", line.split(":", 1)[1].strip()))
    elif line.startswith("Note: offline heuristic"):
        out.append(Event("note", "Ohne API-Key erstellt – mit Key werden Auswahl und Titel deutlich besser."))
    return out


def friendly_error(message: str) -> str:
    low = message.lower()
    if "ffmpeg" in low and "not found" in low:
        return "ffmpeg fehlt.\n\nIn PowerShell installieren:\n  winget install Gyan.FFmpeg\nDanach die App neu starten."
    if "no captions" in low or "429" in low:
        return ("YouTube hat keine Untertitel geliefert und die Spracherkennung ist nicht installiert.\n\n"
                "Starte start.bat einmal neu – es installiert die Spracherkennung nach.\n\n" + message)
    if "sign in" in low or "bot" in low:
        return "YouTube blockiert den Download gerade. Bitte später erneut versuchen.\n\n" + message
    if "credentials" in low or "api key" in low or "401" in low:
        return "Der API-Key wurde abgelehnt. Bitte prüfen.\n\n" + message
    return message


def short_label(video: Path) -> str:
    """'Title  (42 s, Highlight)' from the .json written next to each Short."""
    try:
        meta = json.loads(video.with_suffix(".json").read_text(encoding="utf-8"))
        kind = "Zusammenfassung" if meta.get("kind") == "recap" else "Highlight"
        return f"{meta['title']}   ({round(meta['duration'])} s, {kind})"
    except (OSError, ValueError, KeyError, TypeError):
        return video.stem


def open_path(path: Path) -> None:
    if IS_WINDOWS:
        os.startfile(str(path))  # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path)])
