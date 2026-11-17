"""The free local AI path, against a fake Ollama server on localhost."""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from shorts_agent import analyze, local_llm, pipeline
from shorts_agent.models import VideoPlan

PLAN = {
    "tldr": "Focus beats hours.", "summary": "A talk about deep work.",
    "key_points": [{"sentence": 4, "text": "Top performers worked less."}],
    "clips": [{"kind": "highlight", "title": "Weniger arbeiten, mehr schaffen", "hook": "4000 Leute, 2 Jahre",
               "teaser_sentence": 6, "description": "d", "hashtags": ["fokus"],
               "parts": [{"first_sentence": 3, "last_sentence": 7}], "reason": "r", "score": 80}],
}
PACK = {"clips": [{"clip": 1, "options": [{"title": "Top-Leute arbeiten WENIGER", "hook": "Die Studie mit 4000 Leuten"}],
                   "chosen": 0, "why": "klar", "description": "Was denkst du?", "hashtags": ["fokus"]}]}


class FakeOllama(BaseHTTPRequestHandler):
    models = ["qwen2.5:7b"]
    answers = []  # queue of chat replies (str); default: valid by schema
    requests = []

    def log_message(self, *a):
        pass

    def _send(self, body, status=200):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/api/tags":
            self._send({"models": [{"name": m} for m in self.models]})
        else:
            self._send({}, 404)

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        type(self).requests.append((self.path, body))
        if self.path == "/api/chat":
            if self.answers:
                content = self.answers.pop(0)
            else:
                content = json.dumps(PACK if body["format"].get("title") == "PackagingPlan" else PLAN)
            self._send({"message": {"role": "assistant", "content": content}})
        elif self.path == "/api/pull":
            lines = [{"status": "pulling", "total": 100, "completed": c} for c in (0, 50, 100)]
            type(self).models.append(body["model"])
            self._send(b"".join(json.dumps(x).encode() + b"\n" for x in lines + [{"status": "success"}]))


@pytest.fixture
def ollama(monkeypatch):
    FakeOllama.models, FakeOllama.answers, FakeOllama.requests = ["qwen2.5:7b"], [], []
    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeOllama)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setenv("OLLAMA_HOST", f"127.0.0.1:{server.server_address[1]}")
    monkeypatch.delenv("SHORTS_AGENT_LOCAL_MODEL", raising=False)
    yield FakeOllama
    server.shutdown()


def no_claude(monkeypatch):
    def fail(*a, **k):
        raise analyze.AnalysisUnavailable("no Claude credentials configured")

    monkeypatch.setattr(analyze, "plan_with_claude", fail)


def test_base_url_normalisation(monkeypatch):
    monkeypatch.setenv("OLLAMA_HOST", "0.0.0.0")
    assert local_llm.base_url() == "http://127.0.0.1:11434"
    monkeypatch.setenv("OLLAMA_HOST", "http://localhost:9999/")
    assert local_llm.base_url() == "http://localhost:9999"


def test_status(ollama, monkeypatch):
    assert local_llm.status() == "ready"
    assert local_llm.status("llama3.1") == "no-model"
    monkeypatch.setenv("OLLAMA_HOST", "127.0.0.1:9")  # nothing listens there
    monkeypatch.setattr(local_llm, "find_executable", lambda: None)
    assert local_llm.status() == "no-server"


def test_auto_uses_local_ai_without_key(ollama, monkeypatch, info, transcript):
    no_claude(monkeypatch)
    plan, used = analyze.make_plan(info, transcript, analyze.EditBrief(min_len=10, max_len=30))
    assert used == "local" and plan.clips[0].title == "Weniger arbeiten, mehr schaffen"
    path, body = ollama.requests[-1]
    assert path == "/api/chat" and body["model"] == "qwen2.5:7b" and body["stream"] is False
    assert body["format"]["title"] == "VideoPlan" and body["options"]["num_ctx"] >= 8192
    assert "[0] 0:00-0:05 Most people" in body["messages"][1]["content"]


def test_bad_local_answers_are_retried_then_fall_back(ollama, monkeypatch, info, transcript):
    no_claude(monkeypatch)
    ollama.answers[:] = ["not json", json.dumps(PLAN)]
    assert analyze.make_plan(info, transcript, analyze.EditBrief())[1] == "local"
    ollama.answers[:] = ["{}", "nope"]
    assert analyze.make_plan(info, transcript, analyze.EditBrief())[1] == "heuristic"
    ollama.answers[:] = ["{}", "nope"]
    with pytest.raises(RuntimeError):
        analyze.make_plan(info, transcript, analyze.EditBrief(), analyzer="local")


def test_missing_model_falls_back(ollama, monkeypatch, info, transcript):
    no_claude(monkeypatch)
    ollama.models[:] = ["llama3.1:latest"]
    assert analyze.make_plan(info, transcript, analyze.EditBrief())[1] == "heuristic"
    with pytest.raises(local_llm.LocalUnavailable, match="ollama pull"):
        analyze.make_plan(info, transcript, analyze.EditBrief(), analyzer="local")


def test_pull_reports_progress(ollama):
    ollama.models[:] = []
    seen = []
    local_llm.pull(on_progress=lambda s, f: seen.append(f))
    assert seen[:3] == [0.0, 0.5, 1.0] and local_llm.status() == "ready"


def test_setup_free_ai_downloads_model(ollama):
    from shorts_agent.gui_logic import setup_free_ai

    ollama.models[:] = []
    reports = []
    setup_free_ai(lambda text, frac: reports.append((text, frac)))
    assert any("100 %" in t for t, _ in reports) and local_llm.status() == "ready"


def test_pipeline_packages_titles_with_local_ai(ollama, monkeypatch, info, transcript, tmp_path):
    clips = pipeline.resolve_plan(VideoPlan.model_validate(PLAN), transcript, min_len=10, max_len=40,
                                  highlights=1, recap=False)
    cfg = pipeline.Config(source="x", out_dir=tmp_path)
    pipeline._package(cfg, info, transcript, clips, tmp_path, backend="local")
    assert clips[0].title == "Top-Leute arbeiten WENIGER" and clips[0].hook == "Die Studie mit 4000 Leuten"
    assert ollama.requests[-1][1]["format"]["title"] == "PackagingPlan"
