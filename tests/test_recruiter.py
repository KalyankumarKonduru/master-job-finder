"""Recruiter-first scoring: WHAT (the posting's words) and WHERE (inside a job).

Runs on a synthetic bank, so it needs no API key and none of the private master
templates. `python -m pytest tests/` from the project root.
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import tailor  # noqa: E402

JOBS = ["acme", "globex", "initech"]          # most recent first, like the templates


class FakeTpl:
    """Just enough of resume_bank.Template for the scoring and rendering paths."""

    def __init__(self, kind="backend", skills="Skills: Java | Python | Docker"):
        self.kind = kind
        self.jobs = [{"key": k, "title": "Engineer", "company": k.title()} for k in JOBS]
        self.skill_lines = [(0, skills)]
        self.rendered = None

    def bullet_node(self, idx):
        return idx

    def render(self, blocks, out_path, skill_text=None, compact=False, line_spacing=None):
        self.rendered = {"blocks": {b: list(n) for b, n in blocks.items()},
                         "skill_text": skill_text}
        with open(out_path, "w") as f:
            json.dump(self.rendered, f)


def make_bank(tpl, rows):
    """rows: [(id, block, text)] -> the bank shape build_bank() produces."""
    return {bid: {"text": text, "block": block, "tpl": tpl, "idx": i}
            for i, (bid, block, text) in enumerate(rows)}


@pytest.fixture
def tpl():
    return FakeTpl()


@pytest.fixture(autouse=True)
def isolate(monkeypatch, tmp_path):
    monkeypatch.setattr(tailor, "OUT_DIR", str(tmp_path))
    monkeypatch.setattr(tailor, "PAGE_BUDGET",
                        {"achievements": 2, "acme": 2, "globex": 2, "initech": 2})
    monkeypatch.setattr(tailor, "PROOF_INCLUDES_ACHIEVEMENTS", False)


# ---------------------------------------------------------------- matching
@pytest.mark.unit
@pytest.mark.parametrize("term,text,expected", [
    ("C", "Built cloud services", False),          # 'C' must not match inside a word
    ("C++", "Wrote C++ drivers", True),
    ("REST API", "Built 12 REST APIs", True),      # plural in the bullet
    ("CI/CD", "Ran CI/CD pipelines", True),
    ("JavaScript", "Built TypeScript screens", False),   # no implication
])
def test_literal_matching(term, text, expected):
    assert tailor._kw_present(term, tailor._norm_text(text)) is expected


# ---------------------------------------------------------------- the terms
@pytest.mark.unit
def test_normalize_plan_accepts_the_old_response_shape():
    plan = tailor.normalize_plan({"must_have_keywords": ["Kafka", "kafka", " Java "],
                                  "match_score": 70})
    assert plan["required_terms"] == ["Kafka", "Java"]
    assert plan["semantic_score"] == 70


@pytest.mark.unit
def test_validate_terms_drops_terms_the_posting_never_says():
    plan = {"required_terms": ["Spring Boot", "Golang"], "preferred_terms": ["Terraform"]}
    dropped = tailor.validate_terms(plan, "We build on Spring Boot and Go. Terraform a plus.")
    assert plan["required_terms"] == ["Spring Boot"]
    assert plan["preferred_terms"] == ["Terraform"]
    assert dropped == ["Golang"]


# ---------------------------------------------------------------- selection
@pytest.mark.unit
def test_select_blocks_rehomes_misfiles_drops_inventions_and_caps(tpl):
    bank = make_bank(tpl, [("a1", "acme", "Built Kafka consumers"),
                           ("a2", "acme", "Built Redis caches"),
                           ("a3", "acme", "Built Docker images"),
                           ("g1", "globex", "Built Java services")])
    plan = {"selected": {"acme": ["a1", "a2", "a3", "nope"], "initech": ["g1"]}}
    chosen, dropped, rehomed = tailor.select_blocks(plan, bank, tpl)
    assert chosen["acme"] == ["a1", "a2"]                # cap of 2
    assert chosen["globex"] == ["g1"]                    # re-homed, not discarded
    assert any("g1" in r for r in rehomed)
    assert any("nope" in d for d in dropped)
    assert any("a3" in d and "cap" in d for d in dropped)


@pytest.mark.unit
def test_repair_adds_a_proving_bullet_when_the_block_has_room(tpl):
    bank = make_bank(tpl, [("a1", "acme", "Built Kafka consumers"),
                           ("g1", "globex", "Ran Terraform for 4 teams")])
    chosen = {"achievements": [], "acme": ["a1"], "globex": [], "initech": []}
    changes = tailor.repair_coverage(chosen, bank, ["Kafka", "Terraform"], tpl)
    assert chosen["globex"] == ["g1"]
    assert len(changes) == 1 and "Terraform" in changes[0]


@pytest.mark.unit
def test_repair_swaps_only_a_bullet_that_proves_nothing_unique(tpl):
    bank = make_bank(tpl, [("a1", "acme", "Built Kafka consumers"),        # unique: Kafka
                           ("a2", "acme", "Mentored 3 interns"),           # proves nothing
                           ("a3", "acme", "Shipped Terraform modules")])   # proves Terraform
    chosen = {"achievements": [], "acme": ["a1", "a2"], "globex": [], "initech": []}
    tailor.repair_coverage(chosen, bank, ["Kafka", "Terraform"], tpl)
    assert chosen["acme"] == ["a1", "a3"]                # a2 swapped out, Kafka kept
    assert len(chosen["acme"]) <= tailor.PAGE_BUDGET["acme"]


@pytest.mark.unit
def test_repair_never_sacrifices_a_proven_term(tpl):
    bank = make_bank(tpl, [("a1", "acme", "Built Kafka consumers"),
                           ("a2", "acme", "Built Redis caches"),
                           ("a3", "acme", "Shipped Terraform modules")])
    chosen = {"achievements": [], "acme": ["a1", "a2"], "globex": [], "initech": []}
    tailor.repair_coverage(chosen, bank, ["Kafka", "Redis", "Terraform"], tpl)
    assert chosen["acme"] == ["a1", "a2"]                # no safe victim, so no swap


@pytest.mark.unit
def test_repair_leaves_a_bank_gap_alone(tpl):
    bank = make_bank(tpl, [("a1", "acme", "Built Kafka consumers")])
    chosen = {"achievements": [], "acme": ["a1"], "globex": [], "initech": []}
    assert tailor.repair_coverage(chosen, bank, ["Kafka", "OpenShift"], tpl) == []


@pytest.mark.unit
def test_order_puts_the_most_central_term_first(tpl):
    bank = make_bank(tpl, [("a1", "acme", "Built Redis caches"),
                           ("a2", "acme", "Built Kafka consumers")])
    ordered = tailor.order_blocks({"acme": ["a1", "a2"]}, bank, ["Kafka", "Redis"])
    assert ordered["acme"] == ["a2", "a1"]               # Kafka is the hamburger


# ---------------------------------------------------------------- the score
@pytest.mark.unit
def test_only_bullets_inside_a_job_count_as_proof(tpl, monkeypatch):
    bank = make_bank(tpl, [("k1", "achievements", "Built Kafka consumers"),
                           ("a1", "acme", "Built Redis caches")])
    chosen = {"achievements": ["k1"], "acme": ["a1"], "globex": [], "initech": []}
    plan = {"required_terms": ["Kafka", "Redis"], "preferred_terms": []}
    rs = tailor.recruiter_score(plan, chosen, bank, tpl)
    assert rs["score"] == 50
    assert rs["claims_achievements"] == ["Kafka"]

    monkeypatch.setattr(tailor, "PROOF_INCLUDES_ACHIEVEMENTS", True)
    assert tailor.recruiter_score(plan, chosen, bank, tpl)["score"] == 100


@pytest.mark.unit
def test_skills_line_is_a_claim_and_gaps_are_split(tpl):
    bank = make_bank(tpl, [("a1", "acme", "Built Kafka consumers"),
                           ("g1", "globex", "Built Redis caches")])
    chosen = {"achievements": [], "acme": ["a1"], "globex": [], "initech": []}
    plan = {"required_terms": ["Kafka", "Redis", "Java", "OpenShift"],
            "preferred_terms": []}
    rs = tailor.recruiter_score(plan, chosen, bank, tpl)
    assert rs["proven"] == {"Kafka": ["Acme", 1]}
    assert rs["claims_skills"] == ["Java"]               # in SKILLS, no bullet
    assert rs["page_gaps"] == ["Redis"]                  # in the bank, not on the page
    assert rs["bank_gaps"] == ["Java", "OpenShift"]      # no bullet anywhere says them
    assert rs["padding"] == ["Kafka"]                    # SKILLS only repeats what is proven


# ---------------------------------------------------------------- the gate
def _job():
    return {"company": "TestCo", "title": "Backend Engineer", "job_id": "42",
            "url": "https://example.com/42",
            "description": "Required: Kafka, Redis and OpenShift. 5+ years of Java."}


def _plan():
    return {"base_template": "backend", "required_terms": ["Kafka", "Redis", "OpenShift"],
            "meta_requirements": ["5+ years of Java"], "semantic_score": 80,
            "selected": {"acme": ["a1"], "globex": ["g1"]}, "fit_assessment": "ok"}


@pytest.fixture
def wired(tpl, monkeypatch):
    bank = make_bank(tpl, [("a1", "acme", "Built Kafka consumers"),
                           ("g1", "globex", "Built Redis caches so teams could reuse data")])
    sent = []
    monkeypatch.setattr(tailor, "ask_claude", lambda *a, **k: _plan())
    monkeypatch.setattr(tailor, "notify_discord", lambda job, plan, folder: sent.append(folder))
    return bank, sent


def _scored(tmp_path):
    with open(tmp_path / "_gaps" / "scored.jsonl") as f:
        return [json.loads(line) for line in f]


@pytest.mark.integration
def test_below_the_bar_nothing_is_written_and_nothing_is_sent(tpl, wired, tmp_path):
    bank, sent = wired
    res = tailor.tailor_job(_job(), {}, [tpl], bank, {}, "m", notify=True, min_score=70)
    assert res["passed"] is False and res["score"] == 67     # 2 of 3 proven
    assert res["folder"] is None and sent == []
    assert tpl.rendered is None
    assert _scored(tmp_path)[-1]["passed"] is False


@pytest.mark.integration
def test_above_the_bar_one_resume_and_one_alert(tpl, wired, tmp_path):
    bank, sent = wired
    res = tailor.tailor_job(_job(), {}, [tpl], bank, {}, "m", notify=True, min_score=60)
    assert res["passed"] is True and res["score"] == 67
    assert sent == [res["folder"]]
    for name in ("match_report.md", "plan.json", "job_description.txt"):
        assert os.path.exists(os.path.join(res["folder"], name))
    report = open(os.path.join(res["folder"], "match_report.md")).read()
    assert "Recruiter score: 67/100" in report and "OpenShift" in report
    assert "5+ years of Java" in report                      # meta requirement, by eye
    assert _scored(tmp_path)[-1]["passed"] is True


# ---------------------------------------------------------------- the audit
@pytest.mark.unit
def test_audit_flags_missing_why_bare_percentages_and_vague_verbs(tpl):
    bank = make_bank(tpl, [
        ("a1", "acme", "Built REST APIs so customers could book their own slots."),
        ("a2", "acme", "Cut API latency by 28% with Redis caching."),
        ("a3", "acme", "Led key initiatives across the platform."),
    ])
    text, totals = tailor.bank_audit(bank)
    assert totals == {"bullets": 3, "no_why": 2, "pct_no_scale": 1, "vague": 1,
                      "achievements": 0}
    assert "Cut API latency by 28%" in text


# ---------------------------------------------------------------- variety
def _verbs(chosen, bank):
    out = {}
    for ids in chosen.values():
        for b in ids:
            v = tailor.opening_verb(bank[b]["text"])
            out[v] = out.get(v, 0) + 1
    return out


@pytest.mark.unit
@pytest.mark.parametrize("text,verb", [("Built 10 REST APIs.", "built"),
                                       ("Load-tested the service with k6.", "load-tested"),
                                       ("  Performance-tuned 5 queries.", "performance-tuned")])
def test_opening_verb(text, verb):
    assert tailor.opening_verb(text) == verb


@pytest.mark.unit
def test_no_opening_verb_starts_more_than_two_bullets(tpl, monkeypatch):
    monkeypatch.setattr(tailor, "PAGE_BUDGET", {"achievements": 2, "acme": 4, "globex": 2, "initech": 2})
    bank = make_bank(tpl, [("a1", "acme", "Built Kafka consumers"),
                           ("a2", "acme", "Built Redis caches"),
                           ("a3", "acme", "Built Docker images"),
                           ("a4", "acme", "Shipped Docker images to 3 teams")])
    chosen = {"achievements": [], "acme": ["a1", "a2", "a3"], "globex": [], "initech": []}
    final, changes = tailor.enforce_variety(chosen, bank, ["Kafka", "Redis", "Docker"], tpl)
    assert _verbs(final, bank)["built"] == 2
    assert final["acme"] == ["a1", "a2", "a4"]            # a3 swapped for a different verb
    assert "a4" in changes[0] and "Built" in changes[0]
    assert chosen["acme"] == ["a1", "a2", "a3"]           # input left untouched


@pytest.mark.unit
def test_same_claim_with_new_numbers_prints_once(tpl):
    bank = make_bank(tpl, [
        ("a1", "acme", "Added circuit breakers to 3 Spring Boot services so one failure never cascaded."),
        ("g1", "globex", "Added circuit breakers to 6 Spring Boot services so one failure never cascaded."),
        ("g2", "globex", "Tuned Kafka throughput to 500 messages/second.")])
    chosen = {"achievements": [], "acme": ["a1"], "globex": ["g1"], "initech": []}
    final, changes = tailor.enforce_variety(chosen, bank, ["Spring Boot", "Kafka"], tpl)
    assert final["acme"] == ["a1"]                        # most recent job keeps the claim
    assert final["globex"] == ["g2"]                      # the repeat gives way
    assert "repeats `a1`" in changes[0]


@pytest.mark.unit
def test_achievement_that_repeats_a_job_bullet_gives_way(tpl):
    text = "Split a monolith into 5 Spring Boot services so teams deployed independently."
    bank = make_bank(tpl, [("k1", "achievements", text), ("a1", "acme", text),
                           ("k2", "achievements", "Cut release time from 45 to 15 minutes.")])
    chosen = {"achievements": ["k1"], "acme": ["a1"], "globex": [], "initech": []}
    final, _ = tailor.enforce_variety(chosen, bank, ["Spring Boot"], tpl)
    assert final["acme"] == ["a1"]                        # the job copy is the proof
    assert final["achievements"] == ["k2"]


@pytest.mark.unit
def test_sole_proof_stays_even_if_it_breaks_the_verb_cap(tpl, monkeypatch):
    monkeypatch.setattr(tailor, "PAGE_BUDGET", {"achievements": 2, "acme": 3, "globex": 2, "initech": 2})
    bank = make_bank(tpl, [("a1", "acme", "Built Kafka consumers"),
                           ("a2", "acme", "Built Redis caches"),
                           ("a3", "acme", "Built Terraform modules")])   # only Terraform proof
    chosen = {"achievements": [], "acme": ["a1", "a2", "a3"], "globex": [], "initech": []}
    final, changes = tailor.enforce_variety(chosen, bank, ["Kafka", "Redis", "Terraform"], tpl)
    assert final["acme"] == ["a1", "a2", "a3"]
    assert "only proof of Terraform" in changes[0]


@pytest.mark.unit
def test_two_bullets_in_a_row_never_share_an_opening_verb(tpl):
    bank = make_bank(tpl, [("a1", "acme", "Built Kafka consumers"),
                           ("a2", "acme", "Built Redis caches"),
                           ("a3", "acme", "Cut API latency 28%")])
    out = tailor.separate_repeats({"acme": ["a1", "a2", "a3"]}, bank)
    assert out["acme"] == ["a1", "a3", "a2"]              # first bullet never moves


@pytest.mark.unit
def test_repeat_at_the_end_of_a_block_moves_up(tpl):
    bank = make_bank(tpl, [("k1", "achievements", "Provisioned AWS for 6 services"),
                           ("k2", "achievements", "Cut release time to 15 minutes"),
                           ("k3", "achievements", "Kept a service at 99.9% uptime"),
                           ("k4", "achievements", "Kept 8 services monitored")])
    out = tailor.separate_repeats({"achievements": ["k1", "k2", "k3", "k4"]}, bank)
    ids = out["achievements"]
    verbs = [tailor.opening_verb(bank[b]["text"]) for b in ids]
    assert ids[0] == "k1"                                  # first bullet never moves
    assert all(a != b for a, b in zip(verbs, verbs[1:]))   # no back-to-back repeat


# ---------------------------------------------------------------- clauses and page fill
@pytest.mark.unit
def test_clauses_move_to_check_by_eye():
    plan = {"required_terms": ["Java", "Software Development Life Cycle",
                               "responsible AI use in engineering workflows"],
            "preferred_terms": ["experience with event-driven architecture at scale"],
            "meta_requirements": ["2+ years of relevant experience"]}
    moved = tailor.split_long_terms(plan)
    assert plan["required_terms"] == ["Java", "Software Development Life Cycle"]   # 4 words stays
    assert plan["preferred_terms"] == []
    assert moved == ["responsible AI use in engineering workflows",
                     "experience with event-driven architecture at scale"]
    assert plan["meta_requirements"][0] == "2+ years of relevant experience"
    assert "responsible AI use in engineering workflows" in plan["meta_requirements"]


@pytest.mark.unit
def test_page_fill_tops_up_with_relevant_bullets_only(tpl, monkeypatch):
    # room for 4, so the iOS bullet is reached and has to be refused on its own merits
    monkeypatch.setattr(tailor, "PAGE_BUDGET", {"achievements": 0, "acme": 4, "globex": 0, "initech": 0})
    ios = FakeTpl(kind="frontend")
    bank = {**make_bank(tpl, [("a1", "acme", "Built Kafka consumers"),
                              ("a2", "acme", "Tuned Redis caches so pages loaded faster"),
                              ("a4", "acme", "Wrote runbooks for 6 services")]),
            **make_bank(ios, [("i1", "acme", "Shipped 4 Swift screens for an iOS app")])}
    plan = {"required_terms": ["Kafka", "Redis"], "preferred_terms": []}
    chosen = {"achievements": [], "acme": ["a1"], "globex": [], "initech": []}
    final, added = tailor.fill_page(chosen, bank, plan, tpl)
    assert final["acme"] == ["a1", "a2", "a4"]     # Redis first, then a same-template filler
    assert "i1" not in final["acme"]               # unrelated off-template work never pads
    assert len(added) == 2 and "Redis" in added[0]
    assert chosen["acme"] == ["a1"]                # input untouched


@pytest.mark.unit
def test_page_fill_never_repeats_a_claim(tpl, monkeypatch):
    monkeypatch.setattr(tailor, "PAGE_BUDGET", {"achievements": 0, "acme": 3, "globex": 0, "initech": 0})
    bank = make_bank(tpl, [
        ("a1", "acme", "Built Kafka consumers so 3 teams got data in real time."),
        ("a2", "acme", "Built Kafka consumers so 8 teams got data in real time."),  # same claim
        ("a3", "acme", "Tuned Redis caches so pages loaded faster.")])
    plan = {"required_terms": ["Kafka", "Redis"], "preferred_terms": []}
    final, _ = tailor.fill_page({"achievements": [], "acme": ["a1"], "globex": [], "initech": []},
                                bank, plan, tpl)
    assert final["acme"] == ["a1", "a3"]          # verb cap allows a2; only the repeat rule stops it


@pytest.mark.unit
def test_page_fill_obeys_the_variety_rules(tpl, monkeypatch):
    monkeypatch.setattr(tailor, "PAGE_BUDGET", {"achievements": 0, "acme": 4, "globex": 0, "initech": 0})
    bank = make_bank(tpl, [("a1", "acme", "Built Kafka consumers"),
                           ("a2", "acme", "Built Kafka producers"),
                           ("a3", "acme", "Built Redis caches"),                     # 3rd 'Built'
                           ("a4", "acme", "Built Kafka consumers for 3 more teams"),  # repeat claim
                           ("a5", "acme", "Cached Redis lookups so pages loaded faster")])
    plan = {"required_terms": ["Kafka", "Redis"], "preferred_terms": []}
    chosen = {"achievements": [], "acme": ["a1", "a2"], "globex": [], "initech": []}
    final, _ = tailor.fill_page(chosen, bank, plan, tpl)
    assert final["acme"] == ["a1", "a2", "a5"]


@pytest.mark.unit
def test_sole_proof_makes_room_instead_of_breaking_the_cap(tpl):
    bank = make_bank(tpl, [
        ("a1", "acme", "Added circuit breakers to Kafka consumers"),            # proves Kafka
        ("g1", "globex", "Added quality gates to 5 pipelines for Redis"),       # Redis, also proven by g2
        ("g2", "globex", "Tuned Redis caches so pages loaded faster"),
        ("g3", "globex", "Shipped Redis-backed sessions so logins stayed fast"),
        ("i1", "initech", "Added application resiliency via retries and fallbacks")])  # sole proof
    chosen = {"achievements": [], "acme": ["a1"], "globex": ["g1", "g2"], "initech": ["i1"]}
    final, changes = tailor.enforce_variety(
        chosen, bank, ["Kafka", "Redis", "application resiliency"], tpl)
    verbs = [tailor.opening_verb(bank[b]["text"]) for ids in final.values() for b in ids]
    assert verbs.count("added") == 2                      # the cap holds...
    assert "i1" in final["initech"] and "a1" in final["acme"]   # ...and every proof survives
    assert "g1" not in final["globex"]                    # the one with nothing unique stepped aside
    assert any("make room" in c for c in changes)
