"""Command line interface: `shorts-agent <youtube-url | video-file>`."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from . import __version__
from .pipeline import Config, run
from .render import RenderOptions
from .subtitles import CaptionStyle, ass_color


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="shorts-agent",
        description="Summarise a YouTube video and cut its best moments into subtitled 9:16 Shorts.",
    )
    p.add_argument("source", help="YouTube URL or path to a local video file")
    p.add_argument("-o", "--out", type=Path, default=Path("out"), help="output folder (default: ./out)")
    p.add_argument("-n", "--num", type=int, default=3, help="number of highlight Shorts (default: 3)")
    p.add_argument("--mode", choices=["both", "highlights", "recap"], default="both",
                   help="highlights = best moments, recap = whole video in <60s, both (default)")
    p.add_argument("--min-len", type=float, default=20, help="minimum Short length in seconds")
    p.add_argument("--max-len", type=float, default=58, help="maximum Short length in seconds")
    p.add_argument("--summary-only", action="store_true", help="write summary.md, render nothing")

    g = p.add_argument_group("transcript")
    g.add_argument("--lang", help="spoken language, e.g. de or en (default: auto)")
    g.add_argument("--transcript", type=Path, help="use this .srt/.vtt/.json3 instead of YouTube captions")
    g.add_argument("--whisper-model", default="small", help="faster-whisper model when no captions exist")

    g = p.add_argument_group("analysis")
    g.add_argument("--analyzer", choices=["auto", "claude", "heuristic"], default="auto",
                   help="auto = Claude if credentials are available, else offline heuristic")
    g.add_argument("--model", help="Claude model id (default: claude-opus-5-5 or $SHORTS_AGENT_MODEL)")
    g.add_argument("--effort", choices=["low", "medium", "high", "xhigh", "max"], default="high")
    g.add_argument("--output-lang", help="language for summary and titles (default: video language)")

    g = p.add_argument_group("look")
    g.add_argument("--layout", choices=["auto", "crop", "blur"], default="auto",
                   help="auto = crop to the speaker's face if there is one, else blurred background")
    g.add_argument("--font", help="caption font family (default: best installed)")
    g.add_argument("--font-size", type=int, default=88)
    g.add_argument("--highlight-color", default="#FFE500", help="colour of the spoken word (#RRGGBB)")
    g.add_argument("--no-uppercase", action="store_true", help="keep original capitalisation")
    g.add_argument("--no-hook", action="store_true", help="no headline at the top")
    g.add_argument("--no-progress-bar", action="store_true")
    g.add_argument("--quality", type=int, default=20, metavar="CRF", help="x264 CRF, lower = better (default: 20)")
    g.add_argument("--max-height", type=int, default=1080, help="max source resolution to download")

    p.add_argument("--force", action="store_true", help="ignore cached transcript/plan")
    p.add_argument("-v", "--verbose", action="store_true")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO, format="%(message)s", stream=sys.stderr
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("anthropic").setLevel(logging.WARNING)
    try:
        ass_color(args.highlight_color)
        cfg = Config(
            source=args.source,
            out_dir=args.out,
            highlights=max(0, args.num),
            mode=args.mode,
            min_len=args.min_len,
            max_len=args.max_len,
            language=args.lang,
            output_language=args.output_lang,
            transcript_file=args.transcript,
            whisper_model=args.whisper_model,
            analyzer=args.analyzer,
            model=args.model,
            effort=args.effort,
            layout=args.layout,
            max_height=args.max_height,
            summary_only=args.summary_only,
            show_hook=not args.no_hook,
            force=args.force,
            caption=CaptionStyle(
                font=args.font,
                size=args.font_size,
                uppercase=not args.no_uppercase,
                highlight=args.highlight_color,
            ),
            render=RenderOptions(
                progress_bar=not args.no_progress_bar, bar_color=args.highlight_color, crf=args.quality
            ),
        )
        result = run(cfg)
    except KeyboardInterrupt:
        print("Aborted.", file=sys.stderr)
        return 130
    except Exception as exc:  # top-level boundary: readable message, details with -v
        if args.verbose:
            raise
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    print(f"\nSummary: {result.summary}")
    for short in result.shorts:
        print(f"Short:   {short}")
    if result.analyzer != "claude":
        print("Note: offline heuristic was used. Set ANTHROPIC_API_KEY for much better clip selection.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
