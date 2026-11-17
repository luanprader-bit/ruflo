from shorts_agent import analyze, packaging, pipeline
from shorts_agent.gui_logic import details_text, upload_text
from shorts_agent.models import Clip, ClipPlan, PartPlan, Span, VideoPlan
from shorts_agent.packaging import ClipPackaging, PackagingPlan, TitleOption


def make_clip(title="Draft", hook="Draft hook"):
    return Clip(kind="highlight", title=title, hook=hook, description="d", hashtags=["a"], reason="r",
                score=70, spans=[Span(18, 35)], teaser=Span(36, 41))


def test_request_contains_clip_text_teaser_and_video_context(info, transcript):
    req = packaging.build_request(info, transcript, [make_clip()], "Deutsch")
    assert "title: The Focus Talk" in req
    assert '<clip number="1" kind="highlight"' in req
    assert "<teaser>Every phone check" in req
    assert "A study followed 4000 workers" in req and "Write all text in Deutsch" in req


def test_apply_takes_chosen_option_and_keeps_alternatives():
    clips = [make_clip(), make_clip()]
    plan = PackagingPlan(clips=[
        ClipPackaging(clip=1, options=[TitleOption(title="A", hook="Hook A"), TitleOption(title="B", hook="Hook B 🚗.")],
                      chosen=1, why="clear subject", description="What happens?", hashtags=["#lambo", "cars"]),
        ClipPackaging(clip=7, options=[TitleOption(title="X", hook="Y")], chosen=0, why="", description="",
                      hashtags=[]),
    ])
    assert packaging.apply(clips, plan) == 1
    c = clips[0]
    assert (c.title, c.hook, c.title_options) == ("B", "Hook B", ["A"])
    assert c.hashtags == ["lambo", "cars"] and c.reason == "clear subject"
    assert clips[1].title == "Draft"  # unknown clip number ignored


def test_apply_rejects_unusable_options():
    clips = [make_clip()]
    plan = PackagingPlan(clips=[ClipPackaging(clip=1, options=[TitleOption(title="", hook="x")], chosen=5,
                                              why="", description="", hashtags=[])])
    assert packaging.apply(clips, plan) == 0 and clips[0].title == "Draft"


def test_heuristic_titles_use_the_video_title():
    assert analyze._video_title("1000HP LAMBORGHINI REVUELTO TERRORIZES MY NEIGHBORHOOD", 2) == \
        "1000HP Lamborghini Revuelto Terrorizes My Neighborhood (Part 2)"
    assert analyze._video_title("Mein Tag", 0, german=True) == "Mein Tag"
    assert len(analyze._video_title("x " * 80, 3, german=True)) <= 70


def test_pipeline_refines_titles_and_survives_failure(monkeypatch, tmp_path, info, transcript):
    plan = VideoPlan(tldr="t", summary="s", key_points=[], clips=[ClipPlan(
        kind="highlight", title="Draft", hook="Draft", teaser_sentence=-1, description="d", hashtags=["a"],
        parts=[PartPlan(first_sentence=2, last_sentence=6)], reason="r", score=80)])
    clips = pipeline.resolve_plan(plan, transcript, min_len=10, max_len=40, highlights=1, recap=False)
    cfg = pipeline.Config(source="x", out_dir=tmp_path)
    calls = []

    def fake_package(*a, **k):
        calls.append(a)
        return PackagingPlan(clips=[ClipPackaging(clip=1, options=[TitleOption(title="Sharp", hook="Sharp hook")],
                                                  chosen=0, why="w", description="desc?", hashtags=["x"])])

    monkeypatch.setattr(packaging, "package", fake_package)
    pipeline._package(cfg, info, transcript, clips, tmp_path)
    assert clips[0].title == "Sharp" and len(calls) == 1
    pipeline._package(cfg, info, transcript, clips, tmp_path)  # new key (title changed) but fine
    monkeypatch.setattr(packaging, "package", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("down")))
    clips[0].title = "Other"
    pipeline._package(cfg, info, transcript, clips, tmp_path)  # failure keeps drafts, no exception
    assert clips[0].title == "Other"


def test_details_and_upload_text(tmp_path):
    video = tmp_path / "01.mp4"
    video.with_suffix(".json").write_text(
        '{"title": "T", "hook": "H", "title_options": ["T2"], "description": "D\\n\\n#a #b", "why": "W"}')
    assert upload_text(video) == "T\n\nD\n\n#a #b"
    text = details_text(video)
    assert "Titel:  T" in text and "• T2" in text and "Catchphrase:  H" in text and "W" in text
    assert details_text(tmp_path / "missing.mp4") == "missing.mp4"


def test_heuristic_numbers_parts_only_when_several(info, transcript):
    one = analyze.plan_heuristic(info, transcript, analyze.EditBrief(highlights=3, min_len=45, max_len=60))
    assert [c.title for c in one.clips] == ["The Focus Talk"]
    many = analyze.plan_heuristic(info, transcript, analyze.EditBrief(highlights=3, min_len=10, max_len=15))
    titles = [c.title for c in many.clips]
    assert len(titles) > 1 and titles[0] == "The Focus Talk (Part 1)"
