"""Animated word-by-word captions as an ASS subtitle file (burned in by ffmpeg)."""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field

from .models import Span, Transcript

W, H = 1080, 1920
_SENTENCE_END = re.compile(r"[.!?…][\"'”’)\]]*$")
_FONT_PREFERENCE = (
    "Montserrat", "Poppins", "Inter Display", "Inter", "Roboto", "Helvetica Neue",
    "Arial", "Liberation Sans", "Noto Sans", "DejaVu Sans",
)


@dataclass
class CaptionStyle:
    font: str | None = None  # None -> best installed font
    size: int = 88
    uppercase: bool = True
    highlight: str = "#FFE500"  # colour of the word being spoken
    max_words: int = 3
    max_chars: int = 16
    bottom_margin: int = 500  # keeps clear of the Shorts UI overlay
    hook_size: int = 58
    hook_margin: int = 230
    catch_size: int = 86
    catch_margin: int = 330


@dataclass
class TimedWord:
    text: str
    start: float
    end: float
    part: int = 0


@dataclass
class Group:
    words: list[TimedWord] = field(default_factory=list)

    @property
    def chars(self) -> int:
        return sum(len(w.text) for w in self.words) + max(0, len(self.words) - 1)


def pick_font(preferred: str | None = None) -> str:
    """Use the requested font, else the best-looking installed sans."""
    if preferred:
        return preferred
    try:
        out = subprocess.run(
            ["fc-list", ":", "family"], capture_output=True, text=True, timeout=10, check=False
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return "Arial"
    families = {name.strip() for line in out.splitlines() for name in line.split(",")}
    return next((f for f in _FONT_PREFERENCE if f in families), "Arial")


def ass_color(hex_color: str) -> str:
    """#RRGGBB -> ASS &HBBGGRR&."""
    h = hex_color.lstrip("#")
    if not re.fullmatch(r"[0-9a-fA-F]{6}", h):
        raise ValueError(f"Not a #RRGGBB colour: {hex_color!r}")
    return f"&H{h[4:6]}{h[2:4]}{h[0:2]}&".upper()


def _timestamp(t: float) -> str:
    cs = max(0, round(t * 100))
    h, rem = divmod(cs, 360000)
    m, rem = divmod(rem, 6000)
    s, cs = divmod(rem, 100)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def _escape(text: str) -> str:
    return text.replace("\\", "⧵").replace("{", "(").replace("}", ")").replace("\n", " ")


def timeline_words(transcript: Transcript, spans: list[Span]) -> list[TimedWord]:
    """Map the source words inside each span onto the output timeline."""
    out: list[TimedWord] = []
    offset = 0.0
    for part, span in enumerate(spans):
        for w in transcript.words:
            mid = (w.start + w.end) / 2
            if span.start <= mid < span.end:
                start = max(0.0, w.start - span.start) + offset
                end = min(span.duration, w.end - span.start) + offset
                out.append(TimedWord(w.text, start, max(end, start + 0.04), part))
        offset += span.duration
    return out


def group_words(words: list[TimedWord], style: CaptionStyle) -> list[Group]:
    """Chunk words into short on-screen phrases that follow speech rhythm."""
    groups: list[Group] = []
    current = Group()
    for w in words:
        if current.words:
            last = current.words[-1]
            breaks = (
                len(current.words) >= style.max_words
                or current.chars + 1 + len(w.text) > style.max_chars
                or bool(_SENTENCE_END.search(last.text))
                or (last.text.endswith((",", ";", ":")) and len(current.words) >= 2)
                or w.start - last.end > 0.6
                or w.part != last.part
            )
            if breaks:
                groups.append(current)
                current = Group()
        current.words.append(w)
    if current.words:
        groups.append(current)
    return groups


def _display(text: str, uppercase: bool) -> str:
    text = text.strip().rstrip(".,;:")
    text = text.strip("\"“”„")
    return _escape(text.upper() if uppercase else text)


def _wrap_hook(hook: str, limit: int = 20) -> str:
    """Balance a headline over as few lines as fit `limit` characters each."""
    words = hook.split()
    if not words:
        return ""
    text = " ".join(words)
    lines_needed = max(1, -(-len(text) // limit))
    target = len(text) / lines_needed
    lines, current = [], ""
    for word in words:
        if current and len(current) + 1 + len(word) > max(target, len(word)) + 2:
            lines.append(current)
            current = word
        else:
            current = f"{current} {word}".strip()
    lines.append(current)
    return r"\N".join(_escape(line) for line in lines)


def build_ass(
    words: list[TimedWord],
    total: float,
    style: CaptionStyle,
    hook: str | None = None,
    catch_until: float = 0.0,
) -> str:
    """Captions, plus the hook: big catchphrase until `catch_until`, then a headline."""
    font = pick_font(style.font)
    hi = ass_color(style.highlight)
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {W}
PlayResY: {H}
WrapStyle: 0
ScaledBorderAndShadow: yes
YCbCr Matrix: TV.709

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Caption,{font},{style.size},&H00FFFFFF,&H00FFFFFF,&H00000000,&H96000000,-1,0,0,0,100,100,0,0,1,7,3,2,70,70,{style.bottom_margin},1
Style: Hook,{font},{style.hook_size},&H00FFFFFF,&H00FFFFFF,&H00000000,&H96000000,-1,0,0,0,100,100,0,0,1,5,2,8,90,90,{style.hook_margin},1
Style: Catch,{font},{style.catch_size},&H00FFFFFF,&H00FFFFFF,&H00000000,&H96000000,-1,0,0,0,100,100,0,0,1,8,3,8,80,80,{style.catch_margin},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    lines: list[str] = []
    if hook:
        catch_until = min(max(0.0, catch_until), total)
        if catch_until > 0.5:
            catch = hook.upper() if style.uppercase else hook
            lines.append(
                f"Dialogue: 2,{_timestamp(0)},{_timestamp(catch_until)},Catch,,0,0,0,,"
                f"{{\\fscx135\\fscy135\\t(0,220,\\fscx100\\fscy100)\\fad(0,180)}}{_wrap_hook(catch, 15)}"
            )
        lines.append(
            f"Dialogue: 1,{_timestamp(catch_until if catch_until > 0.5 else 0)},{_timestamp(total)},Hook,,0,0,0,,"
            f"{{\\fad(250,0)}}{_wrap_hook(hook)}"
        )

    groups = group_words(words, style)
    for gi, group in enumerate(groups):
        next_start = groups[gi + 1].words[0].start if gi + 1 < len(groups) else total
        group_end = min(group.words[-1].end + 0.3, next_start, total)
        shown = [_display(w.text, style.uppercase) for w in group.words]
        # Shrink phrases that are wider than the frame (long compound words).
        width = sum(len(s) for s in shown) + len(shown) - 1
        size = "" if width <= style.max_chars else f"\\fs{max(40, round(style.size * style.max_chars / width))}"
        for wi, word in enumerate(group.words):
            start = word.start
            end = group.words[wi + 1].start if wi + 1 < len(group.words) else group_end
            if end - start < 0.02:
                continue
            pop = "\\fscx86\\fscy86\\t(0,90,\\fscx100\\fscy100)" if wi == 0 else ""
            prefix = f"{{{size}{pop}}}" if (size or pop) else ""
            text = " ".join(
                f"{{\\1c{hi}}}{s}{{\\1c&HFFFFFF&}}" if i == wi else s
                for i, s in enumerate(shown)
                if s
            )
            if text:
                lines.append(
                    f"Dialogue: 0,{_timestamp(start)},{_timestamp(end)},Caption,,0,0,0,,{prefix}{text}"
                )
    return header + "\n".join(lines) + "\n"
