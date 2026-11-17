"""Free, local AI via Ollama (https://ollama.com) - no API key, runs on this PC.

Talks to Ollama's REST API on localhost with plain urllib (no extra
dependency) and uses its JSON-schema `format` so the answer parses into the
same Pydantic models the Claude path uses.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from typing import Callable, TypeVar

from pathlib import Path

from pydantic import BaseModel, ValidationError

log = logging.getLogger(__name__)

DEFAULT_LOCAL_MODEL = "qwen2.5:7b"  # good German + English, fits 8-16 GB RAM, ~4.7 GB download
MAX_CONTEXT = 32768
T = TypeVar("T", bound=BaseModel)


class LocalUnavailable(RuntimeError):
    """Ollama is not installed/running, or the model is not downloaded."""


def base_url() -> str:
    host = os.environ.get("OLLAMA_HOST", "127.0.0.1:11434").strip().rstrip("/")
    if "://" not in host:
        host = "http://" + host
    host = host.replace("0.0.0.0", "127.0.0.1")
    if host.count(":") == 1:  # scheme only, no port
        host += ":11434"
    return host


def local_model(name: str | None = None) -> str:
    return name or os.environ.get("SHORTS_AGENT_LOCAL_MODEL") or DEFAULT_LOCAL_MODEL


NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def find_executable() -> str | None:
    exe = shutil.which("ollama")
    if exe:
        return exe
    if os.name == "nt":
        candidate = Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Ollama" / "ollama.exe"
        if candidate.exists():
            return str(candidate)
    return None


def install_windows() -> bool:
    """Install Ollama with winget (per user, no admin rights needed).

    Runs in its own visible console window so the download progress can be seen.
    """
    try:
        subprocess.run(
            ["winget", "install", "-e", "--id", "Ollama.Ollama", "--silent",
             "--accept-source-agreements", "--accept-package-agreements"],
            creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0), timeout=3600, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return find_executable() is not None


def ensure_server(timeout: float = 30.0) -> bool:
    """Start `ollama serve` in the background if Ollama is installed but not running."""
    if status() != "no-server":
        return True
    exe = find_executable()
    if not exe:
        return False
    try:
        subprocess.Popen(
            [exe, "serve"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=NO_WINDOW, start_new_session=os.name != "nt",
        )
    except OSError:
        return False
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if status() != "no-server":
            return True
        time.sleep(0.5)
    return False


# Never route localhost through a corporate/system proxy.
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _request(path: str, payload: dict | None = None, timeout: float = 5.0):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        base_url() + path, data=data, headers={"Content-Type": "application/json"},
        method="POST" if data is not None else "GET",
    )
    return _OPENER.open(req, timeout=timeout)


def _matches(installed: str, wanted: str) -> bool:
    if ":" not in wanted:
        wanted += ":latest"
    return installed == wanted


def status(model: str | None = None) -> str:
    """'ready', 'no-model' (Ollama runs, model missing) or 'no-server'."""
    model = local_model(model)
    try:
        with _request("/api/tags", timeout=3) as resp:
            names = [m.get("name", "") for m in json.load(resp).get("models", [])]
    except (OSError, ValueError):
        return "no-server"
    return "ready" if any(_matches(n, model) for n in names) else "no-model"


def pull(model: str | None = None, on_progress: Callable[[str, float | None], None] | None = None) -> None:
    """Download the model through the running Ollama server, reporting progress."""
    model = local_model(model)
    try:
        resp = _request("/api/pull", {"model": model, "stream": True}, timeout=3600)
    except OSError as exc:
        raise LocalUnavailable(f"Ollama is not running ({exc})") from exc
    with resp:
        for raw in resp:
            if not raw.strip():
                continue
            event = json.loads(raw)
            if event.get("error"):
                raise LocalUnavailable(f"Download failed: {event['error']}")
            total, done = event.get("total"), event.get("completed")
            if on_progress:
                on_progress(event.get("status", ""), (done / total) if total and done is not None else None)


def _context_for(text: str) -> int:
    estimate = int(len(text) / 3.2) + 4096  # prompt + room for the answer
    return min(MAX_CONTEXT, max(8192, 1 << (estimate - 1).bit_length()))


def fits(text: str) -> bool:
    return len(text) / 3.2 + 4096 <= MAX_CONTEXT


def call_local(system: str, user: str, output_type: type[T], *, model: str | None = None,
               attempts: int = 2) -> T:
    """One structured request to the local model."""
    model = local_model(model)
    state = status(model)
    if state == "no-server" and ensure_server():
        state = status(model)
    if state == "no-server":
        raise LocalUnavailable("Ollama is not running (install it from ollama.com or via the app)")
    if state == "no-model":
        raise LocalUnavailable(f"The local model {model} is not downloaded yet (ollama pull {model})")
    if not fits(system + user):
        raise LocalUnavailable("This video is too long for the local AI (works up to about 45 minutes)")

    payload = {
        "model": model,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "stream": False,
        "format": output_type.model_json_schema(),
        "options": {"num_ctx": _context_for(system + user), "temperature": 0.4},
        "keep_alive": "10m",
    }
    last: Exception | None = None
    for attempt in range(attempts):
        try:
            with _request("/api/chat", payload, timeout=3600) as resp:
                content = json.load(resp)["message"]["content"]
            return output_type.model_validate_json(content)
        except (ValidationError, ValueError, KeyError) as exc:  # malformed answer: ask again
            last = exc
            log.info("Local AI answer was not usable (attempt %d): %s", attempt + 1, str(exc)[:200])
        except urllib.error.URLError as exc:
            raise LocalUnavailable(f"Lost the connection to Ollama: {exc}") from exc
    raise RuntimeError(f"The local AI did not return a usable answer: {last}")
