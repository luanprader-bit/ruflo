"""YouTube access via yt-dlp: metadata, captions, video download."""

from __future__ import annotations

import logging
import re
import time
from pathlib import Path

from .models import Transcript, VideoInfo
from .transcript import from_json3

log = logging.getLogger(__name__)

_URL = re.compile(r"^https?://", re.I)


def is_url(value: str) -> bool:
    return bool(_URL.match(value))


def _ydl(**opts):
    import yt_dlp

    return yt_dlp.YoutubeDL({
        "quiet": True, "no_warnings": True, "noplaylist": True,
        "retries": 10, "fragment_retries": 10, "extractor_retries": 3, "socket_timeout": 30,
        **opts,
    })


def fetch_info(url: str) -> tuple[VideoInfo, dict]:
    """Return our slim VideoInfo plus yt-dlp's raw info dict (for captions)."""
    with _ydl() as ydl:
        raw = ydl.extract_info(url, download=False)
    info = VideoInfo(
        id=raw["id"],
        title=raw.get("title") or raw["id"],
        channel=raw.get("channel") or raw.get("uploader") or "",
        url=raw.get("webpage_url") or url,
        duration=float(raw.get("duration") or 0),
        description=(raw.get("description") or "")[:2000],
        language=raw.get("language"),
    )
    return info, raw


def _pick_track(tracks: dict, wanted: list[str]) -> tuple[str, str] | None:
    """Find a json3 URL for the first matching language (exact, then prefix)."""
    def json3(lang: str) -> str | None:
        return next((f["url"] for f in tracks.get(lang, []) if f.get("ext") == "json3"), None)

    for lang in wanted:
        for candidate in (lang, f"{lang}-orig"):
            if url := json3(candidate):
                return candidate, url
        base = lang.split("-")[0]
        for key in tracks:
            if key.split("-")[0] == base and (url := json3(key)):
                return key, url
    return None


def fetch_captions(raw: dict, language: str | None = None) -> Transcript | None:
    """Download captions in the spoken language.

    Auto-generated captions win when both exist: they carry per-word timings,
    which the animated subtitles need. Translated tracks are never used.
    """
    wanted = [l for l in (language, raw.get("language"), "en", "de") if l]
    manual = raw.get("subtitles") or {}
    auto = raw.get("automatic_captions") or {}
    if not language and not raw.get("language") and len(manual) == 1:
        wanted.insert(0, next(iter(manual)))

    for tracks, label in ((auto, "auto"), (manual, "manual")):
        picked = _pick_track(tracks, wanted)
        if not picked:
            continue
        lang, url = picked
        try:
            payload = _download_text(url)
            transcript = from_json3(payload, lang.removesuffix("-orig").split("-")[0])
        except RateLimited:
            log.warning(
                "YouTube is rate-limiting caption downloads right now (HTTP 429) - "
                "falling back to speech recognition"
            )
            return None  # every other track hits the same limit
        except Exception as exc:  # network / format hiccup: try the next track
            log.warning("Could not load %s captions (%s): %s", label, lang, exc)
            continue
        if transcript.words:
            log.info("Using %s captions (%s), %d words", label, lang, len(transcript.words))
            return transcript
    return None


class RateLimited(RuntimeError):
    """YouTube answered HTTP 429 Too Many Requests."""


def _is_rate_limit(exc: Exception) -> bool:
    status = getattr(exc, "status", None) or getattr(exc, "code", None)
    return status == 429 or "429" in str(exc)


def _download_text(url: str, waits: tuple[float, ...] = (3.0, 8.0)) -> str:
    """GET a caption URL, retrying briefly when YouTube rate-limits."""
    for attempt in range(len(waits) + 1):
        try:
            with _ydl() as ydl:
                return ydl.urlopen(url).read().decode("utf-8")
        except Exception as exc:
            if not _is_rate_limit(exc):
                raise
            if attempt == len(waits):
                raise RateLimited(str(exc)) from exc
            log.info("YouTube says 'too many requests' - retrying in %.0fs", waits[attempt])
            time.sleep(waits[attempt])
    raise AssertionError("unreachable")


_NETWORK_HINTS = ("getaddrinfo", "errno 11001", "timed out", "connection", "network", "unreachable",
                  "temporarily", "reset by peer", "eof occurred", "remote end closed")


def _describe(exc: BaseException) -> str:
    """yt-dlp errors often read just 'ERROR:'; dig out the underlying cause."""
    texts = [str(exc).strip()]
    inner = getattr(exc, "exc_info", None)
    if inner and len(inner) > 1 and inner[1] is not None:
        texts.append(str(inner[1]).strip())
    if exc.__cause__ is not None:
        texts.append(str(exc.__cause__).strip())
    useful = [t.removeprefix("ERROR:").strip() for t in texts]
    return " / ".join(t for t in useful if t) or type(exc).__name__


def download_video(
    url: str, dest_dir: Path, video_id: str, max_height: int = 1080, waits: tuple[float, ...] = (5.0, 20.0)
) -> Path:
    """Download (or reuse) the video as mp4. Network drops are retried; partial files resume."""
    target = dest_dir / f"{video_id}.mp4"
    if target.exists() and target.stat().st_size > 0:
        return target
    fmt = (
        f"bv*[height<={max_height}][vcodec^=avc1]+ba[ext=m4a]/"
        f"bv*[height<={max_height}]+ba/b[height<={max_height}]/b"
    )
    for attempt in range(len(waits) + 1):
        try:
            with _ydl(
                format=fmt,
                merge_output_format="mp4",
                outtmpl=str(dest_dir / f"{video_id}.%(ext)s"),
                quiet=False,
                noprogress=not log.isEnabledFor(logging.INFO),
            ) as ydl:
                ydl.download([url])
            break
        except Exception as exc:
            reason = _describe(exc)
            network = any(h in reason.lower() for h in _NETWORK_HINTS)
            if network and attempt < len(waits):
                log.warning("Connection lost (%s) - retrying in %.0fs", reason, waits[attempt])
                time.sleep(waits[attempt])
                continue
            if network:
                raise RuntimeError(
                    f"Video download failed: no internet connection or YouTube unreachable ({reason}). "
                    "Check your connection and start again - the download resumes where it stopped."
                ) from exc
            raise RuntimeError(f"Video download failed: {reason}") from exc
    if not target.exists():
        raise RuntimeError(f"yt-dlp finished but {target} is missing")
    return target
