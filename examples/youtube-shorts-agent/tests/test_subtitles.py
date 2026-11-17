import re

import pytest

from shorts_agent.models import Span
from shorts_agent.subtitles import (CaptionStyle, TimedWord, ass_color, build_ass, group_words,
                                    timeline_words)


def test_ass_color_is_bgr():
    assert ass_color("#FFE500") == "&H00E5FF&"
    with pytest.raises(ValueError):
        ass_color("yellow")


def test_timeline_maps_parts_back_to_back(transcript):
    spans = [Span(5.9, 11.2), Span(29.9, 35.2)]
    words = timeline_words(transcript, spans)
    assert words[0].text == "That" and abs(words[0].start - 0.1) < 1e-6
    second = [w for w in words if w.part == 1]
    assert second[0].text == "They" and abs(second[0].start - (5.3 + 0.1)) < 1e-6
    assert all(0 <= w.start < w.end <= 10.6 + 1e-6 for w in words)


def test_groups_respect_limits_and_sentence_ends():
    style = CaptionStyle()
    words = [TimedWord(t, i * 0.3, i * 0.3 + 0.25) for i, t in enumerate(
        "This is wrong. Donaudampfschifffahrtsgesellschaft is long, yes it really is".split())]
    groups = group_words(words, style)
    texts = [" ".join(w.text for w in g.words) for g in groups]
    assert texts[0] == "This is wrong."
    assert texts[1] == "Donaudampfschifffahrtsgesellschaft"  # oversize word stands alone
    assert all(len(g.words) <= style.max_words for g in groups)
    assert sum(len(g.words) for g in groups) == len(words)


def test_ass_highlights_each_word_once_without_overlap():
    words = [TimedWord("Hello,", 0.0, 0.4), TimedWord("{world}", 0.4, 0.9), TimedWord("again.", 2.0, 2.5)]
    ass = build_ass(words, 3.0, CaptionStyle(font="Inter"), hook="A fairly long headline for wrapping")
    events = [l for l in ass.splitlines() if l.startswith("Dialogue: 0")]
    assert len(events) == 3
    assert "{\\1c&H00E5FF&}HELLO{\\1c&HFFFFFF&} (WORLD)" in events[0]
    assert "HELLO {\\1c&H00E5FF&}(WORLD)" in events[1]
    times = [re.match(r"Dialogue: 0,([\d:.]+),([\d:.]+)", e).groups() for e in events]
    assert times[0][1] == times[1][0]  # handover inside a phrase is seamless
    assert times[1][1] <= times[2][0]  # phrases never overlap
    hook = next(l for l in ass.splitlines() if l.startswith("Dialogue: 1"))
    assert "\\N" in hook and "Style: Hook,Inter" in ass


def test_long_word_is_scaled_down():
    ass = build_ass([TimedWord("Donaudampfschifffahrtsgesellschaft", 0, 1)], 1.0, CaptionStyle(font="Inter"))
    assert re.search(r"\\fs4\d", ass)


def test_catchphrase_then_headline():
    words = [TimedWord("Hello", 0.0, 0.4)]
    ass = build_ass(words, 30.0, CaptionStyle(font="Inter"), hook="Nobody expected what happened next", catch_until=4.0)
    catch = next(l for l in ass.splitlines() if l.startswith("Dialogue: 2"))
    head = next(l for l in ass.splitlines() if l.startswith("Dialogue: 1"))
    assert ",0:00:00.00,0:00:04.00,Catch," in catch and "NOBODY" in catch and "HAPPENED NEXT" in catch and "\\N" in catch
    assert ",0:00:04.00,0:00:30.00,Hook," in head
    catch_style = next(l for l in ass.splitlines() if l.startswith("Style: Catch,"))
    hook_style = next(l for l in ass.splitlines() if l.startswith("Style: Hook,"))
    for style_line in (catch_style, hook_style):  # transparent: outline + shadow, no box
        fields = style_line.split(",")
        assert fields[2] in ("86", "58") and fields[3] == "&H00FFFFFF" and fields[15] == "1"
    plain = build_ass(words, 30.0, CaptionStyle(font="Inter"), hook="Hi")
    assert "Dialogue: 2" not in plain and ",0:00:00.00,0:00:30.00,Hook," in plain
