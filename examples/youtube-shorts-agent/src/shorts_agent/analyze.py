"""The editor: summarise the video and decide which moments become Shorts.

`plan_with_claude` asks Claude for a structured editing plan that references
transcript lines by index (never raw timestamps, so nothing can be invented).
`plan_heuristic` is a keyless extractive fallback.
"""

from __future__ import annotations

import logging
import math
import os
import re
from collections import Counter
from dataclasses import dataclass

from .models import ClipPlan, KeyPoint, PartPlan, Transcript, VideoInfo, VideoPlan
from .transcript import fmt_time

log = logging.getLogger(__name__)

DEFAULT_MODEL = "claude-opus-5-5"


@dataclass
class EditBrief:
    highlights: int = 3
    recap: bool = True
    min_len: float = 20.0
    max_len: float = 58.0
    output_language: str | None = None  # None -> the video's language


class AnalysisUnavailable(RuntimeError):
    """Claude could not be used (no credentials, refusal, ...)."""


SYSTEM_PROMPT = """\
You are a senior short-form video editor. You receive the transcript of a long \
video and turn it into (1) an honest summary and (2) an editing plan for \
vertical Shorts cut from the original footage.

The transcript is a list of numbered lines: `[index] start-end text`. You \
select footage only by line index. A part plays every line from \
`first_sentence` to `last_sentence` inclusive, uncut.

What makes a good highlight Short
- It stands alone. A viewer who has never seen the video understands it: no \
dangling "this", "he", "as I said", no answer without its question.
- It opens on a hook: a bold claim, a question, a surprising fact, tension. \
Start on the line where the thought begins, not in the middle of one.
- It pays off. It ends on the line that completes the thought - a punchline, \
conclusion or takeaway - never mid-sentence and never on a teaser for \
something the clip does not show.
- One idea per Short. Prefer the most valuable, surprising, emotional or funny \
moments over generic intros, sponsor reads, greetings, housekeeping and outros.
- Highlights must not overlap each other.

What makes a good recap Short
- It retells the whole video in under a minute using 3-7 short parts in \
chronological order: the premise, the main points, the conclusion.
- Every part is a complete statement on its own, and consecutive parts must \
read coherently when played back to back.

Text fields
- `title`: specific and curiosity-driven, max 60 characters, no hashtags, no \
clickbait the clip does not deliver.
- `hook`: the on-screen headline, max 32 characters, punchy, no trailing period.
- `score`: your honest estimate (0-100) of how well the clip works on its own.
- The summary must only state what the transcript supports. Auto-generated \
transcripts contain recognition errors and lack punctuation: read through \
them, and spell names and terms correctly in your own text.
"""


def render_transcript(transcript: Transcript) -> str:
    return "\n".join(
        f"[{s.idx}] {fmt_time(s.start)}-{fmt_time(s.end)} {s.text}" for s in transcript.sentences
    )


def build_user_prompt(info: VideoInfo, transcript: Transcript, brief: EditBrief) -> str:
    language = brief.output_language or f"the language spoken in the video ({transcript.language})"
    wanted = []
    if brief.recap:
        wanted.append(
            f"- exactly 1 clip of kind `recap`, total length {brief.min_len:.0f}-{brief.max_len:.0f} seconds"
        )
    if brief.highlights:
        wanted.append(
            f"- up to {brief.highlights} clips of kind `highlight` (one part each), "
            f"each {brief.min_len:.0f}-{brief.max_len:.0f} seconds long, best first. "
            "Return fewer if the video does not contain that many strong moments."
        )
    return (
        f"<video>\ntitle: {info.title}\nchannel: {info.channel}\n"
        f"duration: {fmt_time(info.duration or transcript.duration)}\n"
        f"description: {info.description[:800]}\n</video>\n\n"
        f"<transcript>\n{render_transcript(transcript)}\n</transcript>\n\n"
        "Produce:\n"
        "- `tldr`, `summary` and 4-8 `key_points` (each anchored to the line where it is made)\n"
        + "\n".join(wanted)
        + "\n\nAdd up the line timestamps to check every clip's length before you answer. "
        f"Write all text fields in {language}."
    )


def plan_with_claude(
    info: VideoInfo,
    transcript: Transcript,
    brief: EditBrief,
    *,
    model: str | None = None,
    effort: str = "high",
) -> VideoPlan:
    import anthropic

    model = model or os.environ.get("SHORTS_AGENT_MODEL") or DEFAULT_MODEL
    try:
        client = anthropic.Anthropic()
        # Streaming keeps long transcripts clear of HTTP timeouts. The fallback
        # lets the API re-run the request on another model if a safety
        # classifier declines it, instead of failing the whole run.
        with client.beta.messages.stream(
            model=model,
            max_tokens=32000,
            thinking={"type": "adaptive"},
            output_config={"effort": effort},
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": build_user_prompt(info, transcript, brief)}],
            output_format=VideoPlan,
        ) as stream:
            message = stream.get_final_message()
    except anthropic.AuthenticationError as exc:
        raise AnalysisUnavailable(f"Claude rejected the credentials: {exc.message}") from exc
    except anthropic.PermissionDeniedError as exc:
        raise AnalysisUnavailable(f"No access to model {model}: {exc.message}") from exc
    except anthropic.RateLimitError as exc:
        raise RuntimeError("Claude rate limit reached - try again in a minute") from exc
    except anthropic.APIStatusError as exc:
        raise RuntimeError(f"Claude API error {exc.status_code}: {exc.message}") from exc
    except anthropic.APIConnectionError as exc:
        raise RuntimeError(f"Could not reach the Claude API: {exc}") from exc
    except anthropic.AnthropicError as exc:
        raise AnalysisUnavailable(str(exc)) from exc
    except TypeError as exc:
        # The SDK signals "no API key / token / profile found" with a TypeError.
        if "authentication" not in str(exc).lower():
            raise
        raise AnalysisUnavailable("no Claude credentials configured") from exc

    if message.stop_reason == "refusal":
        raise AnalysisUnavailable("Claude declined to analyse this video")
    if message.stop_reason == "max_tokens" or message.parsed_output is None:
        raise RuntimeError(f"Claude returned an incomplete plan (stop_reason={message.stop_reason})")
    usage = message.usage
    log.info("Claude (%s): %d input / %d output tokens", message.model, usage.input_tokens, usage.output_tokens)
    return message.parsed_output


# --------------------------------------------------------------------------- #
# Keyless fallback
# --------------------------------------------------------------------------- #
_STOP = set(
    """
    a an the and or but if so of to in on at for with from by as is are was were be been being it its
    this that these those i you he she we they me my your his her our their them us what which who how
    why when where there here not no yes do does did have has had will would can could should just like
    really very about into than then also more most some any all one two get got going know think thing
    things yeah okay well right actually because up out over
    der die das den dem des ein eine einen einem einer eines und oder aber wenn dann also von zu im in
    an auf für mit aus bei als ist sind war waren sein bin bist wird werden wurde es ich du er sie wir
    ihr mich mir dich dir uns euch mein dein nicht kein keine ja nein was wer wie warum wo da hier noch
    schon nur auch sehr mal so man hat habe haben hatte kann können muss müssen soll dass ob weil denn
    doch eben halt ganz mehr immer wieder jetzt um über unter nach vor durch am zum zur vom ins beim
    """.split()
)
_TOKEN = re.compile(r"[^\W\d_]{3,}", re.UNICODE)


def _sentence_scores(transcript: Transcript) -> list[float]:
    tokens = [[t for t in _TOKEN.findall(s.text.lower()) if t not in _STOP] for s in transcript.sentences]
    freq = Counter(t for sent in tokens for t in sent)
    top = max(freq.values(), default=1)
    scores = []
    for sent, toks in zip(transcript.sentences, tokens):
        if not toks:
            scores.append(0.0)
            continue
        score = sum(freq[t] / top for t in toks) / math.sqrt(len(toks))
        if re.search(r"\d", sent.text):
            score *= 1.15
        if "?" in sent.text:
            score *= 1.1
        scores.append(score)
    return scores


def _truncate(text: str, limit: int) -> str:
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    return text[: limit + 1].rsplit(" ", 1)[0].rstrip(" ,;:-") or text[:limit]


def plan_heuristic(info: VideoInfo, transcript: Transcript, brief: EditBrief) -> VideoPlan:
    """Extractive plan from word statistics. Works offline; far cruder than Claude."""
    sents = transcript.sentences
    scores = _sentence_scores(transcript)
    german = transcript.language.startswith("de")
    tags = [w for w, _ in Counter(
        t for s in sents for t in _TOKEN.findall(s.text.lower()) if t not in _STOP and len(t) > 4
    ).most_common(4)]
    clips: list[ClipPlan] = []
    ranked = sorted(range(len(sents)), key=lambda i: scores[i], reverse=True)

    if brief.recap and sents:
        budget, chosen, total = brief.max_len * 0.9, [], 0.0
        for i in ranked:
            dur = sents[i].end - sents[i].start + 0.4
            if total + dur <= budget:
                chosen.append(i)
                total += dur
        chosen.sort()
        clips.append(ClipPlan(
            kind="recap",
            title=_truncate(info.title, 60),
            hook="In 60 Sekunden" if german else "In 60 seconds",
            description=_truncate(info.title, 150),
            hashtags=tags,
            parts=[PartPlan(first_sentence=i, last_sentence=i) for i in chosen],
            reason="Highest-scoring statements in chronological order",
            score=50,
        ))

    target = (brief.min_len + brief.max_len) / 2
    windows = []
    for i in range(len(sents)):
        j = i
        while j + 1 < len(sents) and sents[j].end - sents[i].start < target:
            j += 1
        if sents[j].end - sents[i].start < brief.min_len:
            break
        mean = sum(scores[i : j + 1]) / (j - i + 1)
        windows.append((mean + 0.25 * scores[i], i, j))  # reward a strong opening line
    taken: list[tuple[int, int]] = []
    for score, i, j in sorted(windows, reverse=True):
        if len(taken) >= brief.highlights:
            break
        if any(i <= b and a <= j for a, b in taken):
            continue
        taken.append((i, j))
        clips.append(ClipPlan(
            kind="highlight",
            title=_truncate(sents[i].text.capitalize(), 60),
            hook=_truncate(info.title, 32),
            description=_truncate(info.title, 150),
            hashtags=tags,
            parts=[PartPlan(first_sentence=i, last_sentence=j)],
            reason="Dense passage (keyword statistics)",
            score=max(1, min(99, round(40 + 40 * score))),
        ))

    top = sorted(ranked[:6])
    return VideoPlan(
        tldr=_truncate(sents[top[0]].text, 200) if top else info.title,
        summary=" ".join(sents[i].text for i in top),
        key_points=[KeyPoint(sentence=i, text=sents[i].text) for i in top],
        clips=clips,
    )


def make_plan(
    info: VideoInfo,
    transcript: Transcript,
    brief: EditBrief,
    *,
    analyzer: str = "auto",
    model: str | None = None,
    effort: str = "high",
) -> tuple[VideoPlan, str]:
    """Return the plan and the name of the analyzer that produced it."""
    if not transcript.sentences:
        raise RuntimeError("The transcript is empty - nothing to analyse")
    if analyzer in ("auto", "claude"):
        try:
            return plan_with_claude(info, transcript, brief, model=model, effort=effort), "claude"
        except AnalysisUnavailable as exc:
            if analyzer == "claude":
                raise
            log.warning(
                "Claude unavailable (%s). Falling back to the offline heuristic - "
                "set ANTHROPIC_API_KEY for real editorial quality.", exc,
            )
    return plan_heuristic(info, transcript, brief), "heuristic"
