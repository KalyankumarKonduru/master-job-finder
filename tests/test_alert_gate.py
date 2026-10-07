"""The alert gate: with min_match_score set, Discord hears about a posting only once
its resume clears the bar. Nothing here touches the network or the real database.
`python -m pytest tests/` from the project root.
"""
import os
import sqlite3
import sys

import pytest
import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import tailor  # noqa: E402
import watcher  # noqa: E402

JD = "We need Java, Spring Boot and AWS. You will build REST APIs for payments."
CFG = {"min_match_score": 65,
       "tailor": {"enabled": True, "max_per_cycle": 5, "model": "test-model"},
       "companies": [{"name": "Acme", "ats": "greenhouse"}],
       "titles": {"include": ["engineer"], "exclude": []}}


def posting(job_id, title="Software Engineer", description=None):
    job = {"job_id": job_id, "title": title, "location": "New York, NY", "workplace": "",
           "url": f"https://example.com/{job_id}", "posted": "today", "summary": ""}
    if description is not None:
        job["description"] = description
    return job


class FakeNotifier:
    """Records what would have gone to Discord as a plain, unscored alert."""

    def __init__(self, *_):
        self.added, self.failed = [], []

    def add(self, job, company):
        self.added.append((company, job["job_id"]))

    def flush(self):
        return set()


@pytest.fixture
def con(monkeypatch):
    monkeypatch.setattr(watcher, "DB_PATH", ":memory:")
    c = watcher.db_connect()
    yield c
    c.close()


@pytest.fixture
def cards(monkeypatch):
    """Stub the tailor so score_then_alert runs offline; returns the resume cards sent."""
    sent = []
    monkeypatch.setattr(tailor, "apply_config", lambda cfg: None)
    monkeypatch.setattr(tailor, "ensure_schema", lambda con: None)
    monkeypatch.setattr(tailor.resume_bank, "load_templates", lambda: [])
    monkeypatch.setattr(tailor, "build_bank", lambda templates: ({"b1": {}}, ""))
    monkeypatch.setattr(tailor, "notify_discord",
                        lambda job, plan, folder: sent.append(job["job_id"]))
    return sent


def scorer(scores):
    """A tailor_job stand-in: job_id -> recruiter score, None = no description."""
    def tailor_job(job, cfg, templates, bank, skills, model, notify=True, min_score=None):
        if not (job.get("description") or "").strip():
            return None
        score = scores[job["job_id"]]
        passed = score >= min_score
        return {"score": score, "passed": passed, "plan": {"role_summary": "x"},
                "folder": f"/out/{job['job_id']}" if passed else None}
    return tailor_job


def store(con, *jobs):
    for job in jobs:
        con.execute("INSERT INTO jobs (company, job_id, title, url, first_seen, matched, "
                    "notified, description) VALUES ('Acme', ?, ?, ?, ?, 1, 1, ?)",
                    (job["job_id"], job["title"], job["url"], watcher.STARTED_AT,
                     job.get("description", "")))
    con.commit()


# ---------------------------------------------------------------- the queue
@pytest.mark.unit
def test_queued_matches_carry_their_description(monkeypatch, con):
    """The scorer used to get the board listing without the JD, skip it, and let every
    match through unscored."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    con.execute("INSERT INTO jobs (company, job_id, matched, notified) "
                "VALUES ('Acme', 'old', 0, 1)")                  # board already seeded
    new = posting("j1")
    monkeypatch.setattr(watcher, "fetch_company", lambda src: (src, [new], None))
    monkeypatch.setattr(watcher, "fetch_description_map",
                        lambda wanted: {("Acme", "j1"): JD})
    queued = []
    monkeypatch.setattr(watcher, "score_then_alert",
                        lambda cfg, con, notifier, q, bar: queued.extend(q))
    monkeypatch.setattr(watcher, "run_tailor", lambda cfg, con, since=None: None)

    watcher.poll_once(CFG, con, watcher.TitleFilter(["engineer"], []), FakeNotifier())

    assert [(c, j["job_id"]) for c, j in queued] == [("Acme", "j1")]
    assert queued[0][1]["description"] == JD


# ---------------------------------------------------------------- scoring
@pytest.mark.unit
def test_only_a_passing_resume_reaches_discord(monkeypatch, con, cards):
    monkeypatch.setattr(tailor, "tailor_job", scorer({"pass": 80, "fail": 40}))
    jobs = [posting("pass", description=JD), posting("fail", description=JD)]
    store(con, *jobs)
    notifier = FakeNotifier()

    watcher.score_then_alert(CFG, con, notifier, [("Acme", j) for j in jobs], 65)

    assert cards == ["pass"]                  # one "Resume ready" card, which links the posting
    assert notifier.added == []               # no plain alert, scored or not
    marks = dict(con.execute("SELECT job_id, tailored FROM jobs"))
    assert marks == {"pass": "/out/pass", "fail": "skipped: score 40"}


@pytest.mark.unit
def test_matches_past_the_cap_are_not_alerted_unscored(monkeypatch, con, cards):
    """Over-cap matches used to be flagged undelivered, so the next cycle's retry sent
    them as plain alerts before anything scored them."""
    monkeypatch.setattr(tailor, "tailor_job", scorer({"a": 80, "b": 40}))
    cfg = dict(CFG, tailor=dict(CFG["tailor"], max_per_cycle=1))
    jobs = [posting("a", description=JD), posting("b", description=JD)]
    store(con, *jobs)

    watcher.score_then_alert(cfg, con, FakeNotifier(), [("Acme", j) for j in jobs], 65)
    retry = FakeNotifier()
    watcher.retry_undelivered(con, retry)

    assert retry.added == []                  # "b" waits for the tailor step's score
    waiting = tailor.db_jobs(con, watcher.UNTAILORED + " AND first_seen >= ?",
                             (watcher.STARTED_AT,))
    assert [j["job_id"] for j in waiting] == ["b"]


@pytest.mark.unit
def test_a_match_that_cannot_be_scored_still_alerts(monkeypatch, con, cards):
    """No description means no score; dropping it would lose the posting entirely."""
    monkeypatch.setattr(tailor, "tailor_job", scorer({}))
    job = posting("nojd", description="")
    store(con, job)
    notifier = FakeNotifier()

    watcher.score_then_alert(CFG, con, notifier, [("Acme", job)], 65)

    assert notifier.added == [("Acme", "nojd")]
    assert cards == []


@pytest.mark.unit
def test_an_unscoreable_match_past_the_cap_still_alerts_next_cycle(monkeypatch, con, cards):
    monkeypatch.setattr(tailor, "tailor_job", scorer({"a": 80}))
    cfg = dict(CFG, tailor=dict(CFG["tailor"], max_per_cycle=1))
    jobs = [posting("a", description=JD), posting("nojd", description="")]
    store(con, *jobs)

    watcher.score_then_alert(cfg, con, FakeNotifier(), [("Acme", j) for j in jobs], 65)
    retry = FakeNotifier()
    watcher.retry_undelivered(con, retry)

    assert retry.added == [("Acme", "nojd")]


# ---------------------------------------------------------------- the API call
class FakeResponse:
    def __init__(self, status, payload=None, headers=None):
        self.status_code, self.headers = status, headers or {}
        self._payload, self.text = payload or {}, str(payload)

    def json(self):
        return self._payload


def scripted_post(monkeypatch, *outcomes):
    """requests.post that plays back outcomes in order: a response or an exception."""
    calls, waits = [], []

    def post(url, headers=None, json=None, timeout=None):
        outcome = outcomes[len(calls)]
        calls.append(url)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome
    monkeypatch.setattr(tailor.requests, "post", post)
    monkeypatch.setattr(tailor.time, "sleep", waits.append)
    return calls, waits


@pytest.mark.unit
def test_api_call_retries_a_dropped_connection(monkeypatch):
    ssl = requests.exceptions.SSLError("SSLV3_ALERT_BAD_RECORD_MAC")
    calls, _ = scripted_post(monkeypatch, ssl, FakeResponse(200, {"ok": True}))

    assert tailor._api_call({}, "key") == {"ok": True}
    assert len(calls) == 2


@pytest.mark.unit
@pytest.mark.parametrize("status", [429, 500, 529])
def test_api_call_retries_overload_and_rate_limits(monkeypatch, status):
    calls, waits = scripted_post(monkeypatch, FakeResponse(status, headers={"retry-after": "7"}),
                                 FakeResponse(200, {"ok": True}))

    assert tailor._api_call({}, "key") == {"ok": True}
    assert waits == [7.0]                     # the server's retry-after is honoured


@pytest.mark.unit
def test_api_call_fails_fast_on_a_bad_request(monkeypatch):
    calls, _ = scripted_post(monkeypatch, FakeResponse(400, {"error": "bad"}))

    with pytest.raises(RuntimeError, match="Claude API 400"):
        tailor._api_call({}, "key")
    assert len(calls) == 1


@pytest.mark.unit
def test_api_call_gives_up_after_three_attempts(monkeypatch):
    drop = requests.exceptions.ConnectionError("reset")
    calls, _ = scripted_post(monkeypatch, drop, drop, drop)

    with pytest.raises(requests.exceptions.ConnectionError):
        tailor._api_call({}, "key")
    assert len(calls) == 3
