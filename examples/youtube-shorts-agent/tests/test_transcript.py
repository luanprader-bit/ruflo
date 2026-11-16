from shorts_agent.models import Transcript, Word
from shorts_agent.transcript import build_sentences, fmt_time, from_cues, from_json3

AUTO = {
    "events": [
        {"tStartMs": 0, "dDurationMs": 4000, "segs": [{"utf8": "[Music]"}]},
        {"tStartMs": 1000, "dDurationMs": 5000, "segs": [
            {"utf8": "hello"}, {"utf8": " world", "tOffsetMs": 400}, {"utf8": " of the", "tOffsetMs": 900}]},
        {"tStartMs": 3000, "dDurationMs": 100, "aAppend": 1, "segs": [{"utf8": "\n"}]},
        {"tStartMs": 3000, "dDurationMs": 4000, "segs": [
            {"utf8": "second"}, {"utf8": " line", "tOffsetMs": 500}]},
    ]
}
MANUAL = {
    "events": [
        {"tStartMs": 0, "dDurationMs": 2000, "segs": [{"utf8": "Hello there.\nHow are you?"}]},
        {"tStartMs": 2500, "dDurationMs": 2000, "segs": [{"utf8": "<i>Fine</i>, thanks."}]},
    ]
}


def test_json3_auto_uses_word_offsets_and_drops_noise():
    t = from_json3(AUTO, "en")
    assert t.source == "youtube-auto"
    assert [w.text for w in t.words] == ["hello", "world", "of", "the", "second", "line"]
    assert t.words[0].start == 1.0
    assert t.words[1].start == 1.4
    assert t.words[4].start == 3.0 and t.words[5].start == 3.5


def test_word_timings_are_monotonic_and_positive():
    for data in (AUTO, MANUAL):
        words = from_json3(data).words
        assert all(w.end > w.start for w in words)
        assert all(a.end <= b.start + 1e-9 for a, b in zip(words, words[1:]))


def test_json3_manual_interpolates_and_strips_tags():
    t = from_json3(MANUAL, "en")
    assert t.source == "youtube-manual"
    assert [w.text for w in t.words] == ["Hello", "there.", "How", "are", "you?", "Fine,", "thanks."]
    assert t.words[-1].end <= 4.5 + 1e-6
    assert [s.text for s in t.sentences] == ["Hello there.", "How are you?", "Fine, thanks."]


def test_srt_parsing(transcript):
    assert len(transcript.sentences) == 10
    assert transcript.sentences[3].start == 18.0
    assert abs(transcript.sentences[3].end - 23.0) < 1e-6


def test_vtt_rolling_duplicates_are_dropped():
    vtt = "WEBVTT\n\n00:00.000 --> 00:02.000\nhello world\n\n00:02.000 --> 00:04.000\nhello world\nnext line\n"
    assert [w.text for w in from_cues(vtt).words] == ["hello", "world", "next", "line"]


def test_unpunctuated_speech_is_split_into_short_lines():
    words = [Word(f"w{i}", i * 0.3, i * 0.3 + 0.25) for i in range(100)]
    sentences = build_sentences(words)
    assert len(sentences) >= 4
    assert all(s.last_word - s.first_word + 1 <= 26 for s in sentences)
    assert sum(s.last_word - s.first_word + 1 for s in sentences) == 100


def test_roundtrip_and_time_format(transcript):
    again = Transcript.from_dict(transcript.to_dict())
    assert [s.text for s in again.sentences] == [s.text for s in transcript.sentences]
    assert fmt_time(65) == "1:05" and fmt_time(3725) == "1:02:05"
