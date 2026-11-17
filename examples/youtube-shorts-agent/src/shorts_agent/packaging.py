"""Packaging: title, catchphrase, description and hashtags for each finished clip.

The editing plan already drafts these, but it does so while juggling a whole
transcript. This second, focused pass sees only what each Short actually
contains, writes several angles, judges them as a stranger scrolling past
would, and keeps the best. It is optional: any failure keeps the drafts.
"""

from __future__ import annotations

import logging
import re

from pydantic import BaseModel, Field

from .models import Clip, Span, Transcript, VideoInfo

log = logging.getLogger(__name__)

PACKAGING_SYSTEM = """\
You are the packaging editor for YouTube Shorts. For each clip you write the \
title and the catchphrase that is burned into the first seconds of the video.

A viewer scrolling past decides in under a second. Every Short needs:
1. Instant clarity - from the catchphrase and title alone it is obvious WHAT \
this is about. Name the concrete subject: the specific person, car, product, \
place, amount or number, taken from the clip or the original video's title. \
Never vague ("This is insane", "Watch till the end", "He really did it").
2. A reason to stay - open a question the clip answers: stakes, conflict, a \
surprising number, a contrast, an outcome nobody expects. Do not give the \
payoff away.
3. Truth - everything the words promise happens in the clip.

Fields
- `hook` (catchphrase on screen): 3-7 words, max 40 characters, readable at \
a glance, no emoji, no hashtags, no trailing period. It complements the \
title instead of repeating it word for word.
- `title`: max 60 characters, the most important words first (phones cut \
titles after about 40 characters), natural wording, at most one word in \
capitals, no hashtags; one emoji at the end only if it adds meaning.
- `description`: 1-2 sentences on what happens and where it is from, ending \
with a short question that invites comments.
- `hashtags`: 3-5 without the #, the specific subject first, then broader \
topics.

Process: for every clip write 4 options with clearly different angles (for \
example stakes, a number or fact, a question, a contrast or outcome). Judge \
each against the three rules from the viewpoint of someone who has never \
heard of this video, then pick the strongest. Transcripts are auto-generated: \
spell names and terms correctly.
"""

PACKAGING_VERSION = 1


class TitleOption(BaseModel):
    title: str
    hook: str


class ClipPackaging(BaseModel):
    clip: int = Field(description="The clip number from the request")
    options: list[TitleOption] = Field(description="4 options with different angles")
    chosen: int = Field(description="0-based index of the best option")
    why: str = Field(description="One sentence: why this option wins")
    description: str
    hashtags: list[str]


class PackagingPlan(BaseModel):
    clips: list[ClipPackaging]


def span_text(transcript: Transcript, span: Span) -> str:
    return " ".join(w.text for w in transcript.words if span.start <= (w.start + w.end) / 2 < span.end)


def build_request(info: VideoInfo, transcript: Transcript, clips: list[Clip], language: str) -> str:
    parts = [
        f"<video>\ntitle: {info.title}\nchannel: {info.channel}\n"
        f"description: {info.description[:600]}\n</video>\n"
    ]
    for n, clip in enumerate(clips, 1):
        teaser = f"<teaser>{span_text(transcript, clip.teaser)}</teaser>\n" if clip.teaser else ""
        body = " [cut] ".join(span_text(transcript, s) for s in clip.spans)
        parts.append(
            f'<clip number="{n}" kind="{clip.kind}" seconds="{clip.duration:.0f}">\n{teaser}'
            f"<transcript>{body}</transcript>\n"
            f"<draft title=\"{clip.title}\" hook=\"{clip.hook}\" />\n</clip>"
        )
    parts.append(f"Package every clip. Write all text in {language}.")
    return "\n".join(parts)


_EMOJI = re.compile("[\U0001F000-\U0001FAFF☀-➿️]")


def _clean_hook(text: str) -> str:
    return " ".join(_EMOJI.sub("", text).replace("#", "").split()).rstrip(" .")


def apply(clips: list[Clip], plan: PackagingPlan) -> int:
    """Copy the chosen options onto the clips. Returns how many clips changed."""
    changed = 0
    for pack in plan.clips:
        if not 1 <= pack.clip <= len(clips) or not pack.options:
            continue
        clip = clips[pack.clip - 1]
        best = pack.options[max(0, min(len(pack.options) - 1, pack.chosen))]
        title, hook = best.title.strip(), _clean_hook(best.hook)
        if not title or len(title) > 100 or not hook or len(hook) > 60:
            continue
        clip.title, clip.hook = title, hook
        clip.title_options = [o.title.strip() for o in pack.options if o.title.strip() and o.title.strip() != title]
        if pack.description.strip():
            clip.description = pack.description.strip()
        tags = [h.lstrip("#").replace(" ", "") for h in pack.hashtags if h.strip("# ")]
        if tags:
            clip.hashtags = tags[:6]
        clip.reason = pack.why.strip() or clip.reason
        changed += 1
    return changed


def package(
    info: VideoInfo,
    transcript: Transcript,
    clips: list[Clip],
    language: str,
    *,
    model: str | None = None,
    effort: str = "high",
) -> PackagingPlan:
    from .analyze import call_claude

    return call_claude(
        PACKAGING_SYSTEM,
        build_request(info, transcript, clips, language),
        PackagingPlan,
        model=model,
        effort=effort,
        max_tokens=16000,
    )
