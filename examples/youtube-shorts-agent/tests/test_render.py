import shutil
import subprocess

import pytest

from shorts_agent import framing, pipeline
from shorts_agent.models import Span
from shorts_agent.render import MediaInfo, RenderOptions, build_command, probe

HD = MediaInfo(1920, 1080, 100.0, True)


def graph(cmd):
    return cmd[cmd.index("-filter_complex") + 1]


def test_crop_window_follows_face_and_stays_inside_frame(tmp_path):
    spans = [Span(10, 20)]
    g = graph(build_command(tmp_path / "v.mp4", spans, [0.5], HD, None, tmp_path / "o.mp4", RenderOptions()))
    assert "crop=606:1076:656:2," in g
    right = graph(build_command(tmp_path / "v.mp4", spans, [0.99], HD, None, tmp_path / "o.mp4", RenderOptions()))
    x = int(right.split("crop=")[1].split(",")[0].split(":")[2])
    assert x + 606 <= 1920


def test_multi_part_graph_and_options(tmp_path):
    spans = [Span(1, 4), Span(10, 15.5)]
    cmd = build_command(tmp_path / "v.mp4", spans, [None, None], HD, "c.ass", tmp_path / "o.mp4",
                        RenderOptions(progress_bar=True))
    g = graph(cmd)
    assert cmd.count("-i") == 2 and "concat=n=2:v=1:a=1" in g
    assert "ass=c.ass" in g and "boxblur" in g and "loudnorm" in g and "t/8.500" in g
    assert cmd[cmd.index("-t", cmd.index("-filter_complex")) + 1] == "8.500"
    plain = graph(build_command(tmp_path / "v.mp4", spans, [None, None], HD, None, tmp_path / "o.mp4",
                                RenderOptions(progress_bar=False)))
    assert "ass=" not in plain and "overlay=x=" not in plain


def test_silent_source_gets_a_silent_track(tmp_path):
    cmd = build_command(tmp_path / "v.mp4", [Span(0, 3)], [None], MediaInfo(1280, 720, 9, False), None,
                        tmp_path / "o.mp4", RenderOptions())
    assert "anullsrc=r=48000:cl=stereo" in cmd and "1:a" in cmd


def test_framing_is_consistent_per_clip(monkeypatch, tmp_path):
    spans = [Span(0, 5), Span(9, 12)]
    monkeypatch.setattr(framing, "vision_available", lambda: True)
    monkeypatch.setattr(framing, "face_center", lambda v, s: 0.3 if s.start == 0 else None)
    assert framing.choose_framing(tmp_path, spans, 1920, 1080, "auto") == [None, None]
    assert framing.choose_framing(tmp_path, spans, 1920, 1080, "crop") == [0.3, 0.5]
    assert framing.choose_framing(tmp_path, spans, 1920, 1080, "blur") == [None, None]
    assert framing.choose_framing(tmp_path, spans, 1080, 1920, "auto") == [0.5, 0.5]
    monkeypatch.setattr(framing, "face_center", lambda v, s: 0.4)
    assert framing.choose_framing(tmp_path, spans, 1920, 1080, "auto") == [0.4, 0.4]


def test_slugify():
    assert pipeline.slugify("Größe: Über 9000!") == "grosse-uber-9000"
    assert pipeline.slugify("???") == "short"


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg not installed")
@pytest.mark.parametrize("layout", ["blur", "crop"])
def test_end_to_end_local_file(tmp_path, srt_text, layout):
    """Real ffmpeg run: local video + SRT -> summary + one vertical Short."""
    video = tmp_path / "talk.mp4"
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc2=s=640x360:r=15:d=60",
         "-f", "lavfi", "-i", "sine=frequency=220:duration=60", "-c:v", "libx264", "-preset", "ultrafast",
         "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(video)], check=True)
    video.with_suffix(".srt").write_text(srt_text)
    cfg = pipeline.Config(source=str(video), out_dir=tmp_path / "out", highlights=1, mode="both",
                          min_len=8, max_len=14, language="en", analyzer="heuristic", layout=layout)
    cfg.render.preset = "ultrafast"
    result = pipeline.run(cfg)
    assert result.analyzer == "heuristic" and len(result.shorts) == 2
    assert "## Shorts" in result.summary.read_text()
    for short in result.shorts:
        media = probe(short)
        assert (media.width, media.height) == (1080, 1920) and media.has_audio
        assert 7.5 <= media.duration <= 14.3
        assert short.with_suffix(".json").exists()
    assert pipeline.run(cfg).shorts == result.shorts  # second run reuses the cache
