"""Transcript parsing: YouTube json3, SRT/VTT, Whisper. All yield word timings."""

from __future__ import annotations

import json
import re
from pathlib import Path

from .models import Sentence, Transcript, Word

_NOISE = re.compile(r"^(\[[^\]]*\]|\([^)]*\)|♪+|>>+|-+)$")
_SENTENCE_END = re.compile(r"[.!?…][\"'”’)\]]*$")
_TAG = re.compile(r"<[^>]+>")


# --------------------------------------------------------------------------- #
# Sentences
# --------------------------------------------------------------------------- #
def build_sentences(
    words: list[Word],
    *,
    max_words: int = 26,
    max_seconds: float = 12.0,
    pause: float = 0.8,
    soft_pause: float = 0.4,
    soft_min_words: int = 8,
) -> list[Sentence]:
    """Group words into short lines.

    Splits on sentence punctuation where it exists; auto-captions carry none,
    so pauses and hard size limits keep lines short enough to cut on.
    """
    sentences: list[Sentence] = []
    first = 0
    for i, word in enumerate(words):
        count = i - first + 1
        nxt = words[i + 1] if i + 1 < len(words) else None
        gap = (nxt.start - word.end) if nxt else 0.0
        split = (
            nxt is None
            or bool(_SENTENCE_END.search(word.text))
            or gap >= pause
            or (count >= soft_min_words and gap >= soft_pause)
            or count >= max_words
            or (word.end - words[first].start) >= max_seconds
        )
        if split:
            sentences.append(
                Sentence(
                    idx=len(sentences),
                    first_word=first,
                    last_word=i,
                    start=words[first].start,
                    end=word.end,
                    text=" ".join(w.text for w in words[first : i + 1]),
                )
            )
            first = i + 1
    return sentences


def _make(words: list[Word], language: str, source: str) -> Transcript:
    words = [w for w in words if w.text and not _NOISE.match(w.text)]
    words.sort(key=lambda w: w.start)
    # Guarantee monotonic, non-overlapping, non-zero word timings.
    for i, w in enumerate(words):
        if i + 1 < len(words):
            w.end = min(w.end, words[i + 1].start)
        w.end = max(w.end, w.start + 0.04)
    return Transcript(language=language, source=source, words=words, sentences=build_sentences(words))


def _spread(text: str, start: float, end: float) -> list[Word]:
    """Distribute a cue's time over its words, weighted by length."""
    tokens = text.split()
    if not tokens:
        return []
    weights = [len(t) + 1.5 for t in tokens]
    total = sum(weights)
    out, t = [], start
    for tok, w in zip(tokens, weights):
        dur = (end - start) * w / total
        out.append(Word(tok, t, t + dur))
        t += dur
    return out


def _clean(text: str) -> str:
    return _TAG.sub("", text).replace("​", "").replace("\xa0", " ").strip()


# --------------------------------------------------------------------------- #
# YouTube json3
# --------------------------------------------------------------------------- #
def from_json3(data: dict | str, language: str = "und") -> Transcript:
    """Parse YouTube's json3 caption format.

    Auto-generated tracks carry per-word offsets (`tOffsetMs`); manual tracks
    only time whole cues, so their words are interpolated.
    """
    if isinstance(data, str):
        data = json.loads(data)
    events = [e for e in data.get("events", []) if e.get("segs")]
    word_level = any(len(e["segs"]) > 1 or "tOffsetMs" in e["segs"][0] for e in events)

    words: list[Word] = []
    for n, event in enumerate(events):
        t0 = event.get("tStartMs", 0) / 1000
        dur = event.get("dDurationMs", 0) / 1000
        next_t0 = events[n + 1].get("tStartMs", 0) / 1000 if n + 1 < len(events) else t0 + dur
        if word_level:
            segs = [
                (_clean(s.get("utf8", "")), t0 + s.get("tOffsetMs", 0) / 1000)
                for s in event["segs"]
            ]
            segs = [(txt, t) for txt, t in segs if txt]
            for i, (txt, t) in enumerate(segs):
                # A seg may hold several words ("of the"); they share its slot.
                slot_end = segs[i + 1][1] if i + 1 < len(segs) else min(t0 + dur, max(next_t0, t + 0.2))
                slot_end = min(slot_end, t + 0.35 + 0.09 * len(txt))  # don't stretch over silence
                words.extend(_spread(txt, t, max(slot_end, t + 0.05)))
        else:
            text = _clean(" ".join(s.get("utf8", "") for s in event["segs"]).replace("\n", " "))
            end = min(t0 + dur, next_t0) if next_t0 > t0 else t0 + dur
            words.extend(_spread(text, t0, end))
    return _make(words, language, "youtube-auto" if word_level else "youtube-manual")


# --------------------------------------------------------------------------- #
# SRT / VTT
# --------------------------------------------------------------------------- #
_CUE_TIME = re.compile(
    r"(?:(\d+):)?(\d{1,2}):(\d{2})[.,](\d{1,3})\s*-->\s*(?:(\d+):)?(\d{1,2}):(\d{2})[.,](\d{1,3})"
)


def _seconds(h: str | None, m: str, s: str, ms: str) -> float:
    return int(h or 0) * 3600 + int(m) * 60 + int(s) + int(ms.ljust(3, "0")) / 1000


def from_cues(text: str, language: str = "und") -> Transcript:
    """Parse SRT or WebVTT. Rolling duplicate lines (YouTube VTT) are dropped."""
    words: list[Word] = []
    last_line = ""
    blocks = re.split(r"\n\s*\n", text.replace("\r\n", "\n").replace("\r", "\n"))
    for block in blocks:
        lines = [ln for ln in block.split("\n") if ln.strip()]
        for i, line in enumerate(lines):
            m = _CUE_TIME.search(line)
            if not m:
                continue
            start = _seconds(*m.groups()[:4])
            end = _seconds(*m.groups()[4:])
            fresh = []
            for ln in (_clean(x) for x in lines[i + 1 :]):
                if ln and ln != last_line:
                    fresh.append(ln)
                    last_line = ln
            words.extend(_spread(" ".join(fresh), start, end))
            break
    return _make(words, language, "file")


# --------------------------------------------------------------------------- #
# Whisper (optional dependency)
# --------------------------------------------------------------------------- #
def whisper_available() -> bool:
    try:
        import faster_whisper  # noqa: F401
    except ImportError:
        return False
    return True


def from_whisper(media: Path, model: str = "small", language: str | None = None) -> Transcript:
    """Transcribe locally with faster-whisper.

    Runs on the CPU by default: "auto" picks CUDA whenever an NVIDIA driver is
    present and then fails if the CUDA libraries are missing. Set
    SHORTS_AGENT_WHISPER_DEVICE=cuda to use a properly set-up GPU.
    """
    import logging
    import os

    from faster_whisper import WhisperModel

    log = logging.getLogger(__name__)
    device = os.environ.get("SHORTS_AGENT_WHISPER_DEVICE", "cpu")
    engine = WhisperModel(model, device=device, compute_type="int8" if device == "cpu" else "float16")
    segments, info = engine.transcribe(
        str(media), language=language, word_timestamps=True, vad_filter=True
    )
    total = float(getattr(info, "duration", 0) or 0)
    words: list[Word] = []
    next_report = 60.0
    for seg in segments:
        words.extend(Word(w.word.strip(), float(w.start), float(w.end)) for w in (seg.words or []))
        if seg.end >= next_report:
            log.info("   %s / %s transcribed", fmt_time(seg.end), fmt_time(total) if total else "?")
            next_report = seg.end + 60.0
    return _make(words, language or info.language or "und", "whisper")


# --------------------------------------------------------------------------- #
# Files
# --------------------------------------------------------------------------- #
def load_file(path: Path, language: str = "und") -> Transcript:
    text = path.read_text(encoding="utf-8-sig")
    if path.suffix.lower() in {".json", ".json3"}:
        data = json.loads(text)
        if "events" in data:
            return from_json3(data, language)
        return Transcript.from_dict(data)
    return from_cues(text, language)


def find_sidecar(media: Path) -> Path | None:
    for ext in (".json3", ".srt", ".vtt", ".json"):
        candidate = media.with_suffix(ext)
        if candidate.exists():
            return candidate
    return None


def fmt_time(seconds: float) -> str:
    seconds = int(max(0, seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"
