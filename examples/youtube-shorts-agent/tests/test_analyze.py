from types import SimpleNamespace

import pytest

from shorts_agent import analyze
from shorts_agent.clips import resolve_clip, resolve_plan
from shorts_agent.models import ClipPlan, KeyPoint, PartPlan, VideoPlan


def clip(kind, parts, score=80, title="T", teaser=-1):
    return ClipPlan(kind=kind, title=title, hook="Hook.", teaser_sentence=teaser, description="d", hashtags=["#a b", "c"],
                    parts=[PartPlan(first_sentence=a, last_sentence=b) for a, b in parts],
                    reason="r", score=score)


def test_prompt_lists_every_line_with_index(info, transcript):
    prompt = analyze.build_user_prompt(info, transcript, analyze.EditBrief(highlights=2, recap=True))
    assert "[0] 0:00-0:05 Most people think" in prompt
    assert "[9] 0:54-0:59" in prompt
    assert "up to 2 clips of kind `highlight` (one continuous part each)" in prompt and "`recap`" in prompt
    assert "teaser_sentence` to -1" not in prompt
    no_teaser = analyze.build_user_prompt(info, transcript, analyze.EditBrief(teaser=False))
    assert "teaser_sentence` to -1" in no_teaser and "`recap`" not in no_teaser


def test_heuristic_plan_respects_brief(info, transcript):
    brief = analyze.EditBrief(highlights=2, recap=True, min_len=10, max_len=20)
    plan = analyze.plan_heuristic(info, transcript, brief)
    kinds = [c.kind for c in plan.clips]
    assert kinds.count("recap") == 1 and 1 <= kinds.count("highlight") <= 2
    clips = resolve_plan(plan, transcript, min_len=10, max_len=20, highlights=2, recap=True)
    assert clips[0].kind == "recap"
    assert all(c.duration <= 20.01 for c in clips)
    highlights = [c for c in clips if c.kind == "highlight"]
    assert all(c.duration >= 10 for c in highlights)


def test_resolve_trims_long_and_extends_short(transcript):
    long = resolve_clip(clip("highlight", [(0, 9)]), transcript, 10, 20)
    assert long.duration <= 20.01 and long.spans[0].start == 0.0
    short = resolve_clip(clip("highlight", [(4, 4)]), transcript, 10, 20)
    assert short.duration >= 10
    assert short.spans[0].start < 24.01  # still starts on the chosen line


def test_resolve_clamps_bad_indices_and_cleans_text(transcript):
    c = resolve_clip(clip("highlight", [(8, 99)]), transcript, 5, 30)
    assert c.spans[-1].end <= transcript.duration + 0.45
    assert c.hook == "Hook" and c.hashtags == ["ab", "c"]
    assert resolve_clip(clip("highlight", [(-5, -1)]), transcript, 1, 30) is not None


def test_spans_never_cut_into_neighbouring_words(transcript):
    c = resolve_clip(clip("recap", [(1, 1), (5, 5), (3, 3)]), transcript, 1, 58)
    assert [round(s.start) for s in c.spans] == [6, 18, 30]  # chronological
    for span in c.spans:
        inside = [w for w in transcript.words if span.start <= (w.start + w.end) / 2 < span.end]
        assert inside and inside[0].start >= span.start and inside[-1].end <= span.end


def test_adjacent_parts_merge(transcript):
    c = resolve_clip(clip("recap", [(1, 2), (3, 4)]), transcript, 1, 58)
    assert len(c.spans) == 1


def test_overlapping_highlights_keep_best(transcript):
    plan = VideoPlan(tldr="", summary="", key_points=[KeyPoint(sentence=0, text="x")], clips=[
        clip("highlight", [(0, 3)], 60, "weak"), clip("highlight", [(1, 4)], 90, "strong"),
        clip("highlight", [(6, 9)], 70, "other")])
    out = resolve_plan(plan, transcript, min_len=10, max_len=30, highlights=3, recap=False)
    assert [c.title for c in out] == ["strong", "other"]


class _FakeStream:
    def __init__(self, message):
        self.message = message

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get_final_message(self):
        return self.message


def _fake_client(monkeypatch, message, calls):
    import anthropic

    def stream(**kwargs):
        calls.append(kwargs)
        return _FakeStream(message)

    client = SimpleNamespace(beta=SimpleNamespace(messages=SimpleNamespace(stream=stream)))
    monkeypatch.setattr(anthropic, "Anthropic", lambda: client)


def test_claude_request_shape(monkeypatch, info, transcript):
    plan = VideoPlan(tldr="t", summary="s", key_points=[], clips=[clip("highlight", [(0, 3)])])
    message = SimpleNamespace(stop_reason="end_turn", parsed_output=plan, model="m",
                              usage=SimpleNamespace(input_tokens=1, output_tokens=1))
    calls = []
    _fake_client(monkeypatch, message, calls)
    got, used = analyze.make_plan(info, transcript, analyze.EditBrief(), analyzer="claude")
    assert got is plan and used == "claude"
    kw = calls[0]
    assert kw["model"] == "claude-opus-5-5" and kw["output_format"] is VideoPlan
    assert kw["thinking"] == {"type": "adaptive"} and kw["fallbacks"] == "default"
    assert "temperature" not in kw and "[0] 0:00" in kw["messages"][0]["content"]


def test_refusal_falls_back_in_auto_but_not_when_forced(monkeypatch, info, transcript):
    message = SimpleNamespace(stop_reason="refusal", parsed_output=None, model="m", usage=None)
    _fake_client(monkeypatch, message, [])
    _, used = analyze.make_plan(info, transcript, analyze.EditBrief(), analyzer="auto")
    assert used == "heuristic"
    with pytest.raises(analyze.AnalysisUnavailable):
        analyze.make_plan(info, transcript, analyze.EditBrief(), analyzer="claude")


def test_truncated_answer_is_an_error(monkeypatch, info, transcript):
    message = SimpleNamespace(stop_reason="max_tokens", parsed_output=None, model="m", usage=None)
    _fake_client(monkeypatch, message, [])
    with pytest.raises(RuntimeError, match="incomplete"):
        analyze.make_plan(info, transcript, analyze.EditBrief(), analyzer="auto")


def test_teaser_plays_first_and_counts_towards_length(transcript):
    c = resolve_clip(clip("highlight", [(2, 6)], teaser=5), transcript, 10, 34)
    assert c.teaser is not None and round(c.teaser.start) == 30
    assert c.playlist[0] is c.teaser and c.playlist[1:] == c.spans
    assert c.duration <= 34.01 and round(c.spans[0].start) == 12


def test_bad_teasers_are_ignored(transcript):
    assert resolve_clip(clip("highlight", [(2, 6)], teaser=2), transcript, 10, 40).teaser is None  # first line
    assert resolve_clip(clip("highlight", [(2, 6)], teaser=99), transcript, 10, 40).teaser is None
    assert resolve_clip(clip("highlight", [(2, 6)], teaser=-1), transcript, 10, 40).teaser is None


def test_heuristic_highlights_are_continuous_with_hook(info, transcript):
    plan = analyze.plan_heuristic(info, transcript, analyze.EditBrief(highlights=1, min_len=20, max_len=30))
    (h,) = [c for c in plan.clips if c.kind == "highlight"]
    assert len(h.parts) == 1 and h.hook and len(h.hook) <= 40
    assert h.teaser_sentence == -1 or h.parts[0].first_sentence < h.teaser_sentence <= h.parts[0].last_sentence
    recap = analyze.plan_heuristic(info, transcript, analyze.EditBrief(highlights=0, recap=True, max_len=40))
    assert len(recap.clips[0].parts) <= 3


def test_catchphrase_never_ends_mid_clause():
    assert analyze._catchphrase("Every time you check your phone, your brain needs twenty minutes.") == \
        "Every time you check your phone"
    assert analyze._catchphrase("That is completely wrong.") == "That is completely wrong"
    long = analyze._catchphrase("Researchers followed four thousand knowledge workers for two whole years")
    assert long.endswith("…") and len(long) <= 40
