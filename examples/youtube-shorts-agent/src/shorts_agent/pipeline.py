"""End-to-end run: source -> transcript -> plan -> summary + rendered Shorts."""

from __future__ import annotations

import hashlib
import json
import logging
import re
import unicodedata
from dataclasses import asdict, dataclass, field
from pathlib import Path

from . import analyze, framing, render, subtitles, transcript as tr, youtube
from .clips import resolve_plan
from .models import Clip, Transcript, VideoInfo, VideoPlan

log = logging.getLogger(__name__)


@dataclass
class Config:
    source: str
    out_dir: Path = Path("out")
    highlights: int = 3
    mode: str = "both"  # both | highlights | recap
    min_len: float = 20.0
    max_len: float = 58.0
    language: str | None = None  # spoken language (caption track / whisper)
    output_language: str | None = None  # language of summary & titles
    transcript_file: Path | None = None
    whisper_model: str = "small"
    analyzer: str = "auto"  # auto | claude | heuristic
    model: str | None = None
    effort: str = "high"
    layout: str = "auto"  # auto | crop | blur
    max_height: int = 1080
    summary_only: bool = False
    show_hook: bool = True
    force: bool = False
    caption: subtitles.CaptionStyle = field(default_factory=subtitles.CaptionStyle)
    render: render.RenderOptions = field(default_factory=render.RenderOptions)


@dataclass
class Result:
    out_dir: Path
    summary: Path
    shorts: list[Path]
    analyzer: str


def slugify(text: str, limit: int = 48) -> str:
    text = unicodedata.normalize("NFKD", text.replace("ß", "ss")).encode("ascii", "ignore").decode()
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", text).strip("-").lower()
    return slug[:limit].rstrip("-") or "short"


# --------------------------------------------------------------------------- #
# Steps
# --------------------------------------------------------------------------- #
def _load_source(cfg: Config) -> tuple[VideoInfo, dict | None, Path | None]:
    if youtube.is_url(cfg.source):
        info, raw = youtube.fetch_info(cfg.source)
        return info, raw, None
    path = Path(cfg.source).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(f"Neither a URL nor an existing file: {cfg.source}")
    media = render.probe(path)
    digest = hashlib.sha1(str(path).encode()).hexdigest()[:6]
    info = VideoInfo(
        id=f"{slugify(path.stem, 32)}-{digest}",
        title=path.stem.replace("_", " "),
        url="",
        duration=media.duration,
    )
    return info, None, path


def _get_transcript(
    cfg: Config, info: VideoInfo, raw: dict | None, video: Path | None, work: Path
) -> Transcript:
    cache = work / "transcript.json"
    if cache.exists() and not cfg.force:
        return Transcript.from_dict(json.loads(cache.read_text(encoding="utf-8")))

    transcript: Transcript | None = None
    lang = cfg.language or info.language or "und"
    if cfg.transcript_file:
        transcript = tr.load_file(cfg.transcript_file, lang)
    elif raw is not None:
        transcript = youtube.fetch_captions(raw, cfg.language)
    elif video is not None and (sidecar := tr.find_sidecar(video)):
        log.info("Using transcript %s", sidecar.name)
        transcript = tr.load_file(sidecar, lang)

    if transcript is None or not transcript.words:
        if not tr.whisper_available():
            raise RuntimeError(
                "YouTube delivered no captions for this video (none exist, or YouTube is "
                "blocking caption downloads). Install speech recognition with "
                "`pip install faster-whisper` and run again, or pass --transcript file.srt"
            )
        media = video or youtube.download_video(info.url, work, info.id, cfg.max_height)
        log.info("Transcribing with Whisper (%s) - this can take a while", cfg.whisper_model)
        transcript = tr.from_whisper(media, cfg.whisper_model, cfg.language)

    if not transcript.words:
        raise RuntimeError("The transcript is empty (no speech detected)")
    cache.write_text(json.dumps(transcript.to_dict(), ensure_ascii=False), encoding="utf-8")
    return transcript


def _get_plan(cfg: Config, info: VideoInfo, transcript: Transcript, work: Path) -> tuple[VideoPlan, str]:
    brief = analyze.EditBrief(
        highlights=cfg.highlights if cfg.mode in ("both", "highlights") else 0,
        recap=cfg.mode in ("both", "recap"),
        min_len=cfg.min_len,
        max_len=cfg.max_len,
        output_language=cfg.output_language,
    )
    # The cache is only valid for the same request against the same transcript.
    key = hashlib.sha1(
        json.dumps([asdict(brief), cfg.analyzer, cfg.model, len(transcript.words)]).encode()
    ).hexdigest()[:10]
    cache = work / f"plan-{key}.json"
    if cache.exists() and not cfg.force:
        data = json.loads(cache.read_text(encoding="utf-8"))
        return VideoPlan.model_validate(data["plan"]), data["analyzer"]
    plan, used = analyze.make_plan(
        info, transcript, brief, analyzer=cfg.analyzer, model=cfg.model, effort=cfg.effort
    )
    if used == "claude":  # the heuristic is instant, and must not shadow a later Claude run
        cache.write_text(
            json.dumps({"analyzer": used, "plan": plan.model_dump()}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    return plan, used


_LABELS = {
    "de": ("Zusammenfassung", "Kernpunkte", "Shorts", "Kanal", "Länge", "Datei", "Titel", "Dauer", "Quelle", "Warum"),
    "en": ("Summary", "Key points", "Shorts", "Channel", "Length", "File", "Title", "Duration", "Source", "Why"),
}


def write_summary(
    path: Path, info: VideoInfo, transcript: Transcript, plan: VideoPlan,
    rendered: list[tuple[Clip, Path | None]], analyzer: str, language: str,
) -> None:
    L = _LABELS["de" if language.lower().startswith(("de", "ger")) else "en"]

    def stamp(t: float) -> str:
        label = tr.fmt_time(t)
        return f"[{label}]({info.url}&t={int(t)}s)" if "watch?v=" in info.url else label

    lines = [f"# {info.title}", ""]
    meta = [f"**{L[3]}:** {info.channel}" if info.channel else "", f"**{L[4]}:** {tr.fmt_time(info.duration or transcript.duration)}", info.url]
    lines += [" · ".join(m for m in meta if m), "", f"> **TL;DR** {plan.tldr}", "", f"## {L[0]}", "", plan.summary.strip(), "", f"## {L[1]}", ""]
    n = len(transcript.sentences)
    for kp in plan.key_points:
        t = transcript.sentences[max(0, min(n - 1, kp.sentence))].start
        lines.append(f"- {stamp(t)} {kp.text}")
    if rendered:
        lines += ["", f"## {L[2]}", "", f"| # | {L[5]} | {L[6]} | {L[7]} | Score | {L[8]} | {L[9]} |", "|---|---|---|---|---|---|---|"]
        for i, (clip, file) in enumerate(rendered, 1):
            src = ", ".join(f"{stamp(s.start)}–{tr.fmt_time(s.end)}" for s in clip.spans)
            name = f"`shorts/{file.name}`" if file else "–"
            cell = lambda s: s.replace("|", "\\|").replace("\n", " ")  # noqa: E731
            lines.append(f"| {i} | {name} | {cell(clip.title)} | {clip.duration:.0f}s | {clip.score} | {src} | {cell(clip.reason)} |")
    if analyzer != "claude":
        lines += ["", "_Offline heuristic used (no Claude credentials) - summary and clip choice are extractive._"]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _render_clip(
    cfg: Config, index: int, clip: Clip, video: Path, media: render.MediaInfo,
    transcript: Transcript, shorts_dir: Path, work: Path, info: VideoInfo,
) -> Path:
    name = f"{index:02d}-{clip.kind}-{slugify(clip.title)}"
    output = shorts_dir / f"{name}.mp4"
    words = subtitles.timeline_words(transcript, clip.spans)
    ass = subtitles.build_ass(words, clip.duration, cfg.caption, clip.hook if cfg.show_hook else None)
    centers = framing.choose_framing(video, clip.spans, media.width, media.height, cfg.layout)
    render.render(video, clip.spans, centers, media, ass, output, work, cfg.render)
    tags = " ".join(f"#{t}" for t in clip.hashtags)
    (shorts_dir / f"{name}.json").write_text(
        json.dumps(
            {
                "title": clip.title,
                "description": f"{clip.description}\n\n{tags}".strip(),
                "hashtags": clip.hashtags,
                "kind": clip.kind,
                "score": clip.score,
                "duration": round(clip.duration, 2),
                "layout": "crop" if centers[0] is not None else "blur",
                "source": {"url": info.url, "title": info.title, "channel": info.channel,
                           "spans": [[round(s.start, 2), round(s.end, 2)] for s in clip.spans]},
            },
            ensure_ascii=False, indent=2,
        ),
        encoding="utf-8",
    )
    return output


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #
def run(cfg: Config) -> Result:
    if cfg.min_len >= cfg.max_len:
        raise ValueError("--min-len must be smaller than --max-len")
    render.require_ffmpeg()

    log.info("① Loading %s", cfg.source)
    info, raw, video = _load_source(cfg)
    out_dir = cfg.out_dir / info.id
    work = out_dir / ".work"
    work.mkdir(parents=True, exist_ok=True)

    log.info("② Transcript")
    transcript = _get_transcript(cfg, info, raw, video, work)
    log.info("   %d words, %d lines, language=%s, source=%s",
             len(transcript.words), len(transcript.sentences), transcript.language, transcript.source)

    log.info("③ Analysing")
    plan, analyzer = _get_plan(cfg, info, transcript, work)
    clips = resolve_plan(
        plan, transcript, min_len=cfg.min_len, max_len=cfg.max_len,
        highlights=cfg.highlights if cfg.mode in ("both", "highlights") else 0,
        recap=cfg.mode in ("both", "recap"),
    )
    language = cfg.output_language or transcript.language
    summary_path = out_dir / "summary.md"
    write_summary(summary_path, info, transcript, plan, [(c, None) for c in clips], analyzer, language)

    shorts: list[Path] = []
    if not cfg.summary_only and clips:
        if video is None:
            log.info("④ Downloading video")
            video = youtube.download_video(info.url, work, info.id, cfg.max_height)
        media = render.probe(video)
        shorts_dir = out_dir / "shorts"
        shorts_dir.mkdir(exist_ok=True)
        for i, clip in enumerate(clips, 1):
            log.info("⑤ Rendering %d/%d  %s  (%.0fs, %s)", i, len(clips), clip.title, clip.duration, clip.kind)
            shorts.append(_render_clip(cfg, i, clip, video, media, transcript, shorts_dir, work, info))
        write_summary(summary_path, info, transcript, plan, list(zip(clips, shorts)), analyzer, language)
    elif not clips:
        log.warning("No usable clips found (video too short?)")

    return Result(out_dir=out_dir, summary=summary_path, shorts=shorts, analyzer=analyzer)
