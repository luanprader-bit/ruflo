"""Turn an editing plan (line indices) into concrete, valid cut points."""

from __future__ import annotations

import logging

from .models import Clip, ClipPlan, Span, Transcript, VideoPlan

log = logging.getLogger(__name__)

LEAD_IN = 0.10  # seconds of air before the first word
TAIL = 0.28  # seconds after the last word so it is not clipped


def _span(transcript: Transcript, first: int, last: int) -> Span:
    sents, words = transcript.sentences, transcript.words
    s, e = sents[first], sents[last]
    prev_end = words[s.first_word - 1].end if s.first_word > 0 else 0.0
    start = max(0.0, prev_end, s.start - LEAD_IN)
    end = e.end + TAIL
    if e.last_word + 1 < len(words):  # never run into the next spoken word
        end = max(e.end + 0.04, min(end, words[e.last_word + 1].start))
    return Span(start, end)


def _normalise(parts: list[tuple[int, int]], n: int) -> list[tuple[int, int]]:
    """Clamp, order and merge touching/overlapping ranges."""
    cleaned = []
    for a, b in parts:
        a, b = sorted((max(0, min(n - 1, a)), max(0, min(n - 1, b))))
        cleaned.append((a, b))
    cleaned.sort()
    merged: list[tuple[int, int]] = []
    for a, b in cleaned:
        if merged and a <= merged[-1][1] + 1:
            merged[-1] = (merged[-1][0], max(merged[-1][1], b))
        else:
            merged.append((a, b))
    return merged


def _total(transcript: Transcript, parts: list[tuple[int, int]]) -> float:
    return sum(_span(transcript, a, b).duration for a, b in parts)


def _fit(transcript: Transcript, parts: list[tuple[int, int]], min_len: float, max_len: float):
    n = len(transcript.sentences)
    # Too long: shave lines off the end (the opening hook matters most).
    while parts and _total(transcript, parts) > max_len:
        a, b = parts[-1]
        if b > a:
            parts[-1] = (a, b - 1)
        elif len(parts) > 1:
            parts.pop()
        else:
            break
    # Too short: let the last part run on, then reach back before the first.
    while parts and _total(transcript, parts) < min_len:
        a, b = parts[-1]
        nxt = parts[0][0] - 1
        if b + 1 < n and _total(transcript, parts[:-1] + [(a, b + 1)]) <= max_len:
            parts[-1] = (a, b + 1)
        elif nxt >= 0 and _total(transcript, [(nxt, parts[0][1])] + parts[1:]) <= max_len:
            parts[0] = (nxt, parts[0][1])
        else:
            break
        parts = _normalise(parts, n)
    return parts


def resolve_clip(plan: ClipPlan, transcript: Transcript, min_len: float, max_len: float) -> Clip | None:
    n = len(transcript.sentences)
    if not n or not plan.parts:
        return None
    parts = _normalise([(p.first_sentence, p.last_sentence) for p in plan.parts], n)
    parts = _fit(transcript, parts, min_len, max_len)
    spans = [_span(transcript, a, b) for a, b in parts]
    if not spans or sum(s.duration for s in spans) < 3:
        return None
    return Clip(
        kind=plan.kind,
        title=plan.title.strip(),
        hook=plan.hook.strip().rstrip("."),
        description=plan.description.strip(),
        hashtags=[h.lstrip("#").replace(" ", "") for h in plan.hashtags if h.strip("# ")][:6],
        reason=plan.reason,
        score=max(0, min(100, plan.score)),
        spans=spans,
    )


def _overlap(a: Clip, b: Clip) -> float:
    shared = sum(
        max(0.0, min(x.end, y.end) - max(x.start, y.start)) for x in a.spans for y in b.spans
    )
    return shared / max(0.01, min(a.duration, b.duration))


def resolve_plan(
    plan: VideoPlan, transcript: Transcript, *, min_len: float, max_len: float, highlights: int, recap: bool
) -> list[Clip]:
    """Recap first, then highlights by score. Near-duplicate highlights are dropped."""
    resolved = [c for p in plan.clips if (c := resolve_clip(p, transcript, min_len, max_len))]
    out: list[Clip] = []
    if recap:
        out.extend([c for c in resolved if c.kind == "recap"][:1])
    kept: list[Clip] = []
    for clip in sorted((c for c in resolved if c.kind == "highlight"), key=lambda c: -c.score):
        if len(kept) >= highlights:
            break
        if any(_overlap(clip, other) > 0.5 for other in kept):
            log.info("Dropping overlapping highlight: %s", clip.title)
            continue
        kept.append(clip)
    return out + kept
