"""ffmpeg rendering: cut, reframe to 1080x1920, burn captions, normalise audio."""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .models import Span

log = logging.getLogger(__name__)

OUT_W, OUT_H, FPS = 1080, 1920, 30


@dataclass
class MediaInfo:
    width: int
    height: int
    duration: float
    has_audio: bool


@dataclass
class RenderOptions:
    progress_bar: bool = True
    bar_color: str = "#FFE500"
    crf: int = 20
    preset: str = "medium"


def require_ffmpeg() -> None:
    for tool in ("ffmpeg", "ffprobe"):
        if not shutil.which(tool):
            raise RuntimeError(f"{tool} not found - install ffmpeg (https://ffmpeg.org/download.html)")


def probe(video: Path) -> MediaInfo:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-print_format", "json", "-show_streams", "-show_format", str(video)],
        capture_output=True, text=True, check=True,
    ).stdout
    data = json.loads(out)
    streams = data.get("streams", [])
    v = next((s for s in streams if s.get("codec_type") == "video"), None)
    if v is None:
        raise RuntimeError(f"{video} has no video stream")
    width, height = int(v["width"]), int(v["height"])
    rotation = abs(int(float((v.get("side_data_list") or [{}])[0].get("rotation", 0) or 0)))
    if rotation in (90, 270):
        width, height = height, width
    return MediaInfo(
        width=width,
        height=height,
        duration=float(data.get("format", {}).get("duration") or 0),
        has_audio=any(s.get("codec_type") == "audio" for s in streams),
    )


def _even(x: float) -> int:
    return max(2, int(x) // 2 * 2)


def _video_chain(i: int, media: MediaInfo, center: float | None) -> str:
    if center is not None:
        # Largest 9:16 window inside the source, centred on the speaker.
        cw = min(media.width, _even(media.height * OUT_W / OUT_H))
        ch = min(media.height, _even(cw * OUT_H / OUT_W))
        x = _even(min(max(center * media.width - cw / 2, 0), media.width - cw)) if media.width > cw else 0
        y = _even((media.height - ch) / 2) if media.height > ch else 0
        return (
            f"[{i}:v]crop={cw}:{ch}:{x}:{y},scale={OUT_W}:{OUT_H}:flags=lanczos,"
            f"setsar=1,fps={FPS},format=yuv420p[v{i}]"
        )
    # Full frame over a blurred, darkened copy of itself (blur at 1/4 size: fast).
    return (
        f"[{i}:v]fps={FPS},split=2[b{i}][f{i}];"
        f"[b{i}]scale={OUT_W // 4}:{OUT_H // 4}:force_original_aspect_ratio=increase,"
        f"crop={OUT_W // 4}:{OUT_H // 4},boxblur=10:2,scale={OUT_W}:{OUT_H},"
        f"eq=brightness=-0.14:saturation=0.85[bg{i}];"
        f"[f{i}]scale={OUT_W}:{OUT_H}:force_original_aspect_ratio=decrease:"
        f"force_divisible_by=2:flags=lanczos[fg{i}];"
        f"[bg{i}][fg{i}]overlay=(W-w)/2:(H-h)/2,setsar=1,format=yuv420p[v{i}]"
    )


def build_command(
    video: Path,
    spans: list[Span],
    centers: list[float | None],
    media: MediaInfo,
    ass_name: str | None,
    output: Path,
    options: RenderOptions,
) -> list[str]:
    """Assemble one ffmpeg invocation. Run it with cwd = the .ass file's folder."""
    total = sum(s.duration for s in spans)
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-nostdin"]
    for span in spans:
        cmd += ["-ss", f"{span.start:.3f}", "-t", f"{span.duration:.3f}", "-i", str(video)]
    if not media.has_audio:
        cmd += ["-f", "lavfi", "-t", f"{total:.3f}", "-i", "anullsrc=r=48000:cl=stereo"]

    graph: list[str] = []
    for i, (span, center) in enumerate(zip(spans, centers)):
        graph.append(_video_chain(i, media, center))
        if media.has_audio:
            fade_out = max(0.0, span.duration - 0.06)
            graph.append(
                f"[{i}:a]aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo,"
                f"afade=t=in:d=0.04,afade=t=out:st={fade_out:.3f}:d=0.06[a{i}]"
            )
    n = len(spans)
    if media.has_audio:
        graph.append("".join(f"[v{i}][a{i}]" for i in range(n)) + f"concat=n={n}:v=1:a=1[vc][ac]")
        graph.append("[ac]loudnorm=I=-14:TP=-1.5:LRA=11,aresample=48000[aout]")
        audio_map = "[aout]"
    else:
        graph.append("".join(f"[v{i}]" for i in range(n)) + f"concat=n={n}:v=1:a=0[vc]")
        audio_map = f"{n}:a"

    last = "vc"
    if ass_name:
        graph.append(f"[{last}]ass={ass_name}[vs]")
        last = "vs"
    if options.progress_bar:
        color = "0x" + options.bar_color.lstrip("#")
        graph.append(f"color=c={color}:s={OUT_W}x12:r={FPS}[bar]")
        graph.append(f"[{last}][bar]overlay=x='-w+w*t/{total:.3f}':y=H-h:shortest=1[vp]")
        last = "vp"

    cmd += [
        "-filter_complex", ";".join(graph),
        "-map", f"[{last}]", "-map", audio_map,
        "-t", f"{total:.3f}",
        "-c:v", "libx264", "-preset", options.preset, "-crf", str(options.crf),
        "-maxrate", "10M", "-bufsize", "20M", "-pix_fmt", "yuv420p", "-r", str(FPS),
        "-c:a", "aac", "-b:a", "160k", "-ar", "48000",
        "-movflags", "+faststart",
        str(output),
    ]
    return cmd


def render(
    video: Path,
    spans: list[Span],
    centers: list[float | None],
    media: MediaInfo,
    ass_text: str | None,
    output: Path,
    work_dir: Path,
    options: RenderOptions | None = None,
) -> Path:
    options = options or RenderOptions()
    output = output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    work_dir.mkdir(parents=True, exist_ok=True)
    ass_name = None
    if ass_text:
        # A plain relative filename sidesteps ffmpeg's filter-argument escaping.
        ass_name = f"{output.stem}.ass"
        (work_dir / ass_name).write_text(ass_text, encoding="utf-8")
    tmp = output.with_name(output.stem + ".part.mp4")
    cmd = build_command(video.resolve(), spans, centers, media, ass_name, tmp, options)
    log.debug("ffmpeg: %s", " ".join(cmd))
    proc = subprocess.run(cmd, cwd=work_dir, capture_output=True, text=True)
    if proc.returncode != 0 or not tmp.exists():
        tmp.unlink(missing_ok=True)
        raise RuntimeError(f"ffmpeg failed for {output.name}:\n{proc.stderr.strip()[-1500:]}")
    tmp.replace(output)
    return output
