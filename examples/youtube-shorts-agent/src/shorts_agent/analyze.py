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
    recap: bool = False
    min_len: float = 45.0
    max_len: float = 60.0
    teaser: bool = True
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

The Shorts must make sense. A viewer who has never seen the video must \
understand every clip and feel it was worth watching.

What makes a good highlight Short
- It is ONE continuous passage (one part) that tells a complete mini-story: \
setup, development, payoff. No jumps.
- It stands alone: no dangling "this", "he", "as I said", no answer without \
its question. Start on the line where the thought begins, not in the middle.
- It ends on the line that completes the thought - a punchline, result, \
reveal or takeaway - never mid-sentence and never on a teaser for something \
the clip does not show.
- It uses the time it is given: aim for the upper end of the length range \
rather than cutting a story short.
- Prefer the most surprising, emotional, funny, impressive or valuable \
moments over intros, sponsor reads, greetings, housekeeping and outros.
- Highlights must not overlap each other.

What makes a good recap Short
- It retells the whole video in under a minute with at most 3 parts in \
chronological order (premise, main point, conclusion). Each part is at least \
10 seconds and a complete statement; together they read as one coherent story.

The opening - this decides whether people keep watching
- `hook` is the catchphrase shown big on screen for the first seconds. 3-7 \
words, max 40 characters, in the output language. Someone who has never \
heard of this video must understand at a glance WHAT is going on (name the \
concrete subject: the person, car, product, place, number) and want to see \
the rest (stakes, a surprising number, a contrast, an open question). Never \
vague ("This is crazy", "Watch this"). It must be true to the clip. No \
emojis, no hashtags, no trailing period.
- `teaser_sentence` is a cold open: the single most gripping line from inside \
the clip (a reaction, a shocking statement, the climax), at most ~6 seconds \
long, played once before the clip starts from its beginning. It must not be \
the clip's first line, and it must not give the whole ending away if the \
payoff is a reveal. Use -1 when the first line is already the strongest hook.

Other text fields
- `title`: instantly clear and curiosity-driven: the concrete subject first, \
then the tension; max 60 characters, no hashtags, no clickbait the clip does \
not deliver. Use the original video title and description to name things \
the transcript leaves implicit.
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
            f"- up to {brief.highlights} clips of kind `highlight` (one continuous part each), "
            f"each {brief.min_len:.0f}-{brief.max_len:.0f} seconds long including the teaser, best first. "
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
        + ("" if brief.teaser else "\n- set `teaser_sentence` to -1 for every clip (no cold opens)")
        + "\n\nAdd up the line timestamps to check every clip's length before you answer. "
        f"Write all text fields in {language}."
    )


def call_claude(system: str, user: str, output_format, *, model: str | None = None,
                effort: str = "high", max_tokens: int = 32000):
    """One structured-output request; returns the parsed object. Raises AnalysisUnavailable
    when Claude cannot be used at all (no/invalid key, refusal), RuntimeError otherwise."""
    import anthropic

    model = model or os.environ.get("SHORTS_AGENT_MODEL") or DEFAULT_MODEL
    try:
        client = anthropic.Anthropic()
        # Streaming keeps long transcripts clear of HTTP timeouts. The fallback
        # lets the API re-run the request on another model if a safety
        # classifier declines it, instead of failing the whole run.
        with client.beta.messages.stream(
            model=model,
            max_tokens=max_tokens,
            thinking={"type": "adaptive"},
            output_config={"effort": effort},
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            system=system,
            messages=[{"role": "user", "content": user}],
            output_format=output_format,
        ) as stream:
            message = stream.get_final_message()
    except anthropic.AuthenticationError as exc:
        raise AnalysisUnavailable("Claude rejected the API key (invalid or revoked)") from exc
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


def plan_with_claude(
    info: VideoInfo,
    transcript: Transcript,
    brief: EditBrief,
    *,
    model: str | None = None,
    effort: str = "high",
) -> VideoPlan:
    return call_claude(
        SYSTEM_PROMPT, build_user_prompt(info, transcript, brief), VideoPlan, model=model, effort=effort
    )


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


def _video_title(title: str, part: int = 0, german: bool = False) -> str:
    """The original title, de-shouted and shortened; '(Teil 2)' for several clips."""
    words = " ".join(title.split()).split(" ")
    if sum(c.isupper() for c in title) > 0.6 * max(1, sum(c.isalpha() for c in title)):
        words = [w if any(ch.isdigit() for ch in w) else w.capitalize() for w in words]
    suffix = (f" (Teil {part})" if german else f" (Part {part})") if part else ""
    return _truncate(" ".join(words), 70 - len(suffix)) + suffix


def _catchphrase(text: str, limit: int = 40) -> str:
    """A clean on-screen line from a transcript sentence: whole clause, never a cut-off word."""
    text = " ".join(text.split()).strip(" .!?,;:")
    clause = re.split(r"[,;:–—]\s+", text)[0]
    if len(clause.split()) >= 3 and len(clause) <= limit:
        return clause
    if len(text) <= limit:
        return text
    return _truncate(text, limit - 1).rstrip(" ,;:-") + "…"


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

    def window(i: int, target: float) -> int:
        j = i
        while j + 1 < len(sents) and sents[j].end - sents[i].start < target:
            j += 1
        return j

    def teaser_in(i: int, j: int) -> int:
        if not brief.teaser:
            return -1
        options = [k for k in range(i + 1, j + 1) if 1.2 <= sents[k].end - sents[k].start <= 6.0]
        return max(options, key=lambda k: scores[k]) if options else -1

    if brief.recap and sents:
        part_len = max(10.0, brief.max_len / 3 - 1)
        candidates = sorted(
            ((sum(scores[i : window(i, part_len) + 1]) / (window(i, part_len) - i + 1), i, window(i, part_len))
             for i in range(len(sents))),
            reverse=True,
        )
        chosen: list[tuple[int, int]] = []
        for _, i, j in candidates:
            if len(chosen) == 3:
                break
            if all(j < a - 1 or i > b + 1 for a, b in chosen):
                chosen.append((i, j))
        chosen.sort()
        clips.append(ClipPlan(
            kind="recap",
            title=_truncate(info.title, 60),
            hook="Das ganze Video in 60 Sekunden" if german else "The whole video in 60 seconds",
            teaser_sentence=-1,
            description=_truncate(info.title, 150),
            hashtags=tags,
            parts=[PartPlan(first_sentence=i, last_sentence=j) for i, j in chosen],
            reason="Densest passages in chronological order",
            score=50,
        ))

    target = (brief.min_len + brief.max_len) / 2
    windows = []
    for i in range(len(sents)):
        j = window(i, target)
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
        teaser = teaser_in(i, j)
        catch = sents[teaser if teaser >= 0 else i].text
        clips.append(ClipPlan(
            kind="highlight",
            title=_video_title(info.title, 0, german),
            hook=_catchphrase(catch),
            teaser_sentence=teaser,
            description=_truncate(info.title, 150),
            hashtags=tags,
            parts=[PartPlan(first_sentence=i, last_sentence=j)],
            reason="Dense passage (keyword statistics)",
            score=max(1, min(99, round(40 + 40 * score))),
        ))

    numbered = [c for c in clips if c.kind == "highlight"]
    if len(numbered) > 1:  # several clips from one video: "(Teil 1)", "(Teil 2)", ...
        for n, c in enumerate(numbered, 1):
            c.title = _video_title(info.title, n, german)

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
