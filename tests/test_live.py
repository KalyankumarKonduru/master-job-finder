"""Live preview: tailor.py records each pipeline step of a posting to its own JSONL
file, and live_preview.py serves those files to the browser. No network, no API key.
`python -m pytest tests/` from the project root.
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import live_preview  # noqa: E402
import tailor  # noqa: E402
from test_recruiter import FakeTpl, _job, _plan, make_bank  # noqa: E402


@pytest.fixture
def tpl():
    return FakeTpl()


@pytest.fixture
def live(monkeypatch, tmp_path, tpl):
    live_dir = tmp_path / "_live"
    monkeypatch.setattr(tailor, "OUT_DIR", str(tmp_path))
    monkeypatch.setattr(tailor, "LIVE_DIR", str(live_dir))
    monkeypatch.setattr(tailor, "PAGE_BUDGET",
                        {"achievements": 2, "acme": 2, "globex": 2, "initech": 2})
    monkeypatch.setattr(tailor, "PROOF_INCLUDES_ACHIEVEMENTS", False)
    monkeypatch.setattr(tailor, "ask_claude", lambda *a, **k: _plan())
    bank = make_bank(tpl, [("a1", "acme", "Built Kafka consumers so orders landed once"),
                           ("g1", "globex", "Built Redis caches so teams could reuse data")])
    return live_dir, bank


def steps(live_dir):
    (path,) = live_dir.glob("*.jsonl")
    with open(path) as f:
        return [json.loads(line) for line in f]


# ---------------------------------------------------------------- recording
@pytest.mark.integration
def test_every_step_of_a_passing_run_is_recorded_in_order(live, tpl, monkeypatch):
    live_dir, bank = live
    sent = []
    monkeypatch.setattr(tailor.requests, "post",
                        lambda *a, **k: sent.append(1) or type("R", (), {"status_code": 204})())
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://discord.invalid/hook")

    res = tailor.tailor_job(_job(), {}, [tpl], bank, {}, "m", notify=True, min_score=60)

    got = steps(live_dir)
    assert [s["step"] for s in got] == ["job", "plan", "page", "page", "page", "page", "page",
                                        "score", "done", "alert"]
    assert [s["stage"] for s in got if s["step"] == "page"] == \
        ["pick", "repair", "variety", "fill", "final"]
    job, plan, final, score = got[0], got[1], got[6], got[7]
    assert job["jd"] == _job()["description"] and job["bar"] == 60
    assert plan["required"] == ["Kafka", "Redis", "OpenShift"] and plan["base"] == "backend"
    acme = next(b for b in final["blocks"] if b["block"] == "acme")
    assert acme["bullets"][0] == {"id": "a1", "text": bank["a1"]["text"], "hits": [0]}
    assert acme["title"] == "Engineer" and acme["company"] == "Acme" and acme["proof"]
    assert score["score"] == res["score"] == 67 and score["passed"] is True
    assert got[8]["folder"] == res["folder"] and got[9]["sent"] is True and sent == [1]


@pytest.mark.integration
def test_a_run_below_the_bar_ends_skipped(live, tpl):
    live_dir, bank = live
    tailor.tailor_job(_job(), {}, [tpl], bank, {}, "m", notify=False, min_score=70)

    got = steps(live_dir)
    assert got[-1]["step"] == "skipped" and got[-1]["score"] == 67 and got[-1]["bar"] == 70


@pytest.mark.integration
def test_a_failed_claude_call_is_recorded_and_still_raises(live, tpl, monkeypatch):
    live_dir, bank = live

    def boom(*a, **k):
        raise RuntimeError("Claude API 529: overloaded")
    monkeypatch.setattr(tailor, "ask_claude", boom)

    with pytest.raises(RuntimeError):
        tailor.tailor_job(_job(), {}, [tpl], bank, {}, "m", notify=False, min_score=60)
    got = steps(live_dir)
    assert [s["step"] for s in got] == ["job", "error"] and "529" in got[1]["message"]


@pytest.mark.integration
def test_tracing_off_writes_nothing(live, tpl, monkeypatch, tmp_path):
    live_dir, bank = live
    monkeypatch.setattr(tailor, "LIVE_DIR", None)

    res = tailor.tailor_job(_job(), {}, [tpl], bank, {}, "m", notify=False, min_score=60)

    assert res["passed"] and not live_dir.exists()


@pytest.mark.integration
def test_a_broken_trace_never_breaks_tailoring(live, tpl, monkeypatch, tmp_path):
    _, bank = live
    blocker = tmp_path / "not_a_dir"
    blocker.write_text("x")                     # makedirs under a file fails
    monkeypatch.setattr(tailor, "LIVE_DIR", str(blocker / "_live"))

    res = tailor.tailor_job(_job(), {}, [tpl], bank, {}, "m", notify=False, min_score=60)

    assert res["passed"] is True


# ---------------------------------------------------------------- serving
def write_run(live_dir, name, events):
    live_dir.mkdir(parents=True, exist_ok=True)
    with open(live_dir / name, "w") as f:
        for e in events:
            f.write(json.dumps(e) + "\n")


@pytest.mark.unit
def test_runs_are_listed_newest_first_with_their_outcome(tmp_path):
    job = {"step": "job", "company": "Acme", "title": "Engineer", "ts": 1}
    write_run(tmp_path, "20261007-100000_Acme_1.jsonl",
              [job, {"step": "score", "score": 40}, {"step": "skipped", "score": 40}])
    write_run(tmp_path, "20261007-110000_Acme_2.jsonl",
              [dict(job, ts=2), {"step": "score", "score": 80}, {"step": "done"}])
    write_run(tmp_path, "20261007-120000_Acme_3.jsonl", [dict(job, ts=3)])

    runs = live_preview.list_runs(str(tmp_path))

    assert [(r["name"][:15], r["status"], r["score"]) for r in runs] == [
        ("20261007-120000", "running", None), ("20261007-110000", "done", 80),
        ("20261007-100000", "skipped", 40)]


@pytest.mark.unit
def test_a_run_is_read_from_an_offset(tmp_path):
    write_run(tmp_path, "r.jsonl", [{"step": "job"}, {"step": "plan"}, {"step": "page"}])

    assert [e["step"] for e in live_preview.read_run(str(tmp_path), "r.jsonl", 1)] == \
        ["plan", "page"]


@pytest.mark.unit
@pytest.mark.parametrize("name", ["../config.yaml", "/etc/passwd", "missing.jsonl", "x.txt"])
def test_only_run_files_inside_the_live_folder_can_be_read(tmp_path, name):
    write_run(tmp_path, "r.jsonl", [{"step": "job"}])

    assert live_preview.run_path(str(tmp_path), name) is None
