"""Shared data types: transcript primitives and the editing plan."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Literal

from pydantic import BaseModel, Field


# --------------------------------------------------------------------------- #
# Transcript
# --------------------------------------------------------------------------- #
@dataclass
class Word:
    text: str
    start: float
    end: float


@dataclass
class Sentence:
    """A short run of words. The unit the editor (LLM) points at."""

    idx: int
    first_word: int  # index into Transcript.words (inclusive)
    last_word: int  # inclusive
    start: float
    end: float
    text: str


@dataclass
class Transcript:
    language: str
    source: str  # "youtube-auto" | "youtube-manual" | "whisper" | "file"
    words: list[Word] = field(default_factory=list)
    sentences: list[Sentence] = field(default_factory=list)

    @property
    def duration(self) -> float:
        return self.words[-1].end if self.words else 0.0

    def to_dict(self) -> dict:
        return {
            "language": self.language,
            "source": self.source,
            "words": [asdict(w) for w in self.words],
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Transcript":
        from .transcript import build_sentences  # local import: avoid cycle

        words = [Word(**w) for w in data["words"]]
        return cls(
            language=data.get("language", "und"),
            source=data.get("source", "file"),
            words=words,
            sentences=build_sentences(words),
        )


# --------------------------------------------------------------------------- #
# Editing plan (structured output of the analysis step)
# --------------------------------------------------------------------------- #
class PartPlan(BaseModel):
    first_sentence: int = Field(description="Index of the first transcript line of this part")
    last_sentence: int = Field(description="Index of the last transcript line of this part (inclusive)")


class ClipPlan(BaseModel):
    kind: Literal["highlight", "recap"]
    title: str = Field(description="Upload title, max 60 characters, no hashtags")
    hook: str = Field(
        description="Catchphrase shown big on screen in the first seconds, 3-7 words, max 40 characters"
    )
    teaser_sentence: int = Field(
        description="Index of the single most gripping line inside this clip (max ~6 s, not its first "
        "line), played as a cold open before the clip starts; -1 for no teaser"
    )
    description: str = Field(description="1-2 sentence upload description")
    hashtags: list[str] = Field(description="3-5 hashtags without the leading #")
    parts: list[PartPlan] = Field(description="Source ranges, in playback order")
    reason: str = Field(description="Why this works as a Short")
    score: int = Field(description="Expected performance, 0-100")


class KeyPoint(BaseModel):
    sentence: int = Field(description="Transcript line where this point is made")
    text: str


class VideoPlan(BaseModel):
    tldr: str = Field(description="One-sentence summary")
    summary: str = Field(description="Summary of the whole video, 1-3 short paragraphs")
    key_points: list[KeyPoint]
    clips: list[ClipPlan]


# --------------------------------------------------------------------------- #
# Resolved clip: the plan snapped to real timestamps
# --------------------------------------------------------------------------- #
@dataclass
class Span:
    start: float
    end: float

    @property
    def duration(self) -> float:
        return self.end - self.start


@dataclass
class Clip:
    kind: str
    title: str
    hook: str
    description: str
    hashtags: list[str]
    reason: str
    score: int
    spans: list[Span]
    teaser: Span | None = None  # cold open, played before `spans`

    @property
    def playlist(self) -> list[Span]:
        return ([self.teaser] if self.teaser else []) + self.spans

    @property
    def duration(self) -> float:
        return sum(s.duration for s in self.playlist)


@dataclass
class VideoInfo:
    id: str
    title: str
    channel: str = ""
    url: str = ""
    duration: float = 0.0
    description: str = ""
    language: str | None = None
