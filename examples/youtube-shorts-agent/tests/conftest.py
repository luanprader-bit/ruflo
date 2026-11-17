import pytest

from shorts_agent.models import VideoInfo
from shorts_agent.transcript import from_cues

LINES = [
    "Most people think productivity means doing more.",
    "That is completely wrong.",
    "Welcome back to the channel.",
    "A study followed 4000 workers for two years.",
    "Top performers worked fewer hours than everyone else.",
    "They protected three hours of deep focus every morning.",
    "Every phone check costs your brain twenty minutes.",
    "Sleep matters more than any productivity app.",
    "Say no much more often.",
    "Block three hours tomorrow and work on one thing.",
]


def _ts(t: float) -> str:
    ms = round(t * 1000)
    return f"{ms // 3600000:02d}:{ms // 60000 % 60:02d}:{ms // 1000 % 60:02d},{ms % 1000:03d}"


@pytest.fixture
def srt_text() -> str:
    """Ten 5-second cues, each followed by a 1-second pause (60s total)."""
    return "\n".join(
        f"{i + 1}\n{_ts(i * 6)} --> {_ts(i * 6 + 5)}\n{line}\n" for i, line in enumerate(LINES)
    )


@pytest.fixture
def transcript(srt_text):
    return from_cues(srt_text, "en")


@pytest.fixture
def info():
    return VideoInfo(id="abc", title="The Focus Talk", channel="Chan", url="https://www.youtube.com/watch?v=abc", duration=60)
