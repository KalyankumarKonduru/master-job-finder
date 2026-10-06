#!/usr/bin/env python3
"""
tailor - turns a job posting into a tailored one-page resume built ONLY from
bullets that already exist in your master templates.

  python tailor.py --run                  # tailor every new matched job (watcher calls this)
  python tailor.py --job 42               # tailor one job by # from `tailor.py --list`
  python tailor.py --url <job url>        # tailor any posting, even one not in the DB
  python tailor.py --jd-file jd.txt       # tailor from a pasted JD saved to a file
  python tailor.py --list                 # matched jobs and their tailor status

Output per job, under ./applications/<Company>__<Title>__<id>/ :
  <NAME>_<Company>.docx   the tailored resume
  match_report.md         score, matched keywords, gaps, and which bullets were used
  job_description.txt     the JD it was built from

How the resume is produced:
  Claude reads the JD and CHOOSES bullet IDs from your bank. It never writes bullet
  text. Any ID it returns that isn't in the bank is discarded. The .docx is your own
  template with unselected paragraphs removed, so formatting and wording are yours.

Env: ANTHROPIC_API_KEY (required), DISCORD_WEBHOOK_URL / TELEGRAM_* (optional)
"""
import argparse
import json
import logging
import os
import re
import sqlite3
import sys
import textwrap
from datetime import datetime

import requests
import yaml

import resume_bank
import watcher

log = logging.getLogger("tailor")

HERE = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.environ.get("JOBWATCH_APPLICATIONS") or os.path.join(HERE, "applications")
DEFAULT_MODEL = "claude-sonnet-5"
MAX_JD_CHARS = 18000
MAX_TOKENS = 16000

# Bullets kept per block. 19 is the most that fits on ONE page at 1.15 line spacing
# across all four templates (cloud_devops has the longest bullets and sets the limit).
# The defaults below sum to 26, so they intentionally allow a second page; drop to
# 4/6/5/4 to force one. Override in config.yaml under `tailor: page_budget:` - and
# re-check the rendered page count whenever you raise these or change line_spacing.
# A block's cap is enforced in assemble() after Claude has already scored its
# selection, which is why verify_resume() re-scores the bullets that survive.
PAGE_BUDGET = {}            # resolved per run - see resolve_budget()
ACHIEVEMENTS_CAP = 5        # the one fixed block
JOB_BULLETS = "auto"        # "auto" = fit the page; an int pins the first job at n
PAGE_LINES = None           # override the estimated lines per page
EXPLICIT_BLOCKS = set()     # blocks pinned by name in config.yaml, never auto-fitted
DEFAULT_JOB_BUDGET = 5
COMPACT = True
LINE_SPACING = 1.15


def apply_config(cfg):
    """Let config.yaml override the budget, model and compact layout."""
    global PAGE_BUDGET, DEFAULT_JOB_BUDGET, COMPACT, OUT_DIR, MAX_TOKENS, LINE_SPACING
    global ACHIEVEMENTS_CAP, JOB_BULLETS, PAGE_LINES, EXPLICIT_BLOCKS
    t = (cfg or {}).get("tailor") or {}
    pb = {str(k).lower(): v for k, v in (t.get("page_budget") or {}).items()}
    ACHIEVEMENTS_CAP = int(pb.pop("achievements", ACHIEVEMENTS_CAP))
    JOB_BULLETS = pb.pop("job_bullets", JOB_BULLETS)
    pl = pb.pop("page_lines", None)
    PAGE_LINES = int(pl) if pl else None
    EXPLICIT_BLOCKS = set(pb)          # a block named outright still wins
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

You are given a job description and a bank of bullets, grouped into blocks. Every bullet \
belongs to exactly one block (the key achievements block, or one employer). Bullets carry \
a template tag (backend / cloud_devops / frontend / fullstack); you may mix tags freely \
inside a block if that better matches the job.

Return ONLY a JSON object, no prose, no code fences:
{
  "base_template": "backend|cloud_devops|frontend|fullstack",
  "base_reason": "one sentence on why this template's skills section and framing fit best",
  "role_summary": "one sentence on what this job actually is",
  "must_have_keywords": ["the concrete skills/tools/practices the posting requires"],
  "selected": {"<block name>": ["<bullet id>", ...]},
  "selection_notes": "one or two sentences on the ordering logic",
  "extra_skill_terms": ["skill terms to append to the base template's SKILLS line"],
  "matched_keywords": ["required keywords genuinely evidenced by the selected bullets"],
  "missing_keywords": ["required keywords NOT evidenced anywhere in the bank"],
  "match_score": 0-100,
  "fit_assessment": "2-3 sentences: is this worth applying to, and what is the weakest point"
}

Rules:
- An id may ONLY be listed under the block it appears beneath in the bank. A bullet belongs to one employer; it cannot be moved to another. Listing backend:150 under a block other than its own is an error, not a reordering.
- Respect each block's stated cap. Order ids strongest-match first; that is the order they print.
- Prefer a bullet naming the exact tool in the posting over a generic one.
- Avoid near-duplicate bullets; each one should add new evidence.
- extra_skill_terms may ONLY contain terms that appear in the SKILLS lines you are shown.
- match_score is the share of must_have_keywords truly covered by selected bullets. Be strict: \
do not count a keyword as matched because it is adjacent or similar. An honest 60 is more \
useful than an inflated 95.
- missing_keywords drives the candidate's decision, so list every real gap."""


def _api_call(body, key, timeout=180):
    r = requests.post("https://api.anthropic.com/v1/messages",
                      headers={"x-api-key": key, "anthropic-version": "2023-06-01",
                               "content-type": "application/json"},
                      json=body, timeout=timeout)
    if r.status_code >= 300:
        raise RuntimeError(f"Claude API {r.status_code}: {r.text[:400]}")
    return r.json()


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


# ---------------------------------------------------------------- assembly
def assemble(plan, bank, templates, out_docx):
    """Render the .docx. Returns (base, used, dropped) — `used` is block -> [texts]
    in the order they appear in the document."""
    base = next((t for t in templates if t.kind == plan.get("base_template")), templates[0])
    blocks, used, dropped, rehomed = {}, {}, [], []

    # Pass 1 - resolve every id to its TRUE block. A bullet's block is a property of the
    # master template: it is that employer's line, not a slot the model gets to choose.
    # So an id listed under the wrong block is a misfile, and discarding it silently
    # deletes evidence the score already counted. Re-home it instead.
    chosen = {}
    for block, ids in (plan.get("selected") or {}).items():
        for bid in ids:
            b = bank.get(bid)
            if b is None:                       # invented id — nothing to place
                dropped.append(f"{bid} (not in bank)")
                continue
            if b["block"] != block:
                rehomed.append(f"{bid}: filed under '{block}', belongs to '{b['block']}'")
            chosen.setdefault(b["block"], []).append(bid)

    # Pass 2 - drop duplicate text, then apply each block's page_budget cap. Ids the
    # model put in the block itself come first, so a re-homed bullet never displaces
    # one that was chosen for that block deliberately.
    for block, ids in chosen.items():
        cap = PAGE_BUDGET.get(block, DEFAULT_JOB_BUDGET)
        nodes, texts, seen = [], [], set()
        for bid in ids:
            b = bank[bid]
            if b["text"] in seen:
                dropped.append(f"{bid} (duplicate text)")
                continue
            if len(nodes) >= cap:
                dropped.append(f"{bid} (over the {block} cap of {cap})")
                continue
            seen.add(b["text"])
            nodes.append(b["tpl"].bullet_node(b["idx"]))
            texts.append(f"{b['text']}   [{b['tpl'].kind}]")
        blocks[block] = nodes
        used[block] = texts

    base.render(blocks, out_docx, skill_text=build_skill_text(base, plan.get("extra_skill_terms") or []),
                compact=COMPACT, line_spacing=LINE_SPACING)
    return base, used, dropped, rehomed


def build_skill_text(base, extra_terms):
    """Append JD-relevant terms to the base SKILLS lines, skipping ones already there."""
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


# ---------------------------------------------------------------- final check
TPL_TAG = re.compile(r"\s*\[(?:backend|cloud_devops|frontend|fullstack)\]\s*$")


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


def verify_resume(plan, used, base, extra_terms, bank=None):
    """Measure what `assemble` cost the resume, and adjust the score by that much.

    `match_score` is Claude's estimate, made BEFORE the document exists, and it judges
    evidence semantically - a Kafka consumer bullet can evidence "publish/subscribe"
    without containing the words. `assemble` can then drop bullets the estimate counted:
    an id missing from the bank, an id in the wrong block, duplicate text, or anything
    past a block's page_budget cap.

    Matching keywords literally is far too strict to use as an absolute score - phrases
    like "two database technologies" or "on-call rotations" never appear verbatim in a
    bullet. But it IS reliable as a *relative* measure between two sets of the same
    bullets, so that is all it is used for here: literal coverage of the planned
    selection versus literal coverage of what survived. Nothing dropped means the ratio
    is 1 and the score is untouched. Lose half the keyword-bearing bullets and the score
    halves. No API call: `assemble` already returns the surviving text.
    """
    must = [k for k in (plan.get("must_have_keywords") or []) if str(k).strip()]
    planned = plan.get("match_score")
    if not must:
        return None
    doc_hay = " || ".join(_norm_text(TPL_TAG.sub("", t))
                          for items in used.values() for t in items)
    plan_hay = doc_hay
    if bank:                       # every bullet Claude chose, before caps and drops
        chosen = [bank[i]["text"] for ids in (plan.get("selected") or {}).values()
                  for i in ids if i in bank]
        if chosen:
            plan_hay = " || ".join(_norm_text(t) for t in chosen)
    skills = _norm_text(" | ".join(txt for _, txt in base.skill_lines)
                        + " | " + " | ".join(str(t) for t in extra_terms))

    confirmed, skills_only, lost = [], [], []
    for kw in must:
        if _kw_present(kw, doc_hay):
            confirmed.append(str(kw))
        elif _kw_present(kw, skills):
            skills_only.append(str(kw))
        else:
            lost.append(str(kw))
    in_plan = [str(k) for k in must if _kw_present(k, plan_hay)]
    dropped_evidence = [k for k in in_plan if k not in confirmed]

    ratio = (len(confirmed) / len(in_plan)) if in_plan else 1.0
    final = planned if planned is None else int(round(planned * ratio))
    return {"final_score": final, "planned": planned, "ratio": ratio,
            "confirmed": confirmed, "skills_only": skills_only, "lost": lost,
            "dropped_evidence": dropped_evidence,
            "coverage_doc": len(confirmed), "coverage_plan": len(in_plan),
            "bullets": sum(len(v) for v in used.values()), "must_total": len(must)}


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
def write_report(path, job, plan, used, dropped, base, rehomed=None):
    chk = plan.get("verified") or {}
    planned = plan.get("match_score")
    score = chk.get("final_score", planned)
    bar = "#" * int(round((score or 0) / 5)) + "." * (20 - int(round((score or 0) / 5)))
    lines = [
        f"# {job['title']}", f"**{job['company']}** · {job.get('location') or 'n/a'}",
        f"{job.get('url') or ''}", "",
        f"## Match: {score}/100  `{bar}`", "",
    ]
    if chk and chk.get("dropped_evidence"):
        lines += [f"> Planned **{planned}/100**, reduced to **{chk['final_score']}/100**: "
                  f"assembly cut bullets that were the only evidence for "
                  f"{', '.join(chk['dropped_evidence'])}.", ""]
    lines += [
        f"**Base template:** {base.kind}  — {plan.get('base_reason','')}", "",
        f"**Role:** {plan.get('role_summary','')}", "",
        f"**Verdict:** {plan.get('fit_assessment','')}", "",
        "## Keywords covered", "",
        ", ".join(plan.get("matched_keywords") or []) or "_none_", "",
        "## Gaps — nothing in your templates evidences these", "",
    ]
    miss = plan.get("missing_keywords") or []
    lines += (["\n".join(f"- {m}" for m in miss)] if miss else ["_none_"])
    if chk:
        lines += ["", "## Final check against the job description", ""]
        if chk.get("dropped_evidence"):
            lines += [f"`assemble` wrote {chk['bullets']} bullets and cut the only "
                      f"literal evidence for **{len(chk['dropped_evidence'])}** "
                      f"requirement(s), so the score was scaled by "
                      f"{chk['ratio']:.2f}:", "",
                      "- " + "\n- ".join(chk["dropped_evidence"]), ""]
        else:
            lines += [f"`assemble` wrote all {chk['bullets']} selected bullets with "
                      f"nothing cut, so the document matches what was scored and the "
                      f"score stands.", ""]
        lines += [f"_Literal keyword check (diagnostic, not the score): "
                  f"{chk['coverage_doc']} of {chk['must_total']} requirements appear "
                  f"word-for-word in a bullet. This undercounts badly — a Kafka bullet "
                  f"evidences 'publish/subscribe' without the words, and "
                  f"'two database technologies' can never match literally. Use it to "
                  f"spot what is missing, not to judge fit._", ""]
        if chk["skills_only"]:
            lines += ["**Claimed in the SKILLS line only — no bullet proves these:** "
                      + ", ".join(chk["skills_only"]), ""]
        if chk["lost"]:
            lines += ["**Not literally present anywhere on the page** (check these by eye): "
                      + ", ".join(chk["lost"]), ""]
    lines += ["", "## Bullets used", ""]
    for block, items in used.items():
        lines.append(f"**{block}**")
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


# ---------------------------------------------------------------- job sources
def safe(s, n=48):
    return re.sub(r"[^A-Za-z0-9._-]+", "_", s or "").strip("_")[:n] or "job"


def tailor_job(job, cfg, templates, bank, skills, model, notify=True, min_score=None):
    """Score a posting against the bullet bank, and build a resume only if it clears
    `min_score`. Below the bar nothing is written: the score and the keyword gaps are
    still recorded, because a weak match is exactly where the gap list is useful."""
    jd = job.get("description") or ""
    if not jd.strip():
        log.warning("no job description for %s — skipping", job["title"])
        return None
    plan = ask_claude(jd, job["title"], job["company"], bank, skills, model)
    score = plan.get("match_score")

    # Cheap pre-filter. The verified score can only ever be <= the planned score
    # (verify_resume scales by surviving/planned evidence, a ratio of at most 1), so a
    # posting that fails here could never have cleared the bar after assembly either.
    # Nothing that would have passed the real gate below is lost by skipping the build.
    if min_score is not None and (score is None or score < min_score):
        base_kind = plan.get("base_template") or "?"
        record_gaps(plan, job, base_kind)
        log.info("[%s] %s - score %s below %s, no resume built", job["company"],
                 job["title"][:60], score, min_score)
        return {"folder": None, "score": score, "plan": plan, "passed": False}

    folder = os.path.join(OUT_DIR, f"{safe(job['company'],24)}__{safe(job['title'],40)}__{safe(str(job.get('job_id','')),14)}")
    os.makedirs(folder, exist_ok=True)
    docx = os.path.join(folder, f"KALYANKUMAR_KONDURU_{safe(job['company'],20).upper()}.docx")
    base, used, dropped, rehomed = assemble(plan, bank, templates, docx)

    # Final check: score the document that was actually written, not the plan.
    check = verify_resume(plan, used, base, plan.get("extra_skill_terms") or [], bank)
    if check:
        plan["verified"] = check
        final = check["final_score"]
        if check["dropped_evidence"]:
            log.warning("[%s] %s - %s/100 -> %s/100: assembly cut evidence for %s",
                        job["company"], job["title"][:50], score, final,
                        ", ".join(check["dropped_evidence"][:6]))
    else:
        final = score

    write_report(os.path.join(folder, "match_report.md"), job, plan, used, dropped, base,
                 rehomed)
    with open(os.path.join(folder, "job_description.txt"), "w") as f:
        f.write(f"{job['title']}\n{job['company']}\n{job.get('url','')}\n\n{jd}")

    gaps = record_gaps(plan, job, base.kind)

    # The gate that decides whether this is worth telling you about is applied to the
    # score of the DOCUMENT, after assembly - not to the plan that preceded it.
    cleared = min_score is None or final is None or final >= min_score
    if not cleared:
        log.info("[%s] %s -> built but verified %s/100 below %s, no alert sent (%s)",
                 job["company"], job["title"][:50], final, min_score, folder)
        return {"folder": folder, "score": final, "planned_score": score,
                "plan": plan, "passed": False}

    log.info("[%s] %s -> %s (score %s, base %s)", job["company"], job["title"], folder,
             final, base.kind)
    if gaps:
        log.info("  %d gap keyword(s) pooled into %s",
                 len(plan.get("missing_keywords") or []), gaps)
    if notify:
        notify_discord(job, plan, folder)
    return {"folder": folder, "score": final, "planned_score": score,
            "plan": plan, "passed": True}


def notify_discord(job, plan, folder):
    hook = os.environ.get("DISCORD_WEBHOOK_URL")
    if not hook:
        return
    chk = plan.get("verified") or {}
    planned = plan.get("match_score") or 0
    score = chk.get("final_score", planned)
    color = 0x43B581 if score >= 75 else (0xFAA61A if score >= 55 else 0xED4245)
    miss = ", ".join((plan.get("missing_keywords") or [])[:12]) or "none"
    fields = [{"name": "Role", "value": job["title"][:250], "inline": False},
              {"name": "Base template", "value": plan.get("base_template", "?"), "inline": True}]
    if chk:
        if chk.get("dropped_evidence"):
            verdict = (f"{planned} -> **{score}** · assembly cut evidence for "
                       + ", ".join(chk["dropped_evidence"][:6]))
        else:
            verdict = f"**{score}/100** · all {chk['bullets']} selected bullets made the page"
        fields.append({"name": "Final check", "value": verdict[:1000], "inline": False})
        if chk["skills_only"]:
            fields.append({"name": "In SKILLS only, no bullet proof",
                           "value": ", ".join(chk["skills_only"])[:1000], "inline": False})
    fields += [{"name": "Folder", "value": f"`{folder}`"[:1000], "inline": False},
               {"name": "Gaps", "value": miss[:1000], "inline": False}]
    embed = {"title": f"📄 Resume ready — {score}/100"[:250], "url": job.get("url") or None,
             "description": (plan.get("fit_assessment") or "")[:600], "color": color,
             "fields": fields}
    try:
        requests.post(hook, json={"embeds": [embed]}, timeout=15)
    except Exception as e:
        log.warning("discord notify failed: %s", e)


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
    a = ap.parse_args()

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