#!/usr/bin/env python3
"""
tailor - turns a job posting into a tailored one-page resume built ONLY from
bullets that already exist in your master templates.

  python tailor.py --run                  # tailor every new matched job (watcher calls this)
  python tailor.py --job 42               # tailor one job by # from `tailor.py --list`
  python tailor.py --url <job url>        # tailor any posting, even one not in the DB
  python tailor.py --jd-file jd.txt       # tailor from a pasted JD saved to a file
  python tailor.py --list                 # matched jobs and their tailor status
  python tailor.py --fit                  # one-page budget and the geometry behind it
  python tailor.py --audit                # bullets missing a WHY, real scale, or a concrete verb
  python tailor.py --scores               # recruiter vs semantic scores, to tune the bar
  python tailor.py --away                 # away days, and a test write to iCloud Drive

Output per job, under ./applications/<Company>__<Title>__<id>/ :
  <NAME>_<Company>.docx   the tailored resume
  match_report.md         recruiter score, proof per term, gaps, and the bullets used
  job_description.txt     the JD it was built from
  plan.json               Claude's full answer plus the scoring, for re-scoring later
On a date listed in away.yaml, inside its hours, the .docx is also copied to
iCloud Drive/<folder>/<Company>__<Title>__<id>/ so it can be attached from a phone.

How the resume is produced:
  Claude reads the JD, copies its required terms VERBATIM, and CHOOSES bullet IDs
  from your bank. It never writes bullet text. The code then scores the page the way
  a recruiter reads it: a term counts only when its exact words appear in a bullet
  under a job - never from the SKILLS line or a summary. Coverage is repaired inside
  the page budget and the most-asked-for terms are printed first. That recruiter
  score gates the alert; Claude's own semantic score is kept for context only.
  The .docx is your own template with unselected paragraphs removed, so formatting
  and wording are yours.

Env: ANTHROPIC_API_KEY (required), DISCORD_WEBHOOK_URL / TELEGRAM_* (optional)
"""
import argparse
import glob
import json
import logging
import os
import re
import shutil
import sqlite3
import statistics
import sys
import textwrap
import time
from datetime import datetime

import requests
import yaml

import resume_bank
import watcher

log = logging.getLogger("tailor")

HERE = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.environ.get("JOBWATCH_APPLICATIONS") or os.path.join(HERE, "applications")
LIVE_DIR = None             # <OUT_DIR>/_live once configured: one step file per posting
DEFAULT_MODEL = "claude-sonnet-5"
MAX_JD_CHARS = 18000
MAX_TOKENS = 16000

# Bullets kept per block. 19 is the most that fits on ONE page at 1.15 line spacing
# across all four templates (cloud_devops has the longest bullets and sets the limit).
# The defaults below sum to 26, so they intentionally allow a second page; drop to
# 4/6/5/4 to force one. Override in config.yaml under `tailor: page_budget:` - and
# re-check the rendered page count whenever you raise these or change line_spacing.
# Caps are enforced in select_blocks(), and repair_coverage() works inside them, so
# the recruiter score is always computed on exactly what the page will print.
PAGE_BUDGET = {}            # resolved per run - see resolve_budget()
ACHIEVEMENTS_CAP = 5        # the one fixed block
JOB_BULLETS = "auto"        # "auto" = fit the page; an int pins the first job at n
PAGE_LINES = None           # override the estimated lines per page
EXPLICIT_BLOCKS = set()     # blocks pinned by name in config.yaml, never auto-fitted
PROOF_INCLUDES_ACHIEVEMENTS = False  # Key Achievements names no employer, so no WHERE
MAX_VERB_USES = 2           # how many bullets one opening verb may start, page-wide
DEFAULT_JOB_BUDGET = 5
COMPACT = True
LINE_SPACING = 1.15


def apply_config(cfg):
    """Let config.yaml override the budget, model and compact layout."""
    global PAGE_BUDGET, DEFAULT_JOB_BUDGET, COMPACT, OUT_DIR, MAX_TOKENS, LINE_SPACING
    global ACHIEVEMENTS_CAP, JOB_BULLETS, PAGE_LINES, EXPLICIT_BLOCKS, PROOF_INCLUDES_ACHIEVEMENTS, MAX_VERB_USES
    global LIVE_DIR
    t = (cfg or {}).get("tailor") or {}
    pb = {str(k).lower(): v for k, v in (t.get("page_budget") or {}).items()}
    ACHIEVEMENTS_CAP = int(pb.pop("achievements", ACHIEVEMENTS_CAP))
    JOB_BULLETS = pb.pop("job_bullets", JOB_BULLETS)
    pl = pb.pop("page_lines", None)
    PAGE_LINES = int(pl) if pl else None
    EXPLICIT_BLOCKS = set(pb)          # a block named outright still wins
    PROOF_INCLUDES_ACHIEVEMENTS = bool(t.get("proof_includes_achievements",
                                             PROOF_INCLUDES_ACHIEVEMENTS))
    MAX_VERB_USES = max(1, int(t.get("max_verb_uses", MAX_VERB_USES)))
    PAGE_BUDGET.update({k: int(v) for k, v in pb.items()})
    DEFAULT_JOB_BUDGET = int(t.get("default_job_budget", DEFAULT_JOB_BUDGET))
    COMPACT = bool(t.get("compact", COMPACT))
    LINE_SPACING = float(t.get("line_spacing", LINE_SPACING))
    MAX_TOKENS = int(t.get("max_tokens", MAX_TOKENS))
    resume_bank.set_template_dir(t.get("templates_dir"),
                                 include=t.get("templates_include") or [],
                                 exclude=t.get("templates_exclude") or [])
    if t.get("output_dir"):
        OUT_DIR = t["output_dir"] if os.path.isabs(t["output_dir"]) else os.path.join(HERE, t["output_dir"])
    LIVE_DIR = os.path.join(OUT_DIR, "_live") if t.get("live_preview", True) else None
    return t


# ---------------------------------------------------------------- bullet bank
def build_bank(templates):
    """All bullets from all templates, with stable ids: '<kind>:<paragraph idx>'.

    Also resolves the page budget, because every caller loads templates and builds the
    bank together, and bank_for_prompt() must show Claude the caps it has to respect.
    """
    resolve_budget(templates)
    bank, skills = {}, {}
    for t in templates:
        for idx in t.achievements:
            bank[f"{t.kind}:{idx}"] = {"tpl": t, "idx": idx, "block": "achievements",
                                       "text": t.bullets[idx]["text"]}
        for j in t.jobs:
            for idx in j["bullets"]:
                bank[f"{t.kind}:{idx}"] = {"tpl": t, "idx": idx, "block": j["key"],
                                           "text": t.bullets[idx]["text"]}
        skills[t.kind] = [txt for _, txt in t.skill_lines]
    return bank, skills


def bank_for_prompt(bank):
    """Compact listing grouped by block so the model sees the section structure."""
    blocks = {}
    for bid, b in bank.items():
        blocks.setdefault(b["block"], []).append((bid, b["tpl"].kind, b["text"]))
    out = []
    for block, rows in blocks.items():
        cap = PAGE_BUDGET.get(block, DEFAULT_JOB_BUDGET)
        out.append(f"\n### BLOCK: {block}  (choose at most {cap})")
        for bid, kind, text in rows:
            out.append(f"[{bid}] ({kind}) {text}")
    return "\n".join(out)


# ---------------------------------------------------------------- page fitting
# How many bullets fit on one page is not a constant: it depends on line spacing, the
# margins `compact` applies, the font the template uses and how long the bullets are.
# A fixed cap therefore either wastes space or overflows. These helpers estimate the
# page from the template's own geometry so the budget can follow the spacing.
#
# No renderer is available (the project deliberately depends only on requests + PyYAML),
# so this is an ESTIMATE, not a measurement. SAFETY_LINES keeps a margin of error, and
# `tailor.py --fit` prints the numbers so a real .docx can be used to calibrate.
TWIPS_PER_INCH = 1440
COMPACT_MARGINS = {"top": 450, "right": 700, "bottom": 450, "left": 700}  # _tighten_margins
WORD_LINE_FACTOR = 1.15     # Word's "single" line box is ~1.15x the font size
CHAR_WIDTH_EM = 0.48        # mean glyph width of a proportional face, in em
SAFETY_LINES = 3            # slack, because glyph widths vary by more than the mean


def _para_text(para):
    return "".join(t.text or "" for t in para.iter(resume_bank.W + "t"))


def _bullet_point_size(tpl):
    """Font size of the bullets, in points (w:sz is in half-points)."""
    for i in tpl.bullets:
        for sz in tpl.paras[i].iter(resume_bank.W + "sz"):
            try:
                return int(sz.get(resume_bank.W + "val")) / 2
            except (TypeError, ValueError):
                pass
    return 11.0


def page_metrics(tpl, line_spacing=1.0, compact=True):
    """(lines_per_page, chars_per_line) for this template at this spacing."""
    m = re.search(r'<w:pgSz w:w="(\d+)" w:h="(\d+)"', tpl.doc_xml)
    pw, ph = (int(m.group(1)), int(m.group(2))) if m else (12240, 15840)
    mar = dict(COMPACT_MARGINS)
    if not compact:
        mm = re.search(r'<w:pgMar w:top="(\d+)" w:right="(\d+)" w:bottom="(\d+)" w:left="(\d+)"',
                       tpl.doc_xml)
        if mm:
            mar = {"top": int(mm.group(1)), "right": int(mm.group(2)),
                   "bottom": int(mm.group(3)), "left": int(mm.group(4))}
    usable_w = (pw - mar["left"] - mar["right"]) / TWIPS_PER_INCH
    usable_h = (ph - mar["top"] - mar["bottom"]) / TWIPS_PER_INCH
    pt = _bullet_point_size(tpl)
    char_w = (pt / 72.0) * CHAR_WIDTH_EM
    line_h = (pt / 72.0) * WORD_LINE_FACTOR * float(line_spacing)
    line_h += 20 / TWIPS_PER_INCH            # w:after="20" that compact sets per paragraph
    return int(usable_h / line_h), max(20, int(usable_w / char_w))


def _lines_for(text, chars_per_line):
    return max(1, -(-len(text) // chars_per_line))        # ceil division


def furniture_lines(tpl, chars_per_line):
    """Lines consumed by everything that is not a bullet: name, contact, section
    headings, each job's title and company/date line, the SKILLS block, education."""
    return sum(_lines_for(_para_text(p), chars_per_line)
               for i, p in enumerate(tpl.paras) if i not in tpl.bullets
               and _para_text(p).strip())


def fit_job_bullets(templates, line_spacing=1.0, compact=True, achievements=5,
                    page_lines=None, bullet_chars=None):
    """Largest n that still fits one page, where the first job gets n bullets, the
    second n-1, the third n-2 and so on. Returns (n, detail) for logging.

    The tightest template wins, since one budget is shared by all of them.
    """
    worst = None
    for tpl in templates:
        cap, cpl = page_metrics(tpl, line_spacing, compact)
        cap = page_lines or cap
        furn = furniture_lines(tpl, cpl)
        texts = [b["text"] for b in tpl.bullets.values()]
        # p75 length, so a run of longer-than-average bullets does not overflow
        p75 = sorted(len(t) for t in texts)[int(len(texts) * 0.75)] if texts else 90
        per_bullet = _lines_for("x" * (bullet_chars or p75), cpl)
        budget_lines = cap - furn - SAFETY_LINES - achievements * per_bullet
        jobs = max(1, len(tpl.jobs))
        # n + (n-1) + ... for `jobs` terms = jobs*n - (0+1+...+(jobs-1))
        offset = sum(range(jobs))
        n = (budget_lines // per_bullet + offset) // jobs
        detail = {"template": tpl.kind, "lines_per_page": cap, "chars_per_line": cpl,
                  "furniture": furn, "lines_per_bullet": per_bullet, "n": n}
        if worst is None or n < worst[0]:
            worst = (n, detail)
    n, detail = worst
    return max(1, int(n)), detail


def resolve_budget(templates):
    """Set PAGE_BUDGET for this run: achievements fixed, each job one bullet fewer than
    the one before it, and the whole thing sized to a single page at the configured
    line spacing. A block pinned by name in config.yaml overrides the fitted value."""
    global PAGE_BUDGET
    if not templates:
        return PAGE_BUDGET
    if str(JOB_BULLETS).strip().lower() == "auto":
        n, detail = fit_job_bullets(templates, LINE_SPACING, COMPACT,
                                    ACHIEVEMENTS_CAP, PAGE_LINES)
        how = f"auto-fit, tightest template {detail['template']}"
    else:
        n, how = int(JOB_BULLETS), "pinned in config"
    budget = budget_for(templates[0], n, ACHIEVEMENTS_CAP)
    for block in EXPLICIT_BLOCKS:                  # explicit names win over the fit
        if block in PAGE_BUDGET:
            budget[block] = PAGE_BUDGET[block]
    PAGE_BUDGET = budget
    shape = " + ".join(str(v) for v in budget.values())
    log.info("page budget: %s = %d bullets (line_spacing %s, %s)",
             shape, sum(budget.values()), LINE_SPACING, how)
    return budget


def budget_for(tpl, n, achievements=5, floor=1):
    """{block: cap} with the first job at n, the next n-1, and so on."""
    out = {"achievements": achievements}
    for i, job in enumerate(tpl.jobs):
        out[job["key"]] = max(floor, n - i)
    return out


# ---------------------------------------------------------------- Claude
SYSTEM = """You tailor a resume by SELECTING pre-written bullets. You never write, \
edit, paraphrase or invent bullet text — you only choose ids from the bank you are given.

The first reader is a recruiter, not an engineer, and a recruiter reads literally. A \
requirement only counts when the posting's EXACT words appear in a bullet that sits under a \
job. Nothing is inferred on the candidate's behalf: TypeScript does not prove JavaScript, \
Pydantic does not prove Python, cloud does not prove AWS, GitHub Actions does not prove CI/CD.

You are given a job description and a bank of bullets, grouped into blocks. Every bullet \
belongs to exactly one block (the key achievements block, or one employer). Bullets carry \
a template tag (backend / cloud_devops / frontend / fullstack); you may mix tags freely \
inside a block if that better matches the job.

Return ONLY a JSON object, no prose, no code fences:
{
  "base_template": "backend|cloud_devops|frontend|fullstack",
  "base_reason": "one sentence on why this template's skills section and framing fit best",
  "role_summary": "one sentence on what this job actually is",
  "required_terms": ["the posting's exact words for each required skill, most central first"],
  "preferred_terms": ["the posting's exact words for each preferred / nice-to-have skill"],
  "meta_requirements": ["requirements no bullet can say word-for-word: years, degrees, certs"],
  "selected": {"<block name>": ["<bullet id>", ...]},
  "selection_notes": "one or two sentences on the ordering logic",
  "semantic_score": 0-100,
  "fit_assessment": "2-3 sentences: is this worth applying to, and what is the weakest point"
}

Rules for the terms:
- Copy every term VERBATIM from the posting, in its own wording: if it says "Spring Boot", \
write "Spring Boot", not "Spring". One tool, language, platform or practice per entry. Never \
paraphrase, generalise, translate or merge terms.
- required_terms come from the required / basic / minimum qualifications. Order them by how \
central the posting makes them: the first is what the role exists to do.
- Anything that cannot appear word-for-word in a bullet goes in meta_requirements instead: \
"5+ years of Java", "a degree in Computer Science", "two database technologies", \
"US citizenship", "AWS certification".
- A term is what a recruiter would type into search: usually 1-3 words, never a clause. When \
the posting phrases a requirement as a long clause, take its searchable core in the posting's \
own words and put the full clause in meta_requirements. "Hands-on experience using \
enterprise-authorized AI-assisted software development tools" gives the term "AI-assisted".

Rules for the selection:
- An id may ONLY be listed under the block it appears beneath in the bank. A bullet belongs \
to one employer; it cannot be moved to another. Listing backend:150 under a block other than \
its own is an error, not a reordering.
- Respect each block's stated cap, and use it: the page is sized to hold exactly that many \
bullets, so fill every block unless nothing left in it relates to the posting.
- Serve what was ordered first. A bullet containing a required term in the posting's own \
words beats a more impressive bullet that contains none.
- Order each block so the bullets carrying the most central required terms come first. The \
FIRST bullet of the most recent job is the most-read line on the page: give it the \
highest-priority terms.
- Avoid near-duplicate bullets; each one should add new evidence. Never pick two bullets \
that make the same claim with different numbers, in the same block or across blocks.
- Vary the opening verbs: no verb may open more than 2 bullets on the whole page, and two \
bullets in a row never start with the same verb.
- semantic_score is your own engineer's estimate of fit. It is shown for context only; the \
recruiter score is computed separately, from the exact words."""


# Worth a second try: a dropped or garbled connection (SSLV3_ALERT_BAD_RECORD_MAC), a
# rate limit, an overload (529) or an upstream hiccup. A bad key or request is not, and
# neither is a read timeout - waiting another 180s would only stall the cycle.
RETRY_STATUS = {429, 500, 502, 503, 504, 529}
API_ATTEMPTS = 3


def _api_call(body: dict, key: str, timeout: int = 180) -> dict:
    for attempt in range(1, API_ATTEMPTS + 1):
        try:
            r = requests.post("https://api.anthropic.com/v1/messages",
                              headers={"x-api-key": key, "anthropic-version": "2023-06-01",
                                       "content-type": "application/json"},
                              json=body, timeout=timeout)
        except requests.ConnectionError as e:     # includes SSLError and ConnectTimeout
            if attempt == API_ATTEMPTS:
                raise
            wait, why = 2.0 ** attempt, type(e).__name__
        else:
            if r.status_code < 300:
                return r.json()
            if r.status_code not in RETRY_STATUS or attempt == API_ATTEMPTS:
                raise RuntimeError(f"Claude API {r.status_code}: {r.text[:400]}")
            wait, why = _retry_after(r, attempt), f"HTTP {r.status_code}"
        log.warning("Claude API %s - retry %d of %d in %.0fs", why, attempt,
                    API_ATTEMPTS - 1, wait)
        time.sleep(wait)


def _retry_after(r, attempt: int) -> float:
    """The server's retry-after when it sends one (capped), else 2s, 4s, ..."""
    try:
        return min(float(r.headers.get("retry-after")), 30.0)
    except (TypeError, ValueError):
        return 2.0 ** attempt


def _text_of(payload):
    return "".join(b.get("text", "") for b in payload.get("content", []) if b.get("type") == "text")


def _extract_json(payload):
    """Pull the JSON object out of a response, or explain precisely what came back."""
    text = _text_of(payload)
    stop = payload.get("stop_reason")
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.M).strip()
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start == -1 or end <= start:
        if stop == "max_tokens":
            raise ValueError(
                "hit max_tokens before any JSON was produced - the budget was spent on "
                "reasoning. Raise tailor.max_tokens in config.yaml")
        raise ValueError(f"no JSON object in response (stop_reason={stop}): "
                         f"{cleaned[:300] or '<empty response>'}")
    try:
        return json.loads(cleaned[start:end + 1])
    except json.JSONDecodeError as e:
        if stop == "max_tokens":
            raise ValueError("JSON truncated at max_tokens - raise tailor.max_tokens") from e
        raise ValueError(f"malformed JSON ({e}): {cleaned[start:start + 300]}") from e


def _save_debug(company, text):
    try:
        d = os.path.join(OUT_DIR, "_debug")
        os.makedirs(d, exist_ok=True)
        p = os.path.join(d, f"{safe(company, 24)}_{datetime.now():%Y%m%d-%H%M%S}.txt")
        with open(p, "w") as f:
            f.write(text)
        return p
    except Exception:
        return None


def ask_claude(jd_text, title, company, bank, skills, model, max_tokens=None):
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise SystemExit("ANTHROPIC_API_KEY is not set - export it and re-run.")
    skills_block = "\n".join(f"{kind}: {' || '.join(lines)}" for kind, lines in skills.items())
    user = (f"JOB TITLE: {title}\nCOMPANY: {company}\n\n"
            f"JOB DESCRIPTION:\n{jd_text[:MAX_JD_CHARS]}\n\n"
            f"SKILLS LINES BY TEMPLATE:\n{skills_block}\n\n"
            f"BULLET BANK:{bank_for_prompt(bank)}")
    budget = int(max_tokens or MAX_TOKENS)
    messages = [{"role": "user", "content": user}]

    payload = _api_call({"model": model, "max_tokens": budget, "system": SYSTEM,
                         "messages": messages}, key)
    try:
        return _extract_json(payload)
    except ValueError as first:
        # Retry with a much larger budget and the contract restated as a user turn.
        # (Assistant prefill is rejected by current models, so it is not used.)
        log.warning("[%s] %s - retrying with a larger token budget", company, first)
        retry_messages = messages + [
            {"role": "assistant", "content": _text_of(payload) or "(no output)"},
            {"role": "user", "content": "Reply with only the JSON object described in the "
                                        "system prompt - start at '{' and end at '}', with no "
                                        "preamble, explanation or code fence."},
        ]
        payload2 = _api_call({"model": model, "max_tokens": max(budget * 4, 16000),
                              "system": SYSTEM, "messages": retry_messages}, key)
        try:
            return _extract_json(payload2)
        except ValueError as second:
            path = _save_debug(company, f"FIRST: {first}\n\nSECOND: {second}\n\n"
                                        f"RAW 1:\n{json.dumps(payload, indent=2)[:6000]}\n\n"
                                        f"RAW 2:\n{json.dumps(payload2, indent=2)[:6000]}")
            raise ValueError(f"{second} (raw responses saved to {path})") from second


# ---------------------------------------------------------------- the recruiter's reading
# A recruiter is qualification hunting, and reads literally. A requirement counts only
# when the posting's own words (WHAT) appear in a bullet that sits inside a job (WHERE).
# Skills lines and summaries name no employer, so they are claims, never proof.
#
# Claude extracts the terms and proposes a selection; everything in this section is
# deterministic. It decides what is proven, repairs coverage within the page budget,
# orders the page so the most-asked-for terms come first, and computes the score the
# alert is gated on. Nothing here writes or rewords a bullet.

# Writing-quality signals, shared by selection tie-breaks and `--audit`: a plain-English
# reason (WHY), a real count of something (scale), a percentage, a verb that proves nothing.
WHY_RE = re.compile(r"\b(so that|so [\w/.-]+(?: [\w/.-]+){0,3} (?:could|can|got|get|saw|see|"
                    r"had|have|would|stayed|stay|kept|keep|no longer|never|did|caught|found|"
                    r"shipped|loaded|ran)|so customers|so users|so the|so teams?|enabling|"
                    r"allowing|letting|instead of|without (?:having|needing)|for \d+[kK]? "
                    r"(?:users|customers|employees|engineers|team)|used (?:daily )?by)\b", re.I)
SCALE_RE = re.compile(r"\d[\d,.]*\s*(k|m|million|thousand)?\s*(users|customers|requests|"
                      r"events|records|transactions|services|repos|engineers|team|members|"
                      r"screens|apis|endpoints|per (?:day|second|week|month)|daily|weekly|"
                      r"rps|qps|tb|gb|pb)\b", re.I)
PCT_RE = re.compile(r"\d+%")
# A verb is only vague when nothing concrete follows it: "Led a 4-person team" names
# something checkable, "Led key initiatives" would be true of any job.
VAGUE_RE = re.compile(r"^(led|drove|spearheaded|managed|oversaw|responsible|supported|helped|"
                      r"worked on|participated|contributed|collaborated|involved)\b"
                      r"(?!\s+(?:an?\s+|the\s+)?\d)", re.I)


def _norm_text(s):
    s = str(s or "").lower()
    s = re.sub(r"[^a-z0-9+#./ -]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _term_present(term, hay):
    """Word-boundary match, so 'C' does not match 'cloud' but 'c++' and 'ci/cd' do.
    Plurals are matched in both directions: a posting asking for 'REST API' is satisfied
    by a bullet saying 'REST APIs', and vice versa."""
    t = _norm_text(term)
    if not t:
        return False
    edge = r"[a-z0-9+#]"
    variants = [t]
    if len(t) > 3 and t.endswith("s"):
        variants.append(t[:-1])
    elif len(t) > 2 and t[-1].isalpha():
        variants.append(t + "s")
    return any(re.search(f"(?<!{edge})" + re.escape(v) + f"(?!{edge})", hay)
               for v in variants)


def _kw_present(kw, hay):
    """A requirement counts as present if it appears, or - for 'A/B/C' style wording,
    which a posting means as 'any of these' - if any one of its parts appears."""
    base = re.sub(r"\(.*?\)", " ", str(kw))
    if _term_present(base, hay):
        return True
    parts = [p for p in re.split(r"[/,]| or ", base) if len(p.strip()) > 1]
    return len(parts) > 1 and any(_term_present(p, hay) for p in parts)


def _dedupe(items) -> list[str]:
    """Strip, drop blanks and case-insensitive repeats, keep first-seen order."""
    seen, out = set(), []
    for item in items or []:
        text = str(item).strip()
        if text and text.lower() not in seen:
            seen.add(text.lower())
            out.append(text)
    return out


def normalize_plan(plan: dict) -> dict:
    """Accept the older response shape too, so a model that still answers with
    must_have_keywords / match_score is scored instead of rejected."""
    if not plan.get("required_terms") and plan.get("must_have_keywords"):
        plan["required_terms"] = plan["must_have_keywords"]
    if plan.get("semantic_score") is None and plan.get("match_score") is not None:
        plan["semantic_score"] = plan["match_score"]
    for key in ("required_terms", "preferred_terms", "meta_requirements"):
        plan[key] = _dedupe(plan.get(key))
    return plan


MAX_TERM_WORDS = 4   # "Software Development Life Cycle" is a real term; longer is a clause


def split_long_terms(plan: dict) -> list[str]:
    """Move any 'term' longer than MAX_TERM_WORDS words into meta_requirements.

    Recruiters search for terms, not clauses. A phrase like "responsible AI use in
    engineering workflows" can never appear word-for-word in a bullet, so scoring it is
    a guaranteed miss that says nothing about the candidate. It is moved to the
    check-by-eye list instead, and returned so the report can say what moved.
    """
    moved = []
    for key in ("required_terms", "preferred_terms"):
        keep = []
        for term in plan.get(key) or []:
            (moved if len(str(term).split()) > MAX_TERM_WORDS else keep).append(term)
        plan[key] = keep
    plan["meta_requirements"] = _dedupe((plan.get("meta_requirements") or []) + moved)
    return moved


def validate_terms(plan: dict, jd: str) -> list[str]:
    """Drop every term the posting does not literally contain; return what was dropped.

    WHAT means the posting's own wording. A term the model paraphrased ('Spring' for
    'Spring Boot') would score a match the recruiter never sees in the posting, so
    anything not in the JD text is removed before it can count.
    """
    hay = _norm_text(jd)
    dropped = []
    for key in ("required_terms", "preferred_terms"):
        kept = []
        for term in plan.get(key) or []:
            (kept if _kw_present(term, hay) else dropped).append(term)
        plan[key] = kept
    return dropped


def pick_base(plan: dict, templates: list):
    return next((t for t in templates if t.kind == plan.get("base_template")), templates[0])


def block_order(base) -> list[str]:
    """Blocks in the order they print: achievements, then jobs, most recent first."""
    return ["achievements"] + [j["key"] for j in base.jobs]


def proof_blocks(base) -> set[str]:
    """Blocks whose bullets count as proof: the ones that sit inside a job."""
    blocks = {j["key"] for j in base.jobs}
    if PROOF_INCLUDES_ACHIEVEMENTS:
        blocks.add("achievements")
    return blocks


def _hits(text: str, terms: list[str]) -> set[int]:
    """Indexes of the terms this text states in their own words."""
    hay = _norm_text(text)
    return {i for i, t in enumerate(terms) if _kw_present(t, hay)}


def select_blocks(plan: dict, bank: dict, base) -> tuple[dict, list[str], list[str]]:
    """Resolve Claude's selection into {block: [ids]} in print order.

    Pass 1 resolves every id to its TRUE block. A bullet's block is a property of the
    master template - it is that employer's line, not a slot the model gets to choose -
    so an id listed under the wrong block is a misfile, and discarding it would silently
    delete evidence. It is re-homed instead.

    Pass 2 drops duplicate text and applies each block's page_budget cap. Ids the model
    put in a block itself come first, so a re-homed bullet never displaces one chosen
    for that block deliberately. Every job block is present, even if empty, so coverage
    repair can still fill a job the model left out.
    """
    dropped, rehomed, routed = [], [], {}
    for block, ids in (plan.get("selected") or {}).items():
        for bid in ids or []:
            b = bank.get(bid)
            if b is None:                       # invented id - nothing to place
                dropped.append(f"{bid} (not in bank)")
                continue
            if b["block"] != block:
                rehomed.append(f"{bid}: filed under '{block}', belongs to '{b['block']}'")
            routed.setdefault(b["block"], []).append(bid)

    chosen = {}
    order = block_order(base)
    for block in order + [b for b in routed if b not in order]:
        cap = PAGE_BUDGET.get(block, DEFAULT_JOB_BUDGET)
        ids, seen = [], set()
        for bid in routed.get(block, []):
            text = bank[bid]["text"]
            if text in seen:
                dropped.append(f"{bid} (duplicate text)")
                continue
            if len(ids) >= cap:
                dropped.append(f"{bid} (over the {block} cap of {cap})")
                continue
            seen.add(text)
            ids.append(bid)
        chosen[block] = ids
    return chosen, dropped, rehomed


def repair_coverage(chosen: dict, bank: dict, terms: list[str], base) -> list[str]:
    """Swap in bullets that prove required terms the selection left unproven.

    The page has a fixed number of slots, so the most score per slot comes from making
    each one prove something the posting asked for. For each unproven term, most central
    first, the best bank bullet that says it is taken - most recent job first, then the
    one proving the most still-unproven terms, then one with a plain-English reason,
    then one with real scale. It is added if its block has room; otherwise it replaces
    the lowest-ranked bullet in that block whose terms are all proven elsewhere.

    Coverage only ever grows, no bullet changes employer, no cap is exceeded, and a term
    no bullet anywhere states is left alone - that is a bank gap, not a selection one.
    Returns one line per change, for the report.
    """
    if not terms:
        return []
    proof = proof_blocks(base)
    rank = {key: i for i, key in enumerate(block_order(base))}
    cache = {}

    def hits(bid):
        if bid not in cache:
            cache[bid] = _hits(bank[bid]["text"], terms)
        return cache[bid]

    def proven(skip=None):
        return set().union(*(hits(b) for blk in proof for b in chosen.get(blk, [])
                             if b != skip))

    changes = []
    for i, term in enumerate(terms):
        have = proven()
        if i in have:
            continue
        selected = {b for ids in chosen.values() for b in ids}
        on_page = {bank[b]["text"] for b in selected}
        candidates = [bid for bid, b in bank.items()
                      if b["block"] in proof and bid not in selected
                      and b["text"] not in on_page and i in hits(bid)]
        candidates.sort(key=lambda bid: (rank.get(bank[bid]["block"], len(rank)),
                                         -len(hits(bid) - have),
                                         not WHY_RE.search(bank[bid]["text"]),
                                         not SCALE_RE.search(bank[bid]["text"]),
                                         bid))
        for cand in candidates:
            block = bank[cand]["block"]
            ids = chosen.setdefault(block, [])
            if len(ids) < PAGE_BUDGET.get(block, DEFAULT_JOB_BUDGET):
                ids.append(cand)
                changes.append(f"added `{cand}` to {block} — proves *{term}*")
                break
            # lowest-ranked first: Claude ordered strongest-first
            victim = next((v for v in reversed(ids) if not hits(v) - proven(skip=v)), None)
            if victim:
                ids[ids.index(victim)] = cand
                changes.append(f"swapped `{victim}` for `{cand}` in {block} — proves "
                               f"*{term}*, and `{victim}` proved nothing unique")
                break
    return changes


def order_blocks(chosen: dict, bank: dict, terms: list[str]) -> dict:
    """Serve what was ordered first.

    Within each block, bullets carrying the most central required terms print first - a
    term's weight falls with its position in the posting - then ones with a plain-English
    reason, then ones with real scale, then Claude's own order. The first bullet of the
    most recent job, the most-read line on the page, is therefore the strongest one.
    """
    n = len(terms)

    def key(item):
        pos, bid = item
        text = bank[bid]["text"]
        weight = sum(n - i for i in _hits(text, terms))
        return (-weight, not WHY_RE.search(text), not SCALE_RE.search(text), pos)

    return {block: [bid for _, bid in sorted(enumerate(ids), key=key)]
            for block, ids in chosen.items()}


# Words that carry no claim, ignored when comparing two bullets for repetition.
_STOPWORDS = frozenset("a an the and or of to in on for with so by from at as into onto per "
                       "each its it their they them that this one no not across via over "
                       "under".split())


def opening_verb(text: str) -> str:
    """First word of a bullet, lowercased; hyphenated verbs stay whole (Load-tested)."""
    m = re.match(r"[A-Za-z][A-Za-z-]*", str(text).strip())
    return m.group(0).lower() if m else ""


def _shape(text: str) -> frozenset:
    """Content words with every number removed, so two bullets that differ only in
    their numbers have the same shape."""
    t = re.sub(r"\d[\d,.]*\s*[kmb]?\+?%?", " ", str(text).lower())
    return frozenset(w for w in re.findall(r"[a-z][a-z+#/.-]*", t) if w not in _STOPWORDS)


def near_duplicate(a: str, b: str) -> bool:
    """The same claim twice on one page: one sentence with different numbers, or the
    same work reworded. Bullets that also open with the same verb need less word
    overlap to read as a repeat (0.4) than bullets that open differently (0.7).
    Thresholds calibrated on the real bank: every same-verb pair above 0.4 was a repeat
    ("Ran 6 services across AWS EC2, ECS and Lambda" / "Ran 25 services across ..."),
    and the cross-verb pairs above 0.7 were rewordings ("Migrated 30 JavaScript
    modules to TypeScript" / "Moved 150 JavaScript modules to TypeScript")."""
    sa, sb = _shape(a), _shape(b)
    overlap = len(sa & sb) / max(1, len(sa | sb))
    return overlap >= (0.4 if opening_verb(a) == opening_verb(b) else 0.7) - 1e-9


def enforce_variety(chosen: dict, bank: dict, terms: list[str], base) -> tuple[dict, list[str]]:
    """Make the page read like one person wrote it.

    Two rules, applied to the finished page only - the master templates are not
    touched: no opening verb starts more than MAX_VERB_USES bullets, and no claim is
    printed twice (an exact repeat, or the same sentence with different numbers).

    Bullets are judged most-valuable first: jobs before Key Achievements, because only
    job bullets count as proof, and within each block in printed order. Every bullet
    that fits is accepted. Each one that breaks a rule is then replaced by the best
    unused bullet from the same employer that fits and proves everything the page would
    otherwise lose; if there is none it is dropped. A bullet that is the page's only
    proof of a required term is never dropped: if its verb is at the cap, room is made
    by retiring a lower-priority bullet with the same verb that proves nothing unique.
    Only when that is impossible does it stay over the cap, because losing the
    qualification costs more than a repeated verb.

    Returns (new selection, one line per change for the report). Input is not modified.
    """
    proof = proof_blocks(base)
    jobs_first = [j["key"] for j in base.jobs] + ["achievements"]
    jobs_first += [b for b in chosen if b not in jobs_first]
    n = len(terms)
    cache = {}

    def hits(bid):
        if bid not in cache:
            cache[bid] = _hits(bank[bid]["text"], terms)
        return cache[bid]

    kept, verbs = [], {}
    final = {block: [] for block in chosen}

    def fits(bid):
        text = bank[bid]["text"]
        return (verbs.get(opening_verb(text), 0) < MAX_VERB_USES
                and not any(near_duplicate(text, bank[k]["text"]) for k in kept))

    def accept(bid, block):
        kept.append(bid)
        final[block].append(bid)
        v = opening_verb(bank[bid]["text"])
        verbs[v] = verbs.get(v, 0) + 1

    def why_not(bid):
        text = bank[bid]["text"]
        v = opening_verb(text)
        if verbs.get(v, 0) >= MAX_VERB_USES:
            return f"\"{v.capitalize()}\" already opens {MAX_VERB_USES} bullets"
        twin = next(k for k in kept if near_duplicate(text, bank[k]["text"]))
        return f"repeats `{twin}`"

    # pass 1: everything that fits, in priority order
    breaking = []
    for block in jobs_first:
        for bid in chosen.get(block, []):
            if fits(bid):
                accept(bid, block)
            else:
                breaking.append((block, bid, why_not(bid)))

    def rank_key(c):
        return (-sum(n - i for i in hits(c)), not WHY_RE.search(bank[c]["text"]),
                not SCALE_RE.search(bank[c]["text"]), c)

    def make_room(bid, need):
        """`bid` is the page's only proof of `need` but its verb is at the cap. Free
        the verb by retiring a lower-priority bullet that opens the same way and proves
        nothing unique, swapping in another bullet from that bullet's employer."""
        v = opening_verb(bank[bid]["text"])
        if verbs.get(v, 0) < MAX_VERB_USES:
            return None                       # the clash is a repeated claim, not the cap
        for k in reversed([x for x in kept if opening_verb(bank[x]["text"]) == v]):
            kb = bank[k]["block"]
            others = set().union(*(hits(x) for blk in proof for x in final.get(blk, [])
                                   if x != k))
            if kb in proof and hits(k) - others - hits(bid):
                continue                      # k is itself the only proof of something
            kept.remove(k)
            final[kb].remove(k)
            verbs[v] -= 1
            on_page = {bank[x]["text"] for x in kept} | {bank[bid]["text"]}
            pool = sorted((c for c, b in bank.items()
                           if b["block"] == kb and c not in (k, bid) and c not in kept
                           and b["text"] not in on_page and opening_verb(b["text"]) != v
                           and fits(c) and not near_duplicate(b["text"], bank[bid]["text"])),
                          key=rank_key)
            if pool:
                accept(pool[0], kb)
                return (f"replaced `{k}` with `{pool[0]}` in {kb} to make room for `{bid}`, the "
                        f"only proof of {', '.join(terms[i] for i in sorted(need))}")
            kept.append(k)                    # no stand-in for k: put it back
            final[kb].append(k)
            verbs[v] += 1
        return None

    # pass 2: a stand-in for each bullet that broke a rule
    changes = []
    for block, bid, reason in breaking:
        proven = set().union(*(hits(k) for blk in proof for k in final.get(blk, [])))
        need = (hits(bid) - proven) if block in proof else set()
        on_page = {bank[k]["text"] for k in kept}
        pool = [c for c, b in bank.items()
                if b["block"] == block and c not in kept and b["text"] not in on_page
                and need <= hits(c) and fits(c)]
        pool.sort(key=rank_key)
        if pool:
            accept(pool[0], block)
            changes.append(f"replaced `{bid}` with `{pool[0]}` in {block} — {reason}")
        elif need and (room := make_room(bid, need)):
            accept(bid, block)
            changes.append(room)
        elif need:
            accept(bid, block)
            changes.append(f"kept `{bid}` in {block} although {reason}: it is the only proof "
                           f"of {', '.join(terms[i] for i in sorted(need))}")
        else:
            changes.append(f"dropped `{bid}` from {block} — {reason}, and no other bullet "
                           f"from that employer fits")
    return final, changes


def separate_repeats(chosen: dict, bank: dict) -> dict:
    """Two bullets in a row never open with the same verb. The first bullet of every
    block stays put - it is the most-read line. For the second of a pair, the nearest
    bullet below with a different verb is pulled up between them; when the pair ends
    the block and there is nothing below, the repeat moves up instead, to the latest
    earlier gap whose neighbours both open differently."""
    out = {}
    for block, ids in chosen.items():
        ids = list(ids)

        def verb(b):
            return opening_verb(bank[b]["text"])

        for i in range(1, len(ids)):
            v = verb(ids[i])
            if v != verb(ids[i - 1]):
                continue
            j = next((k for k in range(i + 1, len(ids)) if verb(ids[k]) != v), None)
            if j is not None:
                ids.insert(i, ids.pop(j))
                continue
            moving = ids.pop(i)
            spot = next((p for p in range(i - 1, 0, -1)
                         if verb(ids[p - 1]) != v and verb(ids[p]) != v), None)
            ids.insert(i if spot is None else spot, moving)
        out[block] = ids
    return out


def fill_page(chosen: dict, bank: dict, plan: dict, base) -> tuple[dict, list[str]]:
    """Top every block up to its page_budget cap.

    Claude often picks fewer bullets than the page holds - 22 of 35 on one live run -
    and an empty slot proves nothing. Each block is filled, most recent job first, from
    that employer's unused bullets ranked by what the posting asked for: required terms
    (the most central weighted highest), then preferred terms, then a plain-English
    reason and real scale. A filler obeys the same variety rules as every other bullet:
    its opening verb is under MAX_VERB_USES and it repeats no claim already on the page.
    A bullet that relates to nothing in the posting is only used if it comes from the
    base template, so a Java role is never padded with iOS work.

    Returns (new selection, one line per added bullet). Input is not modified.
    """
    required = plan.get("required_terms") or []
    preferred = plan.get("preferred_terms") or []
    n = len(required)
    final = {block: list(ids) for block, ids in chosen.items()}
    on_page = [b for ids in final.values() for b in ids]
    verbs = {}
    for b in on_page:
        v = opening_verb(bank[b]["text"])
        verbs[v] = verbs.get(v, 0) + 1

    def rank(bid):
        text = bank[bid]["text"]
        req = sum(n - i for i in _hits(text, required))
        pref = len(_hits(text, preferred))
        return (-req, -pref, bank[bid]["tpl"].kind != base.kind,
                not WHY_RE.search(text), not SCALE_RE.search(text), bid)

    added = []
    order = [j["key"] for j in base.jobs] + ["achievements"]
    for block in order + [b for b in final if b not in order]:
        cap = PAGE_BUDGET.get(block, DEFAULT_JOB_BUDGET)
        if len(final.setdefault(block, [])) >= cap:
            continue
        texts = {bank[b]["text"] for b in on_page}
        pool = sorted((bid for bid, b in bank.items()
                       if b["block"] == block and bid not in on_page and b["text"] not in texts),
                      key=rank)
        for bid in pool:
            if len(final[block]) >= cap:
                break
            text = bank[bid]["text"]
            r = rank(bid)
            if r[0] == 0 and r[1] == 0 and bank[bid]["tpl"].kind != base.kind:
                continue                      # unrelated to the posting and off-template
            v = opening_verb(text)
            if verbs.get(v, 0) >= MAX_VERB_USES:
                continue
            if any(near_duplicate(text, bank[k]["text"]) for k in on_page):
                continue
            final[block].append(bid)
            on_page.append(bid)
            verbs[v] = verbs.get(v, 0) + 1
            proves = [required[i] for i in sorted(_hits(text, required))]
            added.append(f"filled {block} with `{bid}`"
                         + (f" — says {', '.join(proves)}" if proves else ""))
    return final, added


def recruiter_score(plan: dict, chosen: dict, bank: dict, base) -> dict:
    """What a recruiter would count as proven, and the score the alert is gated on.

    score = required terms stated in the posting's own words inside a job bullet, over
    required terms. A mention only in the SKILLS line or in Key Achievements is reported
    as a claim: neither names an employer, so neither counts (achievements can be made
    to count with tailor.proof_includes_achievements).
    """
    required = plan.get("required_terms") or []
    preferred = plan.get("preferred_terms") or []
    proof = proof_blocks(base)

    def where(terms, blocks):
        found = {}
        for block in block_order(base):
            if block not in blocks:
                continue
            for pos, bid in enumerate(chosen.get(block, []), 1):
                hay = _norm_text(bank[bid]["text"])
                for t in terms:
                    if t not in found and _kw_present(t, hay):
                        found[t] = [block.title(), pos]
        return found

    proven = where(required, proof)
    proven_pref = where(preferred, proof)
    in_achievements = {} if "achievements" in proof else where(required, {"achievements"})
    skills = _norm_text(" | ".join(txt for _, txt in base.skill_lines))
    bank_text = [_norm_text(b["text"]) for b in bank.values() if b["block"] in proof]
    in_bank = {t for t in required if any(_kw_present(t, h) for h in bank_text)}

    return {
        "score": int(round(100 * len(proven) / len(required))) if required else None,
        "required": len(required),
        "proven": proven,
        "proven_preferred": proven_pref,
        "preferred_unproven": [t for t in preferred if t not in proven_pref],
        "claims_skills": [t for t in required if t not in proven and _kw_present(t, skills)],
        "claims_achievements": [t for t in required if t not in proven and t in in_achievements],
        "page_gaps": [t for t in required if t not in proven and t in in_bank],
        "bank_gaps": [t for t in required if t not in in_bank],
        # SKILLS may only repeat what a bullet below it proves
        "padding": [t for t in required + preferred
                    if (t in proven or t in proven_pref) and not _kw_present(t, skills)],
    }


def used_texts(chosen: dict, bank: dict) -> dict:
    """{block: [bullet text + template tag]} for the report, in print order."""
    return {block: [f"{bank[bid]['text']}   [{bank[bid]['tpl'].kind}]" for bid in ids]
            for block, ids in chosen.items() if ids}


def render_resume(base, chosen: dict, bank: dict, out_docx: str, padding: list[str]) -> None:
    """Write the .docx: the base template with exactly `chosen` as its bullets."""
    blocks = {block: [bank[bid]["tpl"].bullet_node(bank[bid]["idx"]) for bid in ids]
              for block, ids in chosen.items()}
    # padding repeats the posting's words, which can arrive lowercase ("testing");
    # capitalise those so they sit naturally beside "Security" and "Java"
    padding = [t[:1].upper() + t[1:] if t.islower() else t for t in padding]
    base.render(blocks, out_docx, skill_text=build_skill_text(base, padding),
                compact=COMPACT, line_spacing=LINE_SPACING)


def build_skill_text(base, extra_terms):
    """Append terms to the base SKILLS line, skipping ones already there. Callers pass
    only terms a selected bullet proves, so the line never claims what the page can't
    back up."""
    if not extra_terms:
        return None
    out = {}
    existing = " | ".join(txt for _, txt in base.skill_lines).lower()
    add = [t for t in extra_terms if t.lower() not in existing]
    if not add:
        return None
    idx, txt = base.skill_lines[0]
    label, _, rest = txt.partition(":")
    out[idx] = f"{rest.strip()} | " + " | ".join(add)
    return out


# ---------------------------------------------------------------- keyword gaps
GAP_DIR_NAME = "_gaps"


def _gap_paths():
    d = os.path.join(OUT_DIR, GAP_DIR_NAME)
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, "keyword_gaps.json"), os.path.join(d, "KEYWORD_GAPS.md")


def _norm_kw(k):
    k = re.sub(r"\(.*?\)", " ", str(k)).strip().lower()
    k = re.sub(r"[^a-z0-9+#./ -]+", " ", k)
    k = re.sub(r"\s+", " ", k).strip()
    return re.sub(r"\b(experience|hands[- ]on|with|in|of|the|a|an)\b", "", k).strip(" -/.")


def record_gaps(plan, job, base_kind):
    """Accumulate every required keyword no bullet in the bank evidences.

    This is a shopping list, not an edit: nothing is written into the templates. Each
    entry keeps a count, the roles that wanted it, and which base template those roles
    mapped to, so it is clear where a real bullet would belong.
    """
    missing = [k for k in (plan.get("missing_keywords") or []) if str(k).strip()]
    if not missing:
        return None
    json_path, md_path = _gap_paths()
    try:
        with open(json_path) as f:
            data = json.load(f)
    except Exception:
        data = {}

    stamp = datetime.now().isoformat(timespec="seconds")
    for kw in missing:
        key = _norm_kw(kw)
        if not key or len(key) > 60:
            continue
        e = data.setdefault(key, {"keyword": str(kw).strip(), "count": 0, "first_seen": stamp,
                                  "templates": {}, "jobs": []})
        e["count"] += 1
        e["last_seen"] = stamp
        e["templates"][base_kind] = e["templates"].get(base_kind, 0) + 1
        entry = {"company": job.get("company"), "title": job.get("title"), "url": job.get("url")}
        if entry not in e["jobs"]:
            e["jobs"] = ([entry] + e["jobs"])[:10]

    with open(json_path, "w") as f:
        json.dump(data, f, indent=2, sort_keys=True)
    _render_gap_report(data, md_path)
    return md_path


def _render_gap_report(data, md_path):
    rows = sorted(data.values(), key=lambda e: (-e["count"], e["keyword"].lower()))
    total = sum(e["count"] for e in rows)
    out = ["# Keyword gaps", "",
           f"Requirements that appeared in job descriptions but are not evidenced by any "
           f"bullet in the resume bank. {len(rows)} distinct keywords across {total} "
           f"mentions. Updated {datetime.now():%Y-%m-%d %H:%M}.", "",
           "Nothing here is added to your templates automatically. Treat the top of this "
           "list as the highest-leverage real experience to acquire, or as bullets to "
           "write yourself if the experience already exists and the bank just never "
           "captured it.", "",
           "| # | Keyword | Seen | Usual template | Example role |",
           "|---:|---|---:|---|---|"]
    for i, e in enumerate(rows[:80], 1):
        tpl = max(e["templates"], key=e["templates"].get) if e.get("templates") else "-"
        job = (e.get("jobs") or [{}])[0]
        ex = f"{job.get('company','')} — {(job.get('title') or '')[:46]}".strip(" —")
        out.append(f"| {i} | {e['keyword'][:52]} | {e['count']} | {tpl} | {ex} |")
    if len(rows) > 80:
        out.append("")
        out.append(f"_...and {len(rows) - 80} more in keyword_gaps.json_")
    out += ["", "## Seen 3+ times — worth acting on", ""]
    hot = [e for e in rows if e["count"] >= 3]
    out += ([f"- **{e['keyword']}** ({e['count']}×)" for e in hot] if hot
            else ["_nothing yet — needs a few more tailored jobs_"])
    with open(md_path, "w") as f:
        f.write("\n".join(out))


# ---------------------------------------------------------------- report
def _bullet_line(text: str) -> str:
    """Report text of a bullet, without the trailing template tag."""
    return re.sub(r"\s+\[[a-z_]+\]\s*$", "", text)


def write_report(path, job, plan, used, dropped, base, rehomed=None, swaps=None, variety=None,
                 filled=None):
    rs = plan.get("recruiter") or {}
    score = rs.get("score")
    required = plan.get("required_terms") or []
    proven = rs.get("proven") or {}
    bar = "#" * int(round((score or 0) / 5)) + "." * (20 - int(round((score or 0) / 5)))
    lines = [
        f"# {job['title']}", f"**{job['company']}** · {job.get('location') or 'n/a'}",
        f"{job.get('url') or ''}", "",
        f"## Recruiter score: {score}/100  `{bar}`", "",
        f"**{len(proven)} of {len(required)}** required terms appear in the posting's own "
        f"words inside a job — the only place a recruiter counts them.", "",
        f"_Semantic fit (Claude's engineering read, context only — never gates the alert): "
        f"{plan.get('semantic_score', '?')}/100_", "",
        f"**Base template:** {base.kind}  — {plan.get('base_reason', '')}", "",
        f"**Role:** {plan.get('role_summary', '')}", "",
        f"**Verdict:** {plan.get('fit_assessment', '')}", "",
        "## Proven — their words, inside a job", "",
    ]
    lines += ([f"- **{t}** — {w[0]}, bullet {w[1]}" for t, w in proven.items()]
              or ["_none_"])

    top = required[:3]
    first = next((j["key"] for j in base.jobs if used.get(j["key"])), None)
    if first and top:
        carried = [t for t in top if _kw_present(t, _norm_text(_bullet_line(used[first][0])))]
        lines += ["", "## First bullet check", ""]
        if carried:
            lines.append(f"The first bullet under **{first.title()}** — the most-read line "
                         f"on the page — carries **{', '.join(carried)}** ({len(carried)} of "
                         f"the top {len(top)} required terms).")
        else:
            lines.append(f"The first bullet under **{first.title()}** carries none of the top "
                         f"{len(top)} required terms ({', '.join(top)}). The most-read line "
                         f"on the page is serving something nobody ordered.")

    claims = ([f"- **{t}** — SKILLS line only" for t in rs.get("claims_skills") or []]
              + [f"- **{t}** — Key Achievements only (names no employer)"
                 for t in rs.get("claims_achievements") or []])
    if claims:
        lines += ["", "## Claims without proof", "",
                  "On the page, but nowhere a recruiter counts it — prove these inside a job:",
                  ""] + claims
    if rs.get("page_gaps"):
        lines += ["", "## Page gaps — in your bank, not on this page", "",
                  "A bullet in your templates says these, but it lost its slot to "
                  "higher-priority terms in the page budget:", ""]
        lines += [f"- {t}" for t in rs["page_gaps"]]
    lines += ["", "## Bank gaps — no bullet anywhere says these", ""]
    if rs.get("bank_gaps"):
        lines += ["Pooled into `KEYWORD_GAPS.md`. If the experience is real, write it into a "
                  "master template in the posting's own words:", ""]
        lines += [f"- {t}" for t in rs["bank_gaps"]]
    else:
        lines.append("_none — every required term is said somewhere in your bank_")

    if plan.get("preferred_terms"):
        lines += ["", "## Preferred terms (reported, never scored)", "",
                  "Proven: " + (", ".join(rs.get("proven_preferred") or {}) or "_none_"), "",
                  "Not proven: " + (", ".join(rs.get("preferred_unproven") or []) or "_none_")]
    if plan.get("meta_requirements"):
        lines += ["", "## Check by eye — requirements no bullet can state", ""]
        lines += [f"- {m}" for m in plan["meta_requirements"]]
    if plan.get("moved_terms"):
        lines += ["", "## Moved to check by eye — clauses, not terms", "",
                  "No bullet could ever contain these word-for-word, so they are not scored:", ""]
        lines += [f"- {t}" for t in plan["moved_terms"]]
    if plan.get("ignored_terms"):
        lines += ["", "## Ignored — not in the posting's own wording", "",
                  ", ".join(plan["ignored_terms"])]
    if swaps:
        lines += ["", f"## Coverage repair ({len(swaps)})", ""] + [f"- {s}" for s in swaps]
    if filled:
        lines += ["", f"## Page fill ({len(filled)})", "",
                  "Claude left slots empty; these were added to reach the page budget:", ""]
        lines += [f"- {f}" for f in filled]
    if variety:
        lines += ["", f"## Variety ({len(variety)})", "",
                  f"No opening verb starts more than {MAX_VERB_USES} bullets, and no claim "
                  f"prints twice:", ""] + [f"- {v}" for v in variety]

    lines += ["", "## Bullets used", ""]
    for block, items in used.items():
        lines.append(f"**{block.title()}**")
        lines += [f"{i}. {t}" for i, t in enumerate(items, 1)] + [""]
    if plan.get("selection_notes"):
        lines += [f"_{plan['selection_notes']}_", ""]
    if rehomed:
        lines += [f"## Re-filed bullets ({len(rehomed)})", "",
                  "These ids were listed under the wrong block and were moved to the "
                  "employer they actually belong to, rather than discarded:", "",
                  "- " + "\n- ".join(rehomed), ""]
    if dropped:
        lines += ["## Discarded ids", "", ", ".join(dropped), ""]
    lines += ["---", f"_generated {datetime.now():%Y-%m-%d %H:%M}_"]
    with open(path, "w") as f:
        f.write("\n".join(lines))


def log_score(job: dict, plan: dict, passed: bool, bar) -> None:
    """One line per scored posting, pass or fail, so the bar can be tuned from data."""
    rs = plan.get("recruiter") or {}
    row = {"ts": datetime.now().isoformat(timespec="seconds"),
           "company": job.get("company"), "title": job.get("title"),
           "job_id": str(job.get("job_id", "")), "url": job.get("url"),
           "recruiter": rs.get("score"), "semantic": plan.get("semantic_score"),
           "required": rs.get("required"), "proven": len(rs.get("proven") or {}),
           "bar": bar, "passed": passed}
    path = os.path.join(OUT_DIR, GAP_DIR_NAME, "scored.jsonl")
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a") as f:
            f.write(json.dumps(row) + "\n")
    except OSError as e:
        log.warning("could not append to %s: %s", path, e)


# ---------------------------------------------------------------- job sources
def safe(s, n=48):
    return re.sub(r"[^A-Za-z0-9._-]+", "_", s or "").strip("_")[:n] or "job"


# ---------------------------------------------------------------- live preview
def append_live(path, step: str, **data) -> None:
    """One step as a JSON line. Watching the tailor must never break it, so a failed
    write is logged and dropped."""
    if not path:
        return
    try:
        with open(path, "a") as f:
            f.write(json.dumps({"step": step, "ts": round(time.time(), 3), **data},
                               default=str) + "\n")
    except OSError as e:
        log.debug("live trace write failed: %s", e)


def page_snapshot(chosen: dict, bank: dict, base, terms: list[str]) -> list[dict]:
    """The page as it stands: every block in print order, each bullet with the
    required terms (by index) it proves."""
    heads = {j["key"]: j for j in base.jobs}
    proof = proof_blocks(base)
    return [{"block": block,
             "title": heads.get(block, {}).get("title") or "Key Achievements",
             "company": heads.get(block, {}).get("company", ""),
             "meta": heads.get(block, {}).get("meta", ""),
             "proof": block in proof,
             "bullets": [{"id": bid, "text": bank[bid]["text"],
                          "hits": sorted(_hits(bank[bid]["text"], terms))}
                         for bid in chosen.get(block) or [] if bid in bank]}
            for block in block_order(base)]


class LiveTrace:
    """One posting's trip through the pipeline, written step by step to its own file in
    LIVE_DIR for live_preview.py to play back as it happens."""

    def __init__(self, job: dict):
        self.path = None
        if not LIVE_DIR:
            return
        try:
            os.makedirs(LIVE_DIR, exist_ok=True)
            stem = (f"{datetime.now():%Y%m%d-%H%M%S}_{safe(job.get('company'), 20)}_"
                    f"{safe(str(job.get('job_id', '')), 14)}")
            path, n = os.path.join(LIVE_DIR, stem + ".jsonl"), 1
            while os.path.exists(path):
                n += 1
                path = os.path.join(LIVE_DIR, f"{stem}-{n}.jsonl")
            self.path = path
        except OSError as e:
            log.debug("live trace off for this posting: %s", e)

    def emit(self, step: str, **data) -> None:
        append_live(self.path, step, **data)

    def page(self, stage: str, label: str, chosen, bank, base, terms, notes=()) -> None:
        if not self.path:
            return
        try:
            self.emit("page", stage=stage, label=label,
                      notes=[re.sub(r"[`*]", "", str(n)) for n in notes or ()],
                      blocks=page_snapshot(chosen, bank, base, terms))
        except Exception as e:                # a snapshot bug must not stop a resume
            log.debug("live snapshot failed: %s", e)


# ---------------------------------------------------------------- away days
AWAY_FILE = os.path.join(HERE, "away.yaml")
ICLOUD_ROOT = os.path.expanduser("~/Library/Mobile Documents/com~apple~CloudDocs")


def load_away(path=None) -> dict:
    """away.yaml, read fresh on every call so a date added while the watcher runs counts
    from the next resume on. Missing or broken file = no away days."""
    try:
        with open(path or AWAY_FILE) as f:
            return yaml.safe_load(f) or {}
    except FileNotFoundError:
        return {}
    except Exception as e:
        log.warning("away.yaml unreadable (%s) - resumes stay local only", e)
        return {}


def _minutes(hhmm: str) -> int:
    h, m = str(hhmm).strip().split(":")
    return int(h) * 60 + int(m)


def away_window(away: dict, now=None):
    """(start, end) in minutes when `now` falls on a listed away date, else None. A date
    may carry its own hours ("2026-10-14 12:00-18:00"); otherwise `hours` applies."""
    now = now or datetime.now()
    today = now.strftime("%Y-%m-%d")
    for entry in away.get("dates") or []:
        day, _, hours = str(entry).strip().partition(" ")
        if day == today:
            start, end = (hours.strip() or away.get("hours") or "09:00-17:00").split("-")
            return _minutes(start), _minutes(end)
    return None


def away_now(now=None, path=None) -> bool:
    now = now or datetime.now()
    win = away_window(load_away(path), now)
    return bool(win) and win[0] <= now.hour * 60 + now.minute < win[1]


def icloud_dir(away=None) -> str:
    away = load_away() if away is None else away
    return os.path.join(ICLOUD_ROOT, str(away.get("folder") or "JobResumes"))


def copy_to_icloud(docx: str, folder: str, now=None, path=None):
    """During away hours, also drop the resume into iCloud Drive so it can be attached
    from the phone. The local copy stays where it is; a failed copy only logs."""
    if not away_now(now, path):
        return None
    dest_dir = os.path.join(icloud_dir(load_away(path)), os.path.basename(folder))
    try:
        os.makedirs(dest_dir, exist_ok=True)
        return shutil.copy2(docx, dest_dir)
    except OSError as e:
        log.warning("iCloud copy failed (%s) - resume is still in %s", e, folder)
        return None


def away_status() -> str:
    """The schedule as the watcher sees it, plus a real write to iCloud Drive - run it
    from the terminal that runs the watcher, since macOS grants iCloud access per app."""
    away = load_away()
    now = datetime.now()
    lines = [f"away.yaml: {AWAY_FILE}",
             f"default hours: {away.get('hours') or '09:00-17:00'}",
             f"iCloud folder: {icloud_dir(away)}", "dates:"]
    upcoming = [str(d) for d in away.get("dates") or []
                if str(d)[:10] >= now.strftime("%Y-%m-%d")]
    lines += [f"  {d}" for d in upcoming] or ["  (none upcoming)"]
    lines.append(f"right now ({now:%Y-%m-%d %H:%M}): "
                 + ("AWAY - resumes also go to iCloud" if away_now(now) else "home - local only"))
    probe = os.path.join(icloud_dir(away), ".write_test")
    try:
        os.makedirs(os.path.dirname(probe), exist_ok=True)
        with open(probe, "w") as f:
            f.write(now.isoformat())
        os.remove(probe)
        lines.append("iCloud write test: OK")
    except OSError as e:
        lines.append(f"iCloud write test: FAILED ({e}) - give this terminal access to "
                     "iCloud Drive in System Settings > Privacy & Security > Files and Folders")
    return "\n".join(lines)


def tailor_job(job, cfg, templates, bank, skills, model, notify=True, min_score=None):
    """Score a posting the way a recruiter reads it, and build a resume only if it clears
    `min_score`.

    Claude extracts the posting's required terms and proposes a selection. Everything
    after that is deterministic: misfiled bullets are re-homed, coverage is repaired
    inside the page budget, the page is ordered so the most-asked-for terms come first,
    and the recruiter score - required terms proven in the posting's own words inside a
    job - decides whether a resume is written and an alert sent. Claude's semantic score
    is recorded for context and never gates anything. Below the bar nothing is written,
    but the score and the gaps are still recorded: a weak match is exactly where the gap
    list is useful.
    """
    jd = job.get("description") or ""
    if not jd.strip():
        log.warning("no job description for %s — skipping", job["title"])
        return None
    trace = LiveTrace(job)
    trace.emit("job", company=job.get("company"), title=job.get("title"),
               url=job.get("url") or "", job_id=str(job.get("job_id", "")), jd=jd,
               model=model, bar=min_score)
    try:
        raw = ask_claude(jd, job["title"], job["company"], bank, skills, model)
    except Exception as e:
        trace.emit("error", message=str(e)[:400])
        raise
    plan = normalize_plan(raw)
    plan["moved_terms"] = split_long_terms(plan)
    plan["ignored_terms"] = validate_terms(plan, jd)
    if plan["ignored_terms"]:
        log.info("[%s] %s - ignored %d term(s) not in the posting's wording: %s",
                 job["company"], job["title"][:50], len(plan["ignored_terms"]),
                 ", ".join(plan["ignored_terms"][:6]))

    base = pick_base(plan, templates)
    terms = plan["required_terms"]
    trace.emit("plan", required=terms, preferred=plan.get("preferred_terms") or [],
               meta=plan.get("meta_requirements") or [], moved=plan["moved_terms"],
               ignored=plan["ignored_terms"], base=base.kind,
               base_reason=plan.get("base_reason"), role_summary=plan.get("role_summary"),
               semantic=plan.get("semantic_score"), fit=plan.get("fit_assessment"),
               skills=[text for _, text in base.skill_lines])
    chosen, dropped, rehomed = select_blocks(plan, bank, base)
    trace.page("pick", "Claude's picks", chosen, bank, base, terms,
               list(rehomed or []) + [f"dropped {d}" for d in dropped or []])
    swaps = repair_coverage(chosen, bank, terms, base)
    trace.page("repair", "Coverage repair", chosen, bank, base, terms, swaps)
    chosen, variety = enforce_variety(order_blocks(chosen, bank, terms), bank, terms, base)
    trace.page("variety", "Strongest proof first, varied verbs", chosen, bank, base, terms,
               variety)
    chosen, filled = fill_page(chosen, bank, plan, base)
    trace.page("fill", "Page fill", chosen, bank, base, terms, filled)
    chosen = separate_repeats(order_blocks(chosen, bank, terms), bank)
    trace.page("final", "Final page", chosen, bank, base, terms)
    rs = plan["recruiter"] = recruiter_score(plan, chosen, bank, base)
    plan["missing_keywords"] = rs["bank_gaps"]   # record_gaps pools only real bank gaps
    score, semantic = rs["score"], plan.get("semantic_score")

    gaps = record_gaps(plan, job, base.kind)
    passed = min_score is None or (score is not None and score >= min_score)
    trace.emit("score", score=score, semantic=semantic, bar=min_score, passed=passed,
               **{k: rs.get(k) for k in ("proven", "page_gaps", "bank_gaps", "claims_skills",
                                         "claims_achievements", "padding")})
    plan["live_trace"] = trace.path          # notify_discord adds the alert step to it
    log_score(job, plan, passed, min_score)
    if not passed:
        log.info("[%s] %s - recruiter %s/100 (semantic %s) below %s, no resume built",
                 job["company"], job["title"][:60], score, semantic, min_score)
        trace.emit("skipped", score=score, bar=min_score)
        return {"folder": None, "score": score, "semantic_score": semantic,
                "plan": plan, "passed": False}

    folder = os.path.join(OUT_DIR, f"{safe(job['company'],24)}__{safe(job['title'],40)}__{safe(str(job.get('job_id','')),14)}")
    os.makedirs(folder, exist_ok=True)
    docx = os.path.join(folder, f"KALYANKUMAR_KONDURU_{safe(job['company'],20).upper()}.docx")
    render_resume(base, chosen, bank, docx, rs["padding"])
    plan["icloud"] = copy_to_icloud(docx, folder)
    write_report(os.path.join(folder, "match_report.md"), job, plan, used_texts(chosen, bank),
                 dropped, base, rehomed, swaps, variety, filled)
    with open(os.path.join(folder, "job_description.txt"), "w") as f:
        f.write(f"{job['title']}\n{job['company']}\n{job.get('url','')}\n\n{jd}")
    with open(os.path.join(folder, "plan.json"), "w") as f:
        json.dump(plan, f, indent=2, default=str)

    log.info("[%s] %s -> %s (recruiter %s/100, semantic %s, base %s)", job["company"],
             job["title"], folder, score, semantic, base.kind)
    if swaps:
        log.info("  coverage repair: %s", "; ".join(re.sub(r"[`*]", "", s) for s in swaps[:4]))
    if filled:
        log.info("  page fill: %d bullet(s) added to reach the page budget", len(filled))
    if variety:
        log.info("  variety: %d change(s) - %s", len(variety),
                 "; ".join(re.sub(r"[`*]", "", v) for v in variety[:3]))
    if gaps:
        log.info("  %d bank gap(s) pooled into %s", len(rs["bank_gaps"]), gaps)
    if plan["icloud"]:
        log.info("  away day: copied to iCloud Drive -> %s", plan["icloud"])
    trace.emit("done", folder=folder, docx=docx)
    if notify:
        notify_discord(job, plan, folder)
    return {"folder": folder, "score": score, "semantic_score": semantic,
            "plan": plan, "passed": True}


def notify_discord(job, plan, folder):
    hook = os.environ.get("DISCORD_WEBHOOK_URL")
    if not hook:
        append_live(plan.get("live_trace"), "alert", sent=False,
                    reason="DISCORD_WEBHOOK_URL is not set")
        return
    rs = plan.get("recruiter") or {}
    score = rs.get("score") or 0
    proven = rs.get("proven") or {}
    required = plan.get("required_terms") or []
    missing = (rs.get("page_gaps") or []) + (rs.get("bank_gaps") or [])
    color = 0x43B581 if score >= 75 else (0xFAA61A if score >= 55 else 0xED4245)
    fields = [
        {"name": "Role", "value": job["title"][:250], "inline": False},
        {"name": f"Proven in their words ({len(proven)} of {len(required)})",
         "value": (", ".join(proven) or "none")[:1000], "inline": False},
        {"name": "Not on the page", "value": (", ".join(missing) or "none")[:1000],
         "inline": False},
        {"name": "Semantic (context)", "value": f"{plan.get('semantic_score', '?')}/100",
         "inline": True},
        {"name": "Base template", "value": str(plan.get("base_template") or "?"),
         "inline": True},
        {"name": "Folder", "value": f"`{folder}`"[:1000], "inline": False},
    ]
    if plan.get("icloud"):
        fields.append({"name": "📱 On your phone",
                       "value": ("Files › iCloud Drive › " + " › ".join(
                           os.path.relpath(plan["icloud"], ICLOUD_ROOT).split(os.sep)))[:1000],
                       "inline": False})
    embed = {"title": f"📄 Resume ready — recruiter {score}/100"[:250],
             "url": job.get("url") or None,
             "description": (plan.get("fit_assessment") or "")[:600], "color": color,
             "fields": fields}
    sent = False
    try:
        r = requests.post(hook, json={"embeds": [embed]}, timeout=15)
        sent = r.status_code < 300
        if not sent:
            log.warning("discord notify failed: HTTP %s", r.status_code)
    except Exception as e:
        log.warning("discord notify failed: %s", e)
    append_live(plan.get("live_trace"), "alert", sent=sent)


def db_jobs(con, where, args=()):
    con.row_factory = sqlite3.Row
    rows = con.execute(
        f"SELECT rowid, company, job_id, title, location, url, posted, description, tailored, "
        f"match_score "
        f"FROM jobs WHERE {where}", args).fetchall()
    return [dict(r) for r in rows]


def ensure_schema(con):
    cols = {r[1] for r in con.execute("PRAGMA table_info(jobs)")}
    for name, decl in (("tailored", "TEXT"), ("match_score", "INTEGER")):
        if name not in cols:
            con.execute(f"ALTER TABLE jobs ADD COLUMN {name} {decl}")
    con.commit()


def fetch_missing_description(cfg, job):
    src = next((c for c in cfg["companies"] if c["name"] == job["company"]), None)
    if not src:
        return ""
    _, desc_fn = watcher.ADAPTERS[src["ats"]]
    try:
        return desc_fn(src, job["job_id"])
    except Exception as e:
        log.warning("[%s] description fetch failed: %s", job["company"], e)
        return ""


# ---------------------------------------------------------------- calibration & audit
def score_summary() -> str:
    """Recruiter vs semantic scores from scored.jsonl, so the bar is set from data.

    Literal scores run lower than semantic ones, so a bar tuned on the old semantic
    score will alert far less often. This shows what each bar would have let through.
    """
    path = os.path.join(OUT_DIR, GAP_DIR_NAME, "scored.jsonl")
    try:
        with open(path) as f:
            rows = [json.loads(line) for line in f if line.strip()]
    except FileNotFoundError:
        return "No scores yet - they accumulate in scored.jsonl as postings are tailored."
    rec = [r["recruiter"] for r in rows if isinstance(r.get("recruiter"), (int, float))]
    sem = [r["semantic"] for r in rows if isinstance(r.get("semantic"), (int, float))]
    if not rec:
        return f"{len(rows)} posting(s) logged, none with a recruiter score yet."
    out = [f"{len(rec)} posting(s) scored   median recruiter {statistics.median(rec):.0f}"
           + (f"   median semantic {statistics.median(sem):.0f}" if sem else ""),
           "", "recruiter score distribution:"]
    for lo in range(0, 100, 10):
        hi = 100 if lo == 90 else lo + 9
        n = sum(1 for v in rec if lo <= v <= hi)
        out.append(f"  {lo:>3}-{hi:<3} {'#' * n} {n}")
    out += ["", "alerts each bar would have sent:"]
    for bar in (40, 50, 55, 60, 65, 70, 75, 80):
        n = sum(1 for v in rec if v >= bar)
        out.append(f"  >= {bar}:  {n:>3} of {len(rec)}  ({100 * n // len(rec)}%)")
    return "\n".join(out)


def _bullet_usage() -> dict:
    """How often each bullet has been printed, read back from the 'Bullets used' list of
    every match_report.md - so the bullets recruiters see most get rewritten first."""
    counts = {}
    pattern = re.compile(r"^\d+\. (.*?)\s+\[[a-z_]+\]\s*$")
    for report in glob.glob(os.path.join(OUT_DIR, "*", "match_report.md")):
        try:
            with open(report) as f:
                for line in f:
                    m = pattern.match(line.strip())
                    if m:
                        counts[m.group(1)] = counts.get(m.group(1), 0) + 1
        except OSError:
            continue
    return counts


def bank_audit(bank: dict) -> tuple[str, dict]:
    """Which bullets a recruiter cannot yet read as a qualification.

    The tailor enforces WHAT (their words) and WHERE (inside a job). HOW, WHY and real
    numbers are facts only the candidate has, so this is a worklist rather than a fix:
    bullets with no plain-English reason, a percentage with no real scale behind it, or
    an opening verb that would be just as true of a different job.
    """
    usage = _bullet_usage()
    totals = {"bullets": len(bank), "no_why": 0, "pct_no_scale": 0, "vague": 0,
              "achievements": 0}
    flagged = {}
    for bid, b in bank.items():
        text, flags = b["text"], []
        if not WHY_RE.search(text):
            flags.append("no WHY")
            totals["no_why"] += 1
        if PCT_RE.search(text) and not SCALE_RE.search(text):
            flags.append("% without scale")
            totals["pct_no_scale"] += 1
        if VAGUE_RE.match(text):
            flags.append("vague verb")
            totals["vague"] += 1
        totals["achievements"] += b["block"] == "achievements"
        if flags:
            flagged.setdefault((b["tpl"].kind, b["block"]), []).append(
                (usage.get(text, 0), len(flags), bid, text, flags))

    n = max(1, totals["bullets"])

    def share(k):
        return f"{100 * totals[k] // n}%"

    lines = [
        "# Bank audit — WHAT / HOW / WHY / WHERE", "",
        "A recruiter counts a qualification only when a bullet says what was used (their "
        "words), how, why in plain English, and where. The tailor already enforces WHAT and "
        "WHERE. The rest are facts only you have — so this is the list of bullets to "
        "rewrite in the master templates, most-printed first.", "",
        f"_generated {datetime.now():%Y-%m-%d %H:%M} from {totals['bullets']} bullets_", "",
        "| Check | Bullets failing | Share |", "|---|---:|---:|",
        f"| No plain-English reason (WHY) | {totals['no_why']} | {share('no_why')} |",
        f"| Percentage with no real scale behind it | {totals['pct_no_scale']} | "
        f"{share('pct_no_scale')} |",
        f"| Opens with a verb that proves nothing | {totals['vague']} | {share('vague')} |", "",
        f"{totals['achievements']} bullets live in Key Achievements, which names no employer. "
        f"A term proven only there reads as a claim — make sure the same work also appears "
        f"under a job.", "",
        "**A passing bullet:** *Built REST APIs in Python with FastAPI, PostgreSQL and AWS so "
        "customers could schedule their own email briefings instead of asking our team to "
        "pull the data by hand.* Their words, how, a reason a non-engineer understands, "
        "inside a real job.", "",
    ]
    for (kind, block), rows in sorted(flagged.items(),
                                      key=lambda kv: (kv[0][0], kv[0][1] != "achievements",
                                                      kv[0][1])):
        rows.sort(key=lambda r: (-r[0], -r[1], r[2]))
        lines += [f"## {kind} — {block.title()} ({len(rows)})", "",
                  "| Used | Bullet | Missing |", "|---:|---|---|"]
        lines += [f"| {used} | {text.replace('|', '/')} | {', '.join(flags)} |"
                  for used, _, _, text, flags in rows]
        lines.append("")
    return "\n".join(lines), totals


# ---------------------------------------------------------------- entry points
def run(cfg, con, templates, bank, skills, model, jobs, notify=True, min_score=None):
    ensure_schema(con)
    if min_score is None:
        min_score = (cfg or {}).get("min_match_score")
    done = 0
    for job in jobs:
        if not (job.get("description") or "").strip():
            job["description"] = fetch_missing_description(cfg, job)
            if job["description"]:
                con.execute("UPDATE jobs SET description=? WHERE company=? AND job_id=?",
                            (job["description"], job["company"], job["job_id"]))
                con.commit()
        try:
            res = tailor_job(job, cfg, templates, bank, skills, model, notify=notify,
                             min_score=min_score)
        except Exception as e:
            log.error("[%s] tailoring failed: %s", job.get("company"), e)
            continue
        if res:
            mark = res["folder"] if res.get("passed") else f"skipped: score {res['score']}"
            con.execute("UPDATE jobs SET tailored=?, match_score=? WHERE company=? AND job_id=?",
                        (mark, res.get("score"), job["company"], job["job_id"]))
            con.commit()
            done += res.get("passed", True)
    return done


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser(add_help=True)
    ap.add_argument("--fit", action="store_true",
                    help="show the one-page budget and the geometry behind it")
    ap.add_argument("--audit", action="store_true",
                    help="write BANK_AUDIT.md: bullets missing a WHY, scale or concrete verb")
    ap.add_argument("--scores", action="store_true",
                    help="recruiter vs semantic score distribution, to tune the bar")
    ap.add_argument("--run", action="store_true", help="tailor all untailored matches")
    ap.add_argument("--job", type=int, help="tailor one job by # from --list")
    ap.add_argument("--url", help="tailor an arbitrary posting URL")
    ap.add_argument("--jd-file", help="tailor from a JD saved in a text file")
    ap.add_argument("--title", default="Software Engineer")
    ap.add_argument("--company", default="Manual")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--gaps", action="store_true", help="print the pooled keyword gaps")
    ap.add_argument("--limit", type=int, default=10)
    ap.add_argument("--min-score", type=int, help="override tailor.min_match_score")
    ap.add_argument("--model", default=os.environ.get("TAILOR_MODEL", DEFAULT_MODEL))
    ap.add_argument("--no-notify", action="store_true")
    ap.add_argument("--away", action="store_true",
                    help="show away days and test writing to iCloud Drive")
    a = ap.parse_args()
    if a.away:
        print(away_status())
        return

    with open(watcher.CONFIG_PATH) as f:
        cfg = yaml.safe_load(f)
    tcfg = apply_config(cfg)
    if not a.model or a.model == DEFAULT_MODEL:
        a.model = tcfg.get("model", a.model)
    con = watcher.db_connect()
    ensure_schema(con)

    if a.gaps:
        json_path, md_path = _gap_paths()
        try:
            with open(json_path) as f:
                data = json.load(f)
        except Exception:
            print("No gaps recorded yet - tailor some jobs first.")
            return
        rows = sorted(data.values(), key=lambda e: (-e["count"], e["keyword"].lower()))
        print(f"{len(rows)} keywords missing from the bank, by how often they are asked for:\n")
        for e in rows[:40]:
            tpl = max(e["templates"], key=e["templates"].get) if e.get("templates") else "-"
            print(f"  {e['count']:>3}x  {e['keyword'][:48]:<50} ({tpl})")
        print(f"\nfull report: {md_path}")
        return
    if a.scores:
        print(score_summary())
        return
    if a.list:
        for r in db_jobs(con, "matched=1 ORDER BY first_seen DESC LIMIT ?", (60,)):
            state = "done" if r["tailored"] else ("ready" if r["description"] else "no JD")
            print(f"#{r['rowid']:<5} {state:<6} {r['company']:<18} {r['title'][:66]}")
        return

    templates = resume_bank.load_templates()
    bank, skills = build_bank(templates)
    log.info("bullet bank: %d bullets from %d templates", len(bank), len(templates))
    notify = not a.no_notify

    if a.jd_file or a.url:
        jd = ""
        if a.jd_file:
            jd = open(a.jd_file).read()
        else:
            jd = watcher.strip_html(watcher._request("GET", a.url, parse_json=False,
                                                     headers=watcher.BROWSER_HEADERS))
        job = {"company": a.company, "job_id": safe(a.url or a.jd_file, 20), "title": a.title,
               "location": "", "url": a.url or "", "description": jd}
        tailor_job(job, cfg, templates, bank, skills, a.model, notify=notify)
        return

    if a.job:
        jobs = db_jobs(con, "rowid=?", (a.job,))
    elif a.fit:
        for sp in sorted({LINE_SPACING, 1.0, 1.15}):
            n, d = fit_job_bullets(templates, sp, COMPACT, ACHIEVEMENTS_CAP, PAGE_LINES)
            caps = budget_for(templates[0], n, ACHIEVEMENTS_CAP)
            mark = "  <- current" if abs(sp - LINE_SPACING) < 1e-9 else ""
            print(f"\nline_spacing {sp}{mark}")
            print(f"  page holds ~{d['lines_per_page']} lines of {d['chars_per_line']} chars; "
                  f"{d['furniture']} go to headers/skills, {SAFETY_LINES} held back as slack")
            print(f"  tightest template: {d['template']}  "
                  f"({d['lines_per_bullet']} line(s) per bullet at the 75th-percentile length)")
            print(f"  budget: " + " + ".join(f"{k}={v}" for k, v in caps.items())
                  + f"  = {sum(caps.values())} bullets")
        print("\nThis is an estimate from the template's geometry, not a render. Open a "
              "generated .docx;\nif space is left over raise page_lines, if it spills "
              "onto page two lower it.")
        return
    elif a.audit:
        text, totals = bank_audit(bank)
        path = os.path.join(OUT_DIR, GAP_DIR_NAME, "BANK_AUDIT.md")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(text)
        n = totals["bullets"]
        print(f"{n} bullets audited:")
        print(f"  {n - totals['no_why']:>4} say WHY in plain English   "
              f"({totals['no_why']} don't)")
        print(f"  {totals['pct_no_scale']:>4} use a percentage with no real scale")
        print(f"  {totals['vague']:>4} open with a verb that proves nothing")
        print(f"\nworklist, most-printed bullets first: {path}")
        return
    elif a.run:
        jobs = db_jobs(con, "matched=1 AND (tailored IS NULL OR tailored='') "
                            "ORDER BY first_seen DESC LIMIT ?", (a.limit,))
    else:
        print(__doc__)
        return
    n = run(cfg, con, templates, bank, skills, a.model, jobs, notify=notify,
            min_score=a.min_score if a.min_score is not None else tcfg.get("min_match_score"))
    log.info("tailored %d job(s) into ./%s/", n, OUT_DIR)


if __name__ == "__main__":
    main()