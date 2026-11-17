"""Decide how a landscape clip is framed in 9:16.

A single, stable face -> crop tight around it. Otherwise (several people,
slides, screen recordings, no OpenCV) -> keep the full frame over a blurred
background, which never cuts content off.
"""

from __future__ import annotations

import logging
import os
import statistics
from pathlib import Path

from .models import Span

log = logging.getLogger(__name__)


def vision_available() -> bool:
    try:
        import cv2
    except ImportError:
        return False
    # OpenCV 5 dropped the bundled Haar cascades; 4.x is required.
    return hasattr(cv2, "CascadeClassifier") and hasattr(cv2, "data")


def face_center(video: Path, span: Span, samples: int = 7) -> float | None:
    """Horizontal centre (0..1) of the speaker's face, or None if unclear."""
    # Seeking into the middle of an H.264 stream makes OpenCV's bundled ffmpeg
    # print harmless "mmco: unref short failure" lines; keep them off the console.
    os.environ.setdefault("OPENCV_FFMPEG_LOGLEVEL", "-8")
    import cv2

    detector = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
    cap = cv2.VideoCapture(str(video))
    if detector.empty() or not cap.isOpened():
        return None
    centers: list[float] = []
    try:
        for n in range(samples):
            t = span.start + span.duration * (n + 0.5) / samples
            cap.set(cv2.CAP_PROP_POS_MSEC, t * 1000)
            ok, frame = cap.read()
            if not ok:
                continue
            h, w = frame.shape[:2]
            scale = 480 / h if h > 480 else 1.0
            gray = cv2.cvtColor(cv2.resize(frame, None, fx=scale, fy=scale), cv2.COLOR_BGR2GRAY)
            min_side = max(24, int(gray.shape[0] * 0.09))
            faces = detector.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=6, minSize=(min_side, min_side))
            if len(faces) == 0:
                continue
            x, _, fw, _ = max(faces, key=lambda f: f[2] * f[3])
            centers.append((x + fw / 2) / gray.shape[1])
    finally:
        cap.release()
    if len(centers) < max(2, (samples + 1) // 2):
        return None
    if max(centers) - min(centers) > 0.22:  # speaker moves or shots alternate
        return None
    return statistics.median(centers)


def choose_framing(
    video: Path, spans: list[Span], width: int, height: int, layout: str = "auto"
) -> list[float | None]:
    """Per span: a crop centre (0..1) for a 9:16 crop, or None for blurred-fit.

    The whole clip uses one layout so the look does not jump between cuts.
    """
    if width / height <= 0.62:  # already vertical
        return [0.5] * len(spans)
    if layout == "blur":
        return [None] * len(spans)
    if not vision_available():
        if layout == "auto":
            log.info("OpenCV 4.x not installed - using blurred-background layout")
            return [None] * len(spans)
        return [0.5] * len(spans)
    centers = [face_center(video, span) for span in spans]
    if layout == "crop":
        return [c if c is not None else 0.5 for c in centers]
    if all(c is not None for c in centers):
        return centers
    return [None] * len(spans)
