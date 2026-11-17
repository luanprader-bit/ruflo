
from shorts_agent import gui_logic as g
from shorts_agent.youtube import RateLimited, _download_text, fetch_captions


def test_cli_args_follow_settings(tmp_path):
    s = g.Settings(highlights=5, mode="Nur Highlights", language="Deutsch", layout="Auf Gesicht zuschneiden",
                   max_len=45, color="#00E0FF", uppercase=False, hook=False)
    args = g.build_cli_args("https://youtu.be/x", s, tmp_path, has_key=True)
    assert args[:3] == ["-m", "shorts_agent.cli", "https://youtu.be/x"]
    joined = " ".join(args)
    for part in ("-n 5", "--mode highlights", "--layout crop", "--max-len 45", "--min-len 30",
                 "--highlight-color #00E0FF", "--output-lang Deutsch", "--no-uppercase", "--no-hook"):
        assert part in joined
    assert "--analyzer" not in args


def test_cli_args_without_key_use_offline_analyzer(tmp_path):
    args = g.build_cli_args("v.mp4", g.Settings(max_len=20, language="Wie im Video"), tmp_path)
    joined = " ".join(args)
    assert "--analyzer" not in joined and "--output-lang" not in joined  # CLI auto: local AI or simple
    assert "--max-len 20" in joined and "--min-len 8" in joined  # min stays below max


def test_parse_line_maps_progress():
    kinds = lambda line: [(e.kind, e.progress) for e in g.parse_line(line)]  # noqa: E731
    assert ("status", 5) in kinds("① Loading https://x")
    assert ("status", 45) in kinds("③ Analysing")
    ev = g.parse_line("⑤ Rendering 2/4  Great title  (40s, highlight)")
    assert ev[-1].text == "Schneide Short 2 von 4: Great title" and ev[-1].progress == 60 + 38 / 4
    assert [e.kind for e in g.parse_line("[download]  42.0% of 10MiB")] == ["status"]  # no log spam
    assert g.parse_line("Short:   C:\\out\\a b.mp4")[-1].text == "C:\\out\\a b.mp4"
    assert g.parse_line("Summary: /x/summary.md")[-1].progress == 100
    assert g.parse_line("Error: boom")[-1].kind == "error"
    assert g.parse_line("   ") == []
    assert g.parse_line("   2:00 / 10:31 transcribed")[-1].text == "Spracherkennung: 2:00 von 10:31 …"


def test_friendly_errors():
    assert "winget install Gyan.FFmpeg" in g.friendly_error("ffmpeg not found - install ffmpeg")
    assert "Spracherkennung" in g.friendly_error("YouTube delivered no captions for this video")
    assert g.friendly_error("weird") == "weird"


def test_settings_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("APPDATA", str(tmp_path))
    g.Settings(highlights=7, color="#123456").save()
    loaded = g.Settings.load()
    assert loaded.highlights == 7 and loaded.color == "#123456"
    (tmp_path / "shorts-agent" / "settings.json").write_text("{broken")
    assert g.Settings.load() == g.Settings()


class _Resp:
    def __init__(self, body):
        self.body = body

    def read(self):
        return self.body


class _FakeYDL:
    calls = 0
    fail = 0

    def __init__(self, *a, **k):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *e):
        return False

    def urlopen(self, url):
        type(self).calls += 1
        if type(self).calls <= type(self).fail:
            raise RuntimeError("HTTP Error 429: Too Many Requests")
        return _Resp(b'{"events": [{"tStartMs": 0, "dDurationMs": 1000, "segs": [{"utf8": "hi there."}]}]}')


def _patch(monkeypatch, fail):
    from shorts_agent import youtube

    _FakeYDL.calls, _FakeYDL.fail = 0, fail
    monkeypatch.setattr(youtube, "_ydl", lambda **k: _FakeYDL())
    monkeypatch.setattr(youtube.time, "sleep", lambda s: None)


def test_caption_download_retries_429(monkeypatch):
    _patch(monkeypatch, fail=2)
    assert "hi there" in _download_text("u")
    assert _FakeYDL.calls == 3


def test_persistent_429_gives_up_quietly(monkeypatch):
    _patch(monkeypatch, fail=99)
    try:
        _download_text("u")
    except RateLimited:
        pass
    else:
        raise AssertionError("expected RateLimited")
    raw = {"language": "en", "automatic_captions": {"en": [{"ext": "json3", "url": "a"}]},
           "subtitles": {"en": [{"ext": "json3", "url": "b"}]}}
    _FakeYDL.calls = 0
    assert fetch_captions(raw) is None
    assert _FakeYDL.calls == 3  # did not also hammer the manual track


def test_short_label(tmp_path):
    video = tmp_path / "01-recap-x.mp4"
    assert g.short_label(video) == "01-recap-x"
    video.with_suffix(".json").write_text('{"title": "Fokus", "duration": 41.6, "kind": "recap"}')
    assert g.short_label(video) == "Fokus   (42 s, Zusammenfassung)"


def test_key_problem():
    assert g.key_problem("") is None
    assert "sk-ant-" in g.key_problem(".")
    assert "unvollständig" in g.key_problem("sk-ant-api03-abc")
    assert g.key_problem("  sk-ant-api03-" + "x" * 80 + "  ") is None


def test_key_rejection_and_network_messages():
    ev = g.parse_line("Claude unavailable (Claude rejected the API key (invalid or revoked)). Falling back")
    assert ev[-1].kind == "keyerror"
    assert "nochmal" in g.friendly_error(
        "Video download failed: no internet connection or YouTube unreachable ([Errno 11001] getaddrinfo failed).")
    assert "API-Key wurde abgelehnt" in g.friendly_error("Claude rejected the API key")


class _DLError(Exception):
    def __init__(self, inner):
        super().__init__("ERROR: ")
        self.exc_info = (type(inner), inner, None)


def test_download_retries_network_drops(monkeypatch, tmp_path):
    from shorts_agent import youtube

    calls = []

    class YDL:
        def __init__(self, **opts):
            self.opts = opts

        def __enter__(self):
            return self

        def __exit__(self, *e):
            return False

        def download(self, urls):
            calls.append(urls)
            if len(calls) < 3:
                raise _DLError(OSError("[Errno 11001] getaddrinfo failed"))
            (tmp_path / "vid.mp4").write_bytes(b"x")

    monkeypatch.setattr(youtube, "_ydl", lambda **o: YDL(**o))
    monkeypatch.setattr(youtube.time, "sleep", lambda s: None)
    assert youtube.download_video("u", tmp_path, "vid").name == "vid.mp4" and len(calls) == 3


def test_download_gives_clear_error(monkeypatch, tmp_path):
    from shorts_agent import youtube

    class YDL:
        def __init__(self, **o):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *e):
            return False

        def download(self, urls):
            raise _DLError(OSError("[Errno 11001] getaddrinfo failed"))

    monkeypatch.setattr(youtube, "_ydl", lambda **o: YDL())
    monkeypatch.setattr(youtube.time, "sleep", lambda s: None)
    try:
        youtube.download_video("u", tmp_path, "vid")
    except RuntimeError as exc:
        assert "no internet connection" in str(exc) and "getaddrinfo" in str(exc)
    else:
        raise AssertionError("expected RuntimeError")
