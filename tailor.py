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

# One-page budget: how many bullets survive in each block. 19 bullets is the most that
# fits on one page at 1.15 line spacing across all four templates (cloud_devops has the
# longest bullets and sets the limit). Override in config.yaml under `tailor:
# page_budget:` - and re-check the page count if you raise it or change line_spacing.
PAGE_BUDGET = {"achievements": 5, "community dreams foundation": 8,
               "medical informatics engineering": 7, "accenture": 6}
DEFAULT_JOB_BUDGET = 5
COMPACT = True
LINE_SPACING = 1.15


def apply_config(cfg):
    """Let config.yaml override the budget, model and compact layout."""
    global PAGE_BUDGET, DEFAULT_JOB_BUDGET, COMPACT, OUT_DIR, MAX_TOKENS, LINE_SPACING
    t = (cfg or {}).get("tailor") or {}
    PAGE_BUDGET.update({k.lower(): int(v) for k, v in (t.get("page_budget") or {}).items()})
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
    """All bullets from all templates, with stable ids: '<kind>:<paragraph idx>'."""
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
    blocks, used, dropped = {}, {}, []

    for block, ids in (plan.get("selected") or {}).items():
        cap = PAGE_BUDGET.get(block, DEFAULT_JOB_BUDGET)
        nodes, texts, seen = [], [], set()
        for bid in ids:
            b = bank.get(bid)
            if b is None:                       # id not in the bank — never reaches the document
                dropped.append(bid)
                continue
            if b["block"] != block:
                dropped.append(f"{bid} (wrong block)")
                continue
            if b["text"] in seen:
                dropped.append(f"{bid} (duplicate text)")
                continue
            seen.add(b["text"])
            nodes.append(b["tpl"].bullet_node(b["idx"]))
            texts.append(f"{b['text']}   [{b['tpl'].kind}]")
            if len(nodes) >= cap:
                break
        blocks[block] = nodes
        used[block] = texts

    base.render(blocks, out_docx, skill_text=build_skill_text(base, plan.get("extra_skill_terms") or []),
                compact=COMPACT, line_spacing=LINE_SPACING)
    return base, used, dropped


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
def write_report(path, job, plan, used, dropped, base):
    score = plan.get("match_score")
    bar = "#" * int(round((score or 0) / 5)) + "." * (20 - int(round((score or 0) / 5)))
    lines = [
        f"# {job['title']}", f"**{job['company']}** · {job.get('location') or 'n/a'}",
        f"{job.get('url') or ''}", "",
        f"## Match: {score}/100  `{bar}`", "",
        f"**Base template:** {base.kind}  — {plan.get('base_reason','')}", "",
        f"**Role:** {plan.get('role_summary','')}", "",
        f"**Verdict:** {plan.get('fit_assessment','')}", "",
        "## Keywords covered", "",
        ", ".join(plan.get("matched_keywords") or []) or "_none_", "",
        "## Gaps — nothing in your templates evidences these", "",
    ]
    miss = plan.get("missing_keywords") or []
    lines += (["\n".join(f"- {m}" for m in miss)] if miss else ["_none_"])
    lines += ["", "## Bullets used", ""]
    for block, items in used.items():
        lines.append(f"**{block}**")
        lines += [f"{i}. {t}" for i, t in enumerate(items, 1)] + [""]
    if plan.get("selection_notes"):
        lines += [f"_{plan['selection_notes']}_", ""]
    if dropped:
        lines += ["## Discarded ids (not in your bank)", "", ", ".join(dropped), ""]
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

    if min_score is not None and (score is None or score < min_score):
        base_kind = plan.get("base_template") or "?"
        record_gaps(plan, job, base_kind)
        log.info("[%s] %s - score %s below %s, no resume built", job["company"],
                 job["title"][:60], score, min_score)
        return {"folder": None, "score": score, "plan": plan, "passed": False}

    folder = os.path.join(OUT_DIR, f"{safe(job['company'],24)}__{safe(job['title'],40)}__{safe(str(job.get('job_id','')),14)}")
    os.makedirs(folder, exist_ok=True)
    docx = os.path.join(folder, f"KALYANKUMAR_KONDURU_{safe(job['company'],20).upper()}.docx")
    base, used, dropped = assemble(plan, bank, templates, docx)
    write_report(os.path.join(folder, "match_report.md"), job, plan, used, dropped, base)
    with open(os.path.join(folder, "job_description.txt"), "w") as f:
        f.write(f"{job['title']}\n{job['company']}\n{job.get('url','')}\n\n{jd}")

    gaps = record_gaps(plan, job, base.kind)
    log.info("[%s] %s -> %s (score %s, base %s)", job["company"], job["title"], folder,
             plan.get("match_score"), base.kind)
    if gaps:
        log.info("  %d gap keyword(s) pooled into %s",
                 len(plan.get("missing_keywords") or []), gaps)
    if notify:
        notify_discord(job, plan, folder)
    return {"folder": folder, "score": score, "plan": plan, "passed": True}


def notify_discord(job, plan, folder):
    hook = os.environ.get("DISCORD_WEBHOOK_URL")
    if not hook:
        return
    score = plan.get("match_score") or 0
    color = 0x43B581 if score >= 75 else (0xFAA61A if score >= 55 else 0xED4245)
    miss = ", ".join((plan.get("missing_keywords") or [])[:12]) or "none"
    embed = {"title": f"📄 Resume ready — {score}/100"[:250], "url": job.get("url") or None,
             "description": (plan.get("fit_assessment") or "")[:600], "color": color,
             "fields": [
                 {"name": "Role", "value": job["title"][:250], "inline": False},
                 {"name": "Base template", "value": plan.get("base_template", "?"), "inline": True},
                 {"name": "Folder", "value": f"`{folder}`"[:1000], "inline": False},
                 {"name": "Gaps", "value": miss[:1000], "inline": False}]}
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