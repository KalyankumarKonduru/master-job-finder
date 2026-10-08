#!/usr/bin/env python3
"""
jobwatch - polls company career boards, filters by title, and sends new matches
to Discord and/or Telegram. Seen jobs are remembered in SQLite so each posting
alerts once.

Usage:
  python watcher.py                 # run forever, polling every N minutes
  python watcher.py --once          # one poll then exit (for cron)
  python watcher.py --check         # test every company's feed, print samples, write nothing
  python watcher.py --test-notify   # send a test message and exit
  python watcher.py --mark-delivered # clear the alert backlog (run once after upgrading)
  python watcher.py --rescan-sponsorship  # re-screen every stored job description
  python watcher.py --explain 512    # why one job alerted: JD length, sponsorship lines

Env vars (set at least one channel):
  DISCORD_WEBHOOK_URL
  TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
  JOBWATCH_CONFIG (default: config.yaml), JOBWATCH_DB (default: jobs.db)
"""
import html
import logging
import os
import random
import json
import re
import sqlite3
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import requests
import yaml

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("jobwatch")

# Everything found BEFORE this watcher started is backlog and is never tailored. Only
# postings first seen during this run are, which is why the per-cycle cap can throttle
# without dropping: a deferred posting is still >= STARTED_AT on the next cycle.
STARTED_AT = datetime.now(timezone.utc).isoformat(timespec="seconds")

CONFIG_PATH = os.environ.get("JOBWATCH_CONFIG", "config.yaml")
DB_PATH = os.environ.get("JOBWATCH_DB", "jobs.db")
_local = threading.local()


def get_session():
    """One requests.Session per thread (Sessions are not meant to be shared)."""
    if not hasattr(_local, "session"):
        s = requests.Session()
        s.headers.update({
            "User-Agent": "Mozilla/5.0 (personal job-alert script; low frequency)",
            "Accept": "application/json",
        })
        _local.session = s
    return _local.session
REQUEST_GAP = 1.2  # seconds between requests to the same site


# ---------------------------------------------------------------- title filter
def normalize_title(title: str) -> str:
    t = title.lower()
    t = re.sub(r"\bsr\b\.?", "senior", t)
    t = re.sub(r"full[\s\-_/]*stack", "full stack", t)
    t = re.sub(r"[^a-z0-9+#]+", " ", t)
    return re.sub(r"\s+", " ", t).strip()


class TitleFilter:
    def __init__(self, include, exclude):
        self.include = [re.compile(p) for p in include]
        self.exclude = [re.compile(p) for p in exclude]

    def matches(self, title: str) -> bool:
        n = normalize_title(title)
        return any(p.search(n) for p in self.include) and not any(p.search(n) for p in self.exclude)


# ---------------------------------------------------------------- http helpers
def strip_html(s: str) -> str:
    s = re.sub(r"(?i)<br\s*/?>|</p>|</li>|</h\d>|</div>", "\n", s or "")
    s = re.sub(r"(?i)<li[^>]*>", "- ", s)
    s = re.sub(r"<[^>]+>", "", s)
    s = html.unescape(s)
    return re.sub(r"\n\s*\n+", "\n\n", s).strip()


def _request(method, url, retries=3, parse_json=True, **kw):
    for attempt in range(retries):
        try:
            r = get_session().request(method, url, timeout=30, **kw)
            if r.status_code == 429 or r.status_code >= 500:
                raise requests.HTTPError(f"HTTP {r.status_code}")
            r.raise_for_status()
            return r.json() if parse_json else r.text
        except Exception as e:
            if attempt == retries - 1:
                raise RuntimeError(f"{method} {url} failed: {e}")
            wait = 5 * (attempt + 1)
            log.warning("%s failed (%s), retry in %ss", method, e, wait)
            time.sleep(wait)


def get_json(url, params=None, headers=None):
    return _request("GET", url, params=params, headers=headers)


BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
}


def src_headers(src, extra=None):
    """Browser-ish headers plus anything set under `headers:` in config."""
    h = dict(BROWSER_HEADERS)
    if extra:
        h.update(extra)
    h.update(src.get("headers") or {})
    return h


def post_json(url, body):
    return _request("POST", url, json=body, headers={"Content-Type": "application/json"})


# ---------------------------------------------------------------- Oracle Recruiting Cloud
def _oracle_api(src, resource):
    return f"https://{src['host']}/hcmRestApi/resources/latest/{resource}"


def _oracle_find(src, extra):
    finder = [f"siteNumber={src['site']}", "facetsList=LOCATIONS"] + extra
    params = {
        "onlyData": "true",
        "expand": "requisitionList.secondaryLocations",
        "finder": "findReqs;" + ",".join(finder),
    }
    data = get_json(_oracle_api(src, "recruitingCEJobRequisitions"), params)
    items = data.get("items") or []
    return items[0] if items else {}


def oracle_location_id(src):
    """Use location_id from config, or look up the ID for `country` once."""
    if src.get("location_id") or not src.get("country"):
        return src.get("location_id")
    if "_location_id" not in src:
        src["_location_id"] = None
        facets = _oracle_find(src, ["limit=1"]).get("locationsFacet") or []
        want = src["country"].lower()
        for f in facets:
            if str(f.get("Name", "")).lower() == want:
                src["_location_id"] = str(f.get("Id"))
                break
        if src["_location_id"]:
            log.info("[%s] resolved country '%s' -> locationId %s", src["name"], src["country"], src["_location_id"])
        else:
            names = ", ".join(str(f.get("Name")) for f in facets[:15])
            log.warning("[%s] country '%s' not found in location facets (saw: %s). Not filtering by location.",
                        src["name"], src["country"], names)
        time.sleep(REQUEST_GAP)
    return src["_location_id"]


def oracle_list(src):
    """Yield newest postings from an Oracle Candidate Experience site."""
    limit = 25
    loc = oracle_location_id(src)
    apply_base = src.get("apply_url_base") or \
        f"https://{src['host']}/hcmUI/CandidateExperience/en/sites/{src['site']}/job/"
    for page in range(int(src.get("pages", 2))):
        extra = [f"limit={limit}", f"offset={page * limit}", "sortBy=POSTING_DATES_DESC"]
        if loc:
            extra.append(f"locationId={loc}")
        if src.get("keyword"):
            extra.append(f'keyword="{src["keyword"]}"')
        reqs = _oracle_find(src, extra).get("requisitionList") or []
        for r in reqs:
            job_id = str(r.get("Id"))
            yield {
                "job_id": job_id,
                "title": (r.get("Title") or "").strip(),
                "location": r.get("PrimaryLocation") or "",
                "workplace": r.get("WorkplaceType") or "",
                "posted": r.get("PostedDate") or "",
                "url": apply_base + job_id,
                "summary": strip_html(r.get("ShortDescriptionStr") or ""),
            }
        if len(reqs) < limit:
            break
        time.sleep(REQUEST_GAP)


# Oracle scatters a posting across many fields, and the visa / sponsorship boilerplate
# often sits in one of the trailing ones rather than the main description.
ORACLE_PRIMARY = ("ExternalDescriptionStr", "ExternalResponsibilitiesStr",
                  "ExternalQualificationsStr")
ORACLE_TEXT_KEY = re.compile(
    r"(?i)description|qualification|responsibilit|requirement|corporate|additional|"
    r"benefit|information|disclaimer|eeo|note|comment|posting|legal|skill|detail")
# Keys that are plainly metadata, never prose.
TEXT_KEY_SKIP = re.compile(
    r"(?i)^(id|.*url|.*date|.*time|.*code|.*number|.*flag|.*id|lang|locale|currency|"
    r"latitude|longitude|.*Id|.*Name|siteNumber|.*Status)$")


def collect_long_text(node, min_len=60, depth=0, out=None, limit=400):
    """Every prose-looking string anywhere in a payload - the whole posting, not a
    hand-picked set of fields. Visa clauses and preferred qualifications routinely
    live in fields nobody thought to list."""
    out = {} if out is None else out
    if len(out) >= limit:
        return out
    if isinstance(node, dict):
        for k, v in node.items():
            if isinstance(v, str):
                looks_prose = len(v.strip()) >= min_len and (" " in v or "<" in v)
                if looks_prose and not TEXT_KEY_SKIP.match(k):
                    out.setdefault(k, v)
            elif depth < 4 and isinstance(v, (dict, list)):
                collect_long_text(v, min_len, depth + 1, out, limit)
    elif isinstance(node, list):
        for v in node[:40]:
            collect_long_text(v, min_len, depth + 1, out, limit)
    return out


def oracle_description(src, job_id):
    params = {
        "expand": "all",
        "onlyData": "true",
        "finder": f'ById;Id="{job_id}",siteNumber={src["site"]}',
    }
    data = get_json(_oracle_api(src, "recruitingCEJobRequisitionDetails"), params)
    items = data.get("items") or []
    if not items:
        return ""
    d = items[0]
    fields = collect_long_text(d, min_len=30)
    for k in ORACLE_PRIMARY:                       # keep these even when short
        if isinstance(d.get(k), str) and d[k].strip():
            fields.setdefault(k, d[k])
    named = sorted(k for k in fields if k not in ORACLE_PRIMARY and ORACLE_TEXT_KEY.search(k))
    rest = sorted(k for k in fields if k not in ORACLE_PRIMARY and k not in named)
    ordered = [k for k in ORACLE_PRIMARY if k in fields] + named + rest
    parts, seen = [], set()
    for k in ordered:
        txt = strip_html(fields[k])
        key = txt[:120]
        if txt and key not in seen:
            seen.add(key)
            parts.append(txt)
    return "\n\n".join(parts)


# ---------------------------------------------------------------- Workday
def _wd_api(src):
    """API root. Either tenant/pod/site, or an explicit api_base (myworkdaysite hosts)."""
    if src.get("api_base"):
        return src["api_base"].rstrip("/")
    return f"https://{src['tenant']}.{src['pod']}.myworkdayjobs.com/wday/cxs/{src['tenant']}/{src['site']}"


def _wd_view(src):
    if src.get("view_base"):
        return src["view_base"].rstrip("/")
    return f"https://{src['tenant']}.{src['pod']}.myworkdayjobs.com/en-US/{src['site']}"


COUNTRY_ALIASES = {
    "united states of america": ["united states of america", "united states", "usa", "u.s.a.",
                                 "us", "u.s.", "united states of america (usa)"],
}


def _wd_walk_facets(facets, parent=None):
    """Yield (facetParameter, id, label) for every facet value, at any depth."""
    for f in facets or []:
        param = f.get("facetParameter") or parent
        for v in f.get("values") or []:
            if v.get("id"):
                yield param, v["id"], str(v.get("descriptor", ""))
            if v.get("values"):
                yield from _wd_walk_facets([v], param)


# Only a country-level facet may be used. Boards also expose a flat `locations` list of
# individual offices and a `hiringCompany` list, and matching "United States" against
# those picks one city (or one legal entity) and silently hides the rest of the board.
COUNTRY_PARAM = re.compile(r"(?i)country|nation")


def _wd_find_facet(facets, name):
    """Find (facetParameter, id) for a country facet, tolerating spelling variants."""
    wanted = [name.lower()] + COUNTRY_ALIASES.get(name.lower(), [])
    rows = [r for r in _wd_walk_facets(facets) if COUNTRY_PARAM.search(r[0] or "")]
    for alias in wanted:
        for param, fid, label in rows:
            if label.lower() == alias:
                return param, fid
    for alias in wanted:
        for param, fid, label in rows:
            if alias in label.lower() and len(label) < 40:
                return param, fid
    return None


def workday_facets(src):
    """Resolve `country` into Workday's facet filter once; cached on the source.
    An explicit `facets:` mapping in config is used as-is (the ids are visible in the
    board's own URL, e.g. locationCountry=bc33aa...)."""
    if "_facets" in src:
        return src["_facets"]
    if src.get("facets"):
        src["_facets"] = {k: (v if isinstance(v, list) else [v]) for k, v in src["facets"].items()}
        return src["_facets"]
    src["_facets"] = {}
    if src.get("country"):
        data = post_json(_wd_api(src) + "/jobs",
                         {"appliedFacets": {}, "limit": 1, "offset": 0, "searchText": ""})
        hit = _wd_find_facet(data.get("facets"), src["country"])
        if hit:
            src["_facets"] = {hit[0]: [hit[1]]}
            log.info("[%s] resolved country '%s' -> %s=%s", src["name"], src["country"], hit[0], hit[1])
        else:
            params = sorted({p for p, _, _ in _wd_walk_facets(data.get("facets")) if p})
            log.info("[%s] no country facet on this board (has: %s) - using location_regex",
                     src["name"], ", ".join(params[:8]) or "none")
            if not src.get("location_regex"):
                log.warning("[%s] and no location_regex is set, so non-US postings will "
                            "come through", src["name"])
        time.sleep(REQUEST_GAP)
    return src["_facets"]


def workday_list(src):
    """Yield postings for each search text. Workday caps pages at 20 results."""
    limit = 20
    facets = workday_facets(src)
    seen = set()
    for text in src.get("search_texts") or [""]:
        for page in range(int(src.get("pages", 3))):
            body = {"appliedFacets": facets, "limit": limit, "offset": page * limit, "searchText": text}
            posts = post_json(_wd_api(src) + "/jobs", body).get("jobPostings") or []
            for p in posts:
                path = p.get("externalPath")
                if not path or path in seen:
                    continue
                seen.add(path)
                yield {
                    "job_id": path,
                    "title": (p.get("title") or "").strip(),
                    "location": p.get("locationsText") or "",
                    "workplace": p.get("remoteType") or "",
                    "posted": p.get("postedOn") or "",
                    "url": _wd_view(src) + path,
                    "summary": "",
                }
            time.sleep(REQUEST_GAP)
            if len(posts) < limit:
                break


def workday_description(src, job_id):
    data = get_json(_wd_api(src) + job_id, headers=src_headers(
        src, {"Referer": _wd_view(src) + job_id}))
    info = data.get("jobPostingInfo") or {}
    return strip_html(info.get("jobDescription") or "")



# ---------------------------------------------------------------- Greenhouse
def greenhouse_list(src):
    """Greenhouse public boards API - full board in one call, newest first."""
    data = get_json(f"https://boards-api.greenhouse.io/v1/boards/{src['slug']}/jobs", {"content": "true"})
    jobs = data.get("jobs") or []
    jobs.sort(key=lambda j: j.get("updated_at") or "", reverse=True)
    for j in jobs:
        yield {
            "job_id": str(j.get("id")),
            "title": (j.get("title") or "").strip(),
            "location": ((j.get("location") or {}).get("name") or ""),
            "workplace": "",
            "posted": (j.get("first_published") or j.get("updated_at") or "")[:10],
            "url": j.get("absolute_url") or "",
            "summary": "",
        }


def greenhouse_description(src, job_id):
    j = get_json(f"https://boards-api.greenhouse.io/v1/boards/{src['slug']}/jobs/{job_id}")
    return strip_html(html.unescape(j.get("content") or ""))


# ---------------------------------------------------------------- Eightfold
def eightfold_list(src):
    """Eightfold career portals (HSBC, PayPal, ...)."""
    api = f"https://{src['host']}/api/apply/v2/jobs"
    per = 10
    for text in src.get("search_texts") or [""]:
        for page in range(int(src.get("pages", 3))):
            params = {"domain": src["domain"], "start": page * per, "num": per,
                      "sort_by": src.get("sort_by", "timestamp")}
            if text:
                params["query"] = text
            if src.get("location"):
                params["location"] = src["location"]
                params["filter_distance"] = src.get("distance", 5000)
            if src.get("include_remote", True):
                params["filter_include_remote"] = 1
            headers = src_headers(src, {"Referer": f"https://{src['host']}/careers", "Origin": f"https://{src['host']}"})
            data = get_json(api, params, headers=headers)
            positions = data.get("positions") or []
            for p in positions:
                pid = str(p.get("id") or p.get("display_job_id") or "")
                locs = p.get("locations") or ([p.get("location")] if p.get("location") else [])
                yield {
                    "job_id": pid,
                    "title": (p.get("name") or "").strip(),
                    "location": ", ".join([l for l in locs if l])[:200],
                    "workplace": "remote" if p.get("work_location_option") == "remote" else "",
                    "posted": str(p.get("t_update") or ""),
                    "url": p.get("canonicalPositionUrl") or
                           f"https://{src['host']}/careers?pid={pid}&domain={src['domain']}",
                    "summary": "",
                }
            time.sleep(REQUEST_GAP)
            if len(positions) < per:
                break


def eightfold_description(src, job_id):
    data = get_json(f"https://{src['host']}/api/apply/v2/jobs/{job_id}", {"domain": src["domain"]},
                    headers=src_headers(src, {"Referer": f"https://{src['host']}/careers"}))
    body = data.get("job_description") or data.get("positionDescription") or ""
    if not body and data.get("positions"):
        body = (data["positions"][0] or {}).get("job_description", "")
    return strip_html(html.unescape(body))


# ---------------------------------------------------------------- Radancy (TalentBrew)
JOB_LINK_RE = re.compile(
    r'<a[^>]*?(?:href="(?P<href>[^"]+)")[^>]*?>(?P<inner>.*?)</a>', re.S | re.I)
JOB_HREF_HINT = re.compile(r"/job[s]?/", re.I)
H2_RE = re.compile(r"<h2[^>]*>(.*?)</h2>", re.S | re.I)
SPAN_RE = re.compile(
    r'<(?:span|div|p|li)[^>]*class="[^"]*\b(?P<kind>job-?location|location|job-?date-?posted|'
    r'date-?posted|posted-?date|job-?info)\b[^"]*"[^>]*>(?P<val>.*?)</(?:span|div|p|li)>',
    re.S | re.I)
# /job/<city>/<title-slug>/<org>/<id> - the city is in the path when the markup hides it
HREF_CITY = re.compile(r"/job/([^/]+)/", re.I)


def radancy_list(src):
    """Radancy/TalentBrew boards (Barclays, Citi's public site, ...). Parses the search HTML."""
    base = src["base_url"].rstrip("/")
    for page in range(1, int(src.get("pages", 2)) + 1):
        params = dict(src.get("query") or {})
        params.update({"CurrentPage": page, "RecordsPerPage": src.get("per_page", 25),
                       "SortCriteria": src.get("sort", 1), "SortDirection": 1, "IsPagination": "True"})
        markup, how = "", ""
        try:
            raw = _request("GET", f"{base}/search-jobs/results", params=params,
                           parse_json=False, headers=src_headers(src), retries=1)
            try:
                markup = json.loads(raw).get("results", "")
            except ValueError:
                markup = raw
            how = "results endpoint"
        except Exception as e:
            log.debug("[%s] results endpoint failed: %s", src.get("name"), e)
        if not JOB_HREF_HINT.search(markup or "") and src.get("search_url"):
            sep = "&" if "?" in src["search_url"] else "?"
            markup = _request("GET", f"{src['search_url']}{sep}p={page}",
                              parse_json=False, headers=src_headers(src))
            how = "search page"
        count = 0
        for m in JOB_LINK_RE.finditer(markup or ""):
            inner, href = m.group("inner"), m.group("href") or ""
            if not JOB_HREF_HINT.search(href):
                continue
            title_m = H2_RE.search(inner)
            title = strip_html(title_m.group(1)) if title_m else strip_html(inner).split("\n")[0]
            if not title:
                continue
            spans = {}
            for mm in SPAN_RE.finditer(inner):
                kind = re.sub(r"[^a-z]", "", mm.group("kind").lower())
                spans.setdefault(kind, strip_html(mm.group("val")))
            location = spans.get("joblocation") or spans.get("location") or ""
            if not location:                      # fall back to the city in the job URL
                m_city = HREF_CITY.search(href)
                if m_city:
                    location = m_city.group(1).replace("-", " ").title()
            count += 1
            yield {
                "job_id": href,
                "title": title,
                "location": location,
                "workplace": "",
                "posted": spans.get("jobdateposted") or spans.get("dateposted")
                          or spans.get("posteddate") or "",
                "url": href if href.startswith("http") else base + href,
                "summary": "",
            }
        log.info("[%s] radancy page %d: %d links parsed via %s (%d bytes)",
                 src.get("name"), page, count, how or "none", len(markup or ""))
        time.sleep(REQUEST_GAP)
        if count == 0:
            break


def radancy_description(src, job_id):
    base = src["base_url"].rstrip("/")
    url = job_id if job_id.startswith("http") else base + job_id
    page = _request("GET", url, parse_json=False, headers=src_headers(src))
    m = re.search(r'<div[^>]*class="[^"]*(?:ats-description|job-description|jobDescription)[^"]*"[^>]*>(.*?)</div>\s*(?:<div|<section|</main)', page, re.S | re.I)
    return strip_html(m.group(1) if m else page)[:20000]



# ---------------------------------------------------------------- Amazon
def amazon_list(src):
    """amazon.jobs search API. `facets` in config map straight to its query params."""
    limit = int(src.get("per_page", 20))
    for page in range(int(src.get("pages", 2))):
        params = [("result_limit", limit), ("offset", page * limit), ("sort", src.get("sort", "recent"))]
        for key, vals in (src.get("facets") or {}).items():
            for v in (vals if isinstance(vals, list) else [vals]):
                params.append((key, v))
        if src.get("keyword"):
            params.append(("base_query", src["keyword"]))
        data = get_json("https://www.amazon.jobs/search.json", params,
                        headers=src_headers(src, {"Referer": "https://www.amazon.jobs/en/search"}))
        jobs = data.get("jobs") or []
        for j in jobs:
            path = j.get("job_path") or ""
            yield {
                "job_id": str(j.get("id_icims") or path),
                "title": (j.get("title") or "").strip(),
                "location": j.get("normalized_location") or ", ".join(
                    x for x in [j.get("city"), j.get("state"), j.get("country_code")] if x),
                "workplace": "",
                "posted": j.get("posted_date") or "",
                "url": "https://www.amazon.jobs" + path,
                "summary": strip_html(j.get("description_short") or "")[:400],
            }
        time.sleep(REQUEST_GAP)
        if len(jobs) < limit:
            break


def amazon_description(src, job_id):
    data = get_json("https://www.amazon.jobs/search.json",
                    [("result_limit", 1), ("base_query", job_id)],
                    headers=src_headers(src, {"Referer": "https://www.amazon.jobs/en/search"}))
    jobs = data.get("jobs") or []
    if not jobs:
        return ""
    j = jobs[0]
    parts = [j.get("description"), j.get("basic_qualifications"), j.get("preferred_qualifications")]
    return "\n\n".join(strip_html(p) for p in parts if p)



# ---------------------------------------------------------------- Lever
def lever_list(src):
    """Lever public postings API. `host` is api.lever.co, or api.eu.lever.co for
    EU-hosted boards (jobs.eu.lever.co/...)."""
    host = src.get("api_host") or ("api.eu.lever.co" if src.get("eu") else "api.lever.co")
    data = get_json(f"https://{host}/v0/postings/{src['slug']}",
                    {"mode": "json", "limit": int(src.get("limit", 200))},
                    headers=src_headers(src))
    rows = data if isinstance(data, list) else (data.get("data") or [])
    rows.sort(key=lambda j: j.get("createdAt") or 0, reverse=True)
    for j in rows:
        cat = j.get("categories") or {}
        locs = j.get("allLocations") or [x for x in [j.get("location"), cat.get("location")] if x]
        posted = j.get("createdAt")
        yield {
            "job_id": str(j.get("id")),
            "title": (j.get("text") or "").strip(),
            "location": ", ".join(dict.fromkeys(locs))[:200],
            "workplace": j.get("workplaceType") or "",
            "posted": (datetime.fromtimestamp(posted / 1000, timezone.utc).strftime("%Y-%m-%d")
                       if isinstance(posted, (int, float)) else ""),
            "url": j.get("hostedUrl") or j.get("applyUrl") or "",
            "summary": (j.get("descriptionPlain") or "")[:400],
        }


def lever_description(src, job_id):
    host = src.get("api_host") or ("api.eu.lever.co" if src.get("eu") else "api.lever.co")
    j = get_json(f"https://{host}/v0/postings/{src['slug']}/{job_id}",
                 {"mode": "json"}, headers=src_headers(src))
    if isinstance(j, list):
        j = j[0] if j else {}
    parts = [j.get("descriptionPlain") or strip_html(j.get("description") or "")]
    for block in j.get("lists") or []:                 # requirements / qualifications
        parts.append(strip_html(block.get("text") or "") + "\n" + strip_html(block.get("content") or ""))
    parts.append(j.get("additionalPlain") or strip_html(j.get("additional") or ""))
    return "\n\n".join(p.strip() for p in parts if p and p.strip())


# ---------------------------------------------------------------- Ashby
def ashby_list(src):
    """Ashby job board API - returns the whole board with descriptions inline."""
    data = get_json(f"https://api.ashbyhq.com/posting-api/job-board/{src['slug']}",
                    {"includeCompensation": "true"}, headers=src_headers(src))
    rows = [j for j in (data.get("jobs") or []) if j.get("isListed", True)]
    rows.sort(key=lambda j: j.get("publishedAt") or "", reverse=True)
    want_type = (src.get("employment_type") or "").lower()
    for j in rows:
        if want_type and (j.get("employmentType") or "").lower() != want_type:
            continue
        locs = [j.get("location")] + [l.get("location") if isinstance(l, dict) else l
                                      for l in (j.get("secondaryLocations") or [])]
        yield {
            "job_id": str(j.get("id")),
            "title": (j.get("title") or "").strip(),
            "location": ", ".join(dict.fromkeys(x for x in locs if x))[:200],
            "workplace": j.get("workplaceType") or ("remote" if j.get("isRemote") else ""),
            "posted": (j.get("publishedAt") or "")[:10],
            "url": j.get("jobUrl") or j.get("applyUrl") or "",
            "summary": (j.get("descriptionPlain") or "")[:400],
            "_description": j.get("descriptionPlain") or strip_html(j.get("descriptionHtml") or ""),
        }


def ashby_description(src, job_id):
    data = get_json(f"https://api.ashbyhq.com/posting-api/job-board/{src['slug']}",
                    headers=src_headers(src))
    for j in data.get("jobs") or []:
        if str(j.get("id")) == str(job_id):
            return j.get("descriptionPlain") or strip_html(j.get("descriptionHtml") or "")
    return ""


ADAPTERS = {
    "oracle": (oracle_list, oracle_description),
    "workday": (workday_list, workday_description),
    "greenhouse": (greenhouse_list, greenhouse_description),
    "eightfold": (eightfold_list, eightfold_description),
    "radancy": (radancy_list, radancy_description),
    "amazon": (amazon_list, amazon_description),
    "lever": (lever_list, lever_description),
    "ashby": (ashby_list, ashby_description),
}


# ---------------------------------------------------------------- sponsorship
SPONSOR_HINT = re.compile(r"sponsor|stem\s*opt|\bh-?1\s*-?b\b|work\s+authoriz", re.I)
SPONSOR_NEG = re.compile(
    r"\b(not|no|never|without|unable|cannot|can't|won't|will\s+not|does\s+not|do\s+not|"
    r"are\s+not|is\s+not|ineligible|not\s+eligible|nor)\b", re.I)
SPONSOR_POS = re.compile(
    r"\b(sponsorship\s+(is\s+)?(available|offered|provided|possible)|will\s+sponsor|"
    r"can\s+sponsor|do\s+sponsor|we\s+sponsor|offer\s+(visa\s+)?sponsorship|"
    r"(willing|happy|able|prepared)\s+to\s+sponsor|sponsorship\s+(is\s+)?considered|open\s+to\s+sponsor)", re.I)
# A negation must sit near the sponsorship word, not just anywhere in a long sentence.
SPONSOR_WORD = re.compile(r"sponsor\w*|stem\s*opt|\bh-?1\s*-?b\b", re.I)


def sponsorship_status(text):
    """'not_offered' | 'offered' | 'unknown' for a job description.

    Sentence-scoped so 'we will not discriminate ... sponsorship is available' does not
    read as a refusal. A sentence counts as a refusal when a negation appears within
    ~90 characters of the sponsorship word.
    """
    if not text:
        return "unknown"
    verdict = "unknown"
    for sentence in re.split(r"(?<=[.!?;])\s+|\n+", text):
        if not SPONSOR_HINT.search(sentence):
            continue
        s = re.sub(r"\s+", " ", sentence)
        for m in SPONSOR_WORD.finditer(s):
            window = s[max(0, m.start() - 90):m.end() + 90]
            if SPONSOR_NEG.search(window):
                return "not_offered"            # a refusal anywhere wins outright
        if SPONSOR_POS.search(s):
            verdict = "offered"
    return verdict


# ---------------------------------------------------------------- experience bar
# Headings after which requirements become "nice to have" and stop counting.
PREFERRED_HEAD = re.compile(
    r"(?im)^\s*(?:[-*•]\s*)?(?:preferred|desired|nice[\s-]?to[\s-]?have|bonus|"
    r"a\s+plus|pluses|additionally\s+valued|what\s+will\s+set\s+you\s+apart|"
    r"preferred\s+qualifications|desired\s+skills)\b.{0,40}$")
WORD_NUM = {w: i for i, w in enumerate(
    "zero one two three four five six seven eight nine ten eleven twelve "
    "thirteen fourteen fifteen".split())}
# A number only counts when it sits near experience language.
EXP_CONTEXT = re.compile(
    r"(?i)experience|experienced|working|work(?:ing)?\s+in|professional|hands[\s-]?on|"
    r"building|developing|designing|background|track\s+record|career|practitioner|"
    r"in\s+(?:software|engineering|a\s+\w+\s+role)|as\s+an?\s+\w+\s+engineer")
YEARS_PAT = re.compile(
    r"(?i)(?:(?P<word>" + "|".join(WORD_NUM) + r")|(?P<lo>\d{1,2}))\s*"
    r"(?:\+|plus)?\s*(?:(?:-|–|—|to)\s*(?P<hi>\d{1,2})\s*\+?\s*)?"
    r"(?:\(\s*\d+\s*\)\s*)?years?")


def _required_section(text):
    """Everything before the first 'preferred / nice to have' heading."""
    m = PREFERRED_HEAD.search(text)
    return text[:m.start()] if m else text


def years_required(text, context_chars=70):
    """Highest years-of-experience bar stated in the REQUIRED part of a posting.

    Returns an int, or None when the posting never states one. Preferred/bonus
    sections are ignored, and a number only counts when experience language sits
    near it, so '401(k) after 1 year' and '175 years of history' do not register.
    """
    if not text:
        return None
    body = _required_section(text)
    best = None
    for m in YEARS_PAT.finditer(body):
        start, end = m.span()
        window = body[max(0, start - context_chars):min(len(body), end + context_chars)]
        if not EXP_CONTEXT.search(window):
            continue
        val = WORD_NUM[m.group("word").lower()] if m.group("word") else int(m.group("lo"))
        if not 0 < val <= 25:
            continue
        best = val if best is None else max(best, val)
    return best


# ---------------------------------------------------------------- storage
def db_connect():
    con = sqlite3.connect(DB_PATH)
    con.execute(
        """CREATE TABLE IF NOT EXISTS jobs (
            company TEXT, job_id TEXT, title TEXT, location TEXT, url TEXT,
            posted TEXT, first_seen TEXT, matched INTEGER, notified INTEGER,
            description TEXT, status TEXT DEFAULT 'new',
            PRIMARY KEY (company, job_id))"""
    )
    ensure_columns(con)
    return con


def ensure_columns(con):
    cols = {r[1] for r in con.execute("PRAGMA table_info(jobs)")}
    for name, decl in (("sponsorship", "TEXT"), ("tailored", "TEXT"),
                       ("years_req", "INTEGER"), ("match_score", "INTEGER")):
        if name not in cols:
            con.execute(f"ALTER TABLE jobs ADD COLUMN {name} {decl}")
    con.commit()


def company_is_seeded(con, company):
    return con.execute("SELECT 1 FROM jobs WHERE company=? LIMIT 1", (company,)).fetchone() is not None


def is_known(con, company, job_id):
    return con.execute("SELECT 1 FROM jobs WHERE company=? AND job_id=?", (company, job_id)).fetchone() is not None


# ---------------------------------------------------------------- notifiers
class Notifier:
    """Queues alerts and delivers them without tripping Discord's rate limit.

    Discord allows roughly 5 requests per 2s per webhook and up to 10 embeds per
    message, so a burst of 37 matches goes out as 4 messages instead of 37, a 429 is
    honoured and retried, and anything that still fails is reported so the caller can
    leave it marked undelivered and try again next cycle.
    """

    DISCORD_EMBEDS_PER_MSG = 10
    MIN_GAP = 0.45                      # seconds between webhook requests

    def __init__(self, cfg=None):
        cfg = cfg or {}
        self.pending = []               # (company, embed, telegram_text, job_id)
        self.last_send = 0.0
        self.sent = 0
        self.failed = []
        self.max_per_company = int(cfg.get("max_alerts_per_company", 0)) or None

    # ------------------------------------------------------------ queueing
    def add(self, job, company):
        loc = job["location"] + (f" ({job['workplace']})" if job["workplace"] else "")
        embed = {
            "title": job["title"][:250],
            "url": job["url"],
            "fields": [
                {"name": "Location", "value": (loc or "n/a")[:1000], "inline": True},
                {"name": "Posted", "value": job["posted"] or "n/a", "inline": True},
            ],
            "color": 0x2E77D0,
        }
        if job.get("summary"):
            embed["description"] = job["summary"][:400]
        text = (f"\U0001f195 <b>{html.escape(company)}</b>\n<b>{html.escape(job['title'])}</b>\n"
                f"\U0001f4cd {html.escape(loc or 'n/a')}\n\U0001f5d3 {html.escape(job['posted'] or 'n/a')}\n"
                f'<a href="{html.escape(job["url"])}">Apply →</a>')
        self.pending.append((company, embed, text, job.get("job_id")))
        return True

    # ------------------------------------------------------------ sending
    def _pace(self):
        gap = time.time() - self.last_send
        if gap < self.MIN_GAP:
            time.sleep(self.MIN_GAP - gap)
        self.last_send = time.time()

    def _post(self, url, payload, label):
        """POST honouring 429 retry_after. True only if the message really landed."""
        for attempt in range(6):
            self._pace()
            try:
                r = requests.post(url, json=payload, timeout=20)
            except Exception as e:
                log.warning("%s post failed (%s), retrying", label, e)
                time.sleep(2 * (attempt + 1))
                continue
            if r.status_code == 429:
                try:
                    wait = float(r.json().get("retry_after", 1))
                except Exception:
                    wait = 1.0
                time.sleep(min(wait, 10) + 0.3)
                continue
            if r.status_code >= 500:
                time.sleep(2 * (attempt + 1))
                continue
            if r.status_code >= 300:
                log.error("%s error %s: %s", label, r.status_code, r.text[:200])
                return False
            return True
        log.error("%s gave up after retries", label)
        return False

    def flush(self):
        """Send everything queued. Returns the set of (company, job_id) that landed."""
        if not self.pending:
            return set()
        queue, self.pending = self.pending, []
        delivered_ids = set()
        overflow = {}

        if self.max_per_company:
            trimmed, counts = [], {}
            for row in queue:
                company = row[0]
                counts[company] = counts.get(company, 0) + 1
                if counts[company] <= self.max_per_company:
                    trimmed.append(row)
                else:
                    overflow.setdefault(company, []).append(row)
            queue = trimmed

        hook = os.environ.get("DISCORD_WEBHOOK_URL")
        token, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")

        if hook:
            by_company = {}
            for company, embed, _, jid in queue:
                by_company.setdefault(company, []).append((embed, jid))
            for company, rows in by_company.items():
                for i in range(0, len(rows), self.DISCORD_EMBEDS_PER_MSG):
                    chunk = rows[i:i + self.DISCORD_EMBEDS_PER_MSG]
                    head = f"\U0001f195 **{company}** — {len(rows)} new match" + \
                           ("es" if len(rows) != 1 else "")
                    if len(rows) > self.DISCORD_EMBEDS_PER_MSG:
                        head += f"  (part {i // self.DISCORD_EMBEDS_PER_MSG + 1})"
                    if self._post(hook, {"content": head, "embeds": [e for e, _ in chunk]}, "Discord"):
                        self.sent += len(chunk)
                        delivered_ids.update((company, jid) for _, jid in chunk)
                    else:
                        self.failed += [e["url"] for e, _ in chunk]

            for company, rows in overflow.items():
                lines = "\n".join(f"• [{e['title']}]({e['url']})" for _, e, _, _ in rows[:40])
                body = f"➕ **{company}** — {len(rows)} more matches (alert cap):\n{lines}"
                if self._post(hook, {"content": body[:1900]}, "Discord"):
                    delivered_ids.update((company, r[3]) for r in rows)
                else:
                    self.failed += [r[1]["url"] for r in rows]

        if token and chat:
            for company, _, text, jid in queue:
                if self._post(f"https://api.telegram.org/bot{token}/sendMessage",
                              {"chat_id": chat, "text": text, "parse_mode": "HTML"}, "Telegram"):
                    self.sent += 1
                    delivered_ids.add((company, jid))

        if not hook and not (token and chat):
            for company, embed, _, jid in queue:
                log.warning("No notifier configured; match: %s %s", embed["title"], embed["url"])
        return delivered_ids


# ---------------------------------------------------------------- main loop
def enabled_companies(cfg):
    return [c for c in cfg["companies"] if c.get("enabled") is not False]


def mark_delivered(con, delivered):
    for company, job_id in delivered or ():
        con.execute("UPDATE jobs SET notified=1 WHERE company=? AND job_id=?", (company, job_id))
    con.commit()


def retry_undelivered(con, notifier, limit=40, max_age_hours=24):
    """A match whose alert never landed stays notified=0 and is re-queued next cycle,
    so a rate limit or an outage delays an alert instead of losing it. Only recent
    failures are retried - an old backlog is never replayed."""
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=max_age_hours)).isoformat(timespec="seconds")
    rows = con.execute(
        "SELECT company, job_id, title, location, url, posted FROM jobs "
        "WHERE matched=1 AND notified=0 AND first_seen >= ? "
        "AND (sponsorship IS NULL OR sponsorship != 'not_offered') "
        "AND (tailored IS NULL OR tailored NOT LIKE 'skipped:%') "
        "ORDER BY first_seen DESC LIMIT ?", (cutoff, limit)).fetchall()
    for company, job_id, title, location, url, posted in rows:
        notifier.add({"job_id": job_id, "title": title, "location": location or "",
                      "workplace": "", "posted": posted or "", "url": url, "summary": ""}, company)
    if rows:
        log.info("re-queued %d undelivered alert(s)", len(rows))
        mark_delivered(con, notifier.flush())


def fetch_company(src):
    """Network only - runs in a worker thread. Returns (src, jobs, error)."""
    try:
        list_fn, _ = ADAPTERS[src["ats"]]
        return src, list(list_fn(src)), None
    except Exception as e:
        return src, [], e


def poll_once(cfg, con, tfilter, notifier=None):
    """Fetch every board in parallel, screen new matches, then alert and store."""
    notifier = notifier or Notifier(cfg)
    retry_undelivered(con, notifier)                       # alerts that failed last cycle
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    companies = enabled_companies(cfg)
    workers = int(cfg.get("max_parallel", 8))
    tcfg = cfg.get("tailor") or {}
    min_score = cfg.get("min_match_score", tcfg.get("min_match_score"))
    # Scoring needs the tailoring step; without it there is no score to gate on, and
    # dropping every alert would be worse than letting them all through.
    score_gate = bool(tcfg.get("enabled")) and min_score is not None \
        and bool(os.environ.get("ANTHROPIC_API_KEY"))
    if min_score is not None and not score_gate:
        log.warning("min_match_score is set but scoring is unavailable "
                    "(tailor disabled or ANTHROPIC_API_KEY missing) - alerting unscored")
    skip_no_sponsor = bool(cfg.get("skip_no_sponsorship", True))
    alert_on_unknown = (cfg.get("on_unknown_sponsorship", "alert") == "alert")
    max_years = cfg.get("max_years_experience")
    max_years = int(max_years) if max_years not in (None, "", False) else None

    started = time.time()
    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(fetch_company, companies))
    log.info("fetched %d boards in %.1fs (%d workers)", len(companies), time.time() - started, workers)

    # ---- pass 1: find what is new, and which of those need a job description
    fresh, wanted = [], []
    for src, jobs, err in results:
        name = src["name"]
        if err:
            log.error("[%s] fetch failed: %s", name, err)
            continue
        seeding = not company_is_seeded(con, name)
        loc_re = re.compile(src["location_regex"], re.I) if src.get("location_regex") else None
        rows, batch_seen, dupes, loc_dropped, blank_loc = [], set(), 0, 0, 0
        for job in jobs:
            if loc_re and not loc_re.search(job["location"] or ""):
                loc_dropped += 1
                blank_loc += not (job["location"] or "").strip()
                continue
            if job["job_id"] in batch_seen:        # same posting twice in one fetch
                dupes += 1
                continue
            batch_seen.add(job["job_id"])
            if is_known(con, name, job["job_id"]):
                continue
            matched = tfilter.matches(job["title"])
            rows.append((job, matched))
            if matched and not seeding and src.get("fetch_description",
                                                   cfg.get("fetch_descriptions", True)):
                wanted.append((src, job["job_id"]))
        if dupes:
            log.info("[%s] %d duplicate posting id(s) in this fetch, ignored", name, dupes)
        if jobs and loc_dropped == len(jobs):
            log.error("[%s] location_regex rejected all %d postings%s - this board will "
                      "re-seed and never alert. Fix or remove its location_regex.",
                      name, len(jobs),
                      f" ({blank_loc} had no location text)" if blank_loc else "")
        fresh.append((src, seeding, rows, len(jobs)))

    # ---- pass 2: pull those descriptions up front, so screening happens before alerting
    descriptions = fetch_description_map(wanted) if wanted else {}

    # ---- pass 3: screen, store, alert
    to_score = []
    for src, seeding, rows, total in fresh:
        name = src["name"]
        new_matches = skipped = 0
        reasons = {}
        for job, matched in rows:
            desc = descriptions.get((name, job["job_id"]), "")
            status = sponsorship_status(desc) if desc else "unknown"
            yrs = years_required(desc) if desc else None
            reason = None
            alerted = 1                                    # 0 only once an alert is queued
            if matched and not seeding:
                if skip_no_sponsor and status == "not_offered":
                    reason = "no sponsorship"
                elif skip_no_sponsor and status == "unknown" and desc and not alert_on_unknown:
                    reason = "sponsorship unclear"
                elif max_years is not None and yrs is not None and yrs > max_years:
                    reason = f"{yrs}+ yrs required"
                if reason:
                    skipped += 1
                    reasons[reason] = reasons.get(reason, 0) + 1
                elif score_gate:
                    # With the JD attached, so the scorer can judge it: without one,
                    # tailor_job skips the posting and it would alert unscored.
                    to_score.append((name, dict(job, description=desc)))
                else:
                    notifier.add(job, name)
                    alerted = 0
                    new_matches += 1
            con.execute(
                "INSERT OR IGNORE INTO jobs (company, job_id, title, location, url, posted, first_seen,"
                " matched, notified, description, sponsorship, years_req, tailored)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (name, job["job_id"], job["title"], job["location"], job["url"], job["posted"],
                 now, int(matched), alerted, desc, status, yrs,
                 f"skipped: {reason}" if reason else None),
            )
        con.commit()
        mark_delivered(con, notifier.flush())              # this company's alerts go now
        if seeding:
            stored = con.execute("SELECT COUNT(*) FROM jobs WHERE company=?", (name,)).fetchone()[0]
            log.info("[%s] seeded %d existing postings (no alerts on first run)", name, total)
            if total and not stored:
                log.error("[%s] nothing was stored - this board will re-seed and never "
                          "alert. Check its job_id values.", name)
        else:
            extra = (", %d skipped (%s)" % (skipped, "; ".join(
                f"{v} {k}" for k, v in sorted(reasons.items())))) if skipped else ""
            if score_gate and new_matches == 0 and not skipped:
                queued = sum(1 for c, _ in to_score if c == name)
                extra += f", {queued} queued for scoring" if queued else ""
            log.info("[%s] checked %d postings, %d new matches%s", name, total, new_matches, extra)

    if to_score:
        score_then_alert(cfg, con, notifier, to_score, min_score)
    mark_delivered(con, notifier.flush())
    if notifier.failed:
        log.error("%d alert(s) undelivered - will retry next cycle: %s",
                  len(notifier.failed), ", ".join(notifier.failed[:3]))
    # since=STARTED_AT, not `now`: pass 1 drops anything already in the DB, so a new
    # posting pushed past max_per_cycle would otherwise never reach the tailor again.
    # Scoped to this run, the cap defers work to the next cycle instead of losing it.
    run_tailor(cfg, con, since=STARTED_AT)


def score_then_alert(cfg, con, notifier, queued, min_score):
    """Score each new match against the bullet bank and alert only the ones that clear
    `min_match_score`. This is why alerts now wait on an API call per posting - the
    score does not exist until the job description has been compared to the bank."""
    tcfg = cfg.get("tailor") or {}
    cap = int(tcfg.get("max_per_cycle", 5))
    try:
        import tailor
        tailor.apply_config(cfg)
        tailor.ensure_schema(con)
        templates = tailor.resume_bank.load_templates()
        bank, skills = tailor.build_bank(templates)
    except Exception as e:
        log.error("scoring unavailable (%s) - alerting all %d match(es) unscored",
                  e, len(queued))
        for company, job in queued:
            notifier.add(job, company)
        return

    log.info("scoring %d new match(es) against %d bullets (min %s)",
             min(len(queued), cap), len(bank), min_score)
    for company, job in queued[:cap]:
        row = dict(job, company=company)
        try:
            res = tailor.tailor_job(row, cfg, templates, bank, skills,
                                    tcfg.get("model", tailor.DEFAULT_MODEL),
                                    notify=False, min_score=min_score)
        except Exception as e:
            log.error("[%s] scoring failed (%s) - alerting unscored", company, e)
            notifier.add(job, company)
            continue
        if not res:
            notifier.add(job, company)
            continue
        mark = res["folder"] if res.get("passed") else f"skipped: score {res['score']}"
        con.execute("UPDATE jobs SET tailored=?, match_score=? WHERE company=? AND job_id=?",
                    (mark, res.get("score"), company, job["job_id"]))
        if res.get("passed"):
            # One message per resume: the card links the posting and carries the score.
            tailor.notify_discord(row, res["plan"], res["folder"])
    if len(queued) > cap:
        over = queued[cap:]
        # The tailor step scores the rest from the stored JD, this cycle or the next, and
        # alerts only those that clear the bar. A posting with no JD can never be scored,
        # so it is flagged undelivered and alerts unscored next cycle instead of vanishing.
        unscoreable = [(c, j["job_id"]) for c, j in over
                       if not (j.get("description") or "").strip()]
        log.info("%d match(es) past max_per_cycle - left to the tailor step%s", len(over),
                 f" ({len(unscoreable)} with no description will alert unscored)"
                 if unscoreable else "")
        con.executemany("UPDATE jobs SET notified=0 WHERE company=? AND job_id=?", unscoreable)
    con.commit()


UNTAILORED = ("matched=1 AND (tailored IS NULL OR tailored='') "
              "AND description IS NOT NULL AND description!='' "
              "AND (sponsorship IS NULL OR sponsorship != 'not_offered')")


def run_tailor(cfg, con, since=None):
    """Build tailored resumes for matches found in THIS cycle.

    Older untailored matches are deliberately left alone: tailoring costs an API call
    each and would otherwise quietly chew through the whole backlog, cycle after cycle.
    Work through those on purpose with `python tailor.py --run`.
    """
    tcfg = cfg.get("tailor") or {}
    if not tcfg.get("enabled"):
        return
    try:
        import tailor
        tailor.apply_config(cfg)
        tailor.ensure_schema(con)
        limit = int(tcfg.get("max_per_cycle", 5))

        where, args = UNTAILORED, []
        if tcfg.get("only_new", True) and since:
            where += " AND first_seen >= ?"
            args.append(since)
        jobs = tailor.db_jobs(con, where + " ORDER BY first_seen DESC LIMIT ?",
                              tuple(args + [limit]))
        backlog = con.execute(f"SELECT COUNT(*) FROM jobs WHERE {UNTAILORED}").fetchone()[0]
        pending = con.execute(f"SELECT COUNT(*) FROM jobs WHERE {UNTAILORED} "
                              "AND first_seen >= ?", (since,)).fetchone()[0] if since else 0
        if not jobs:
            if pending:
                log.info("%d posting(s) from this run still queued for tailoring", pending)
            elif backlog:
                log.info("all caught up for this run (%d pre-start match(es) ignored "
                         "by design)", backlog)
            return

        templates = tailor.resume_bank.load_templates()
        bank, skills = tailor.build_bank(templates)
        log.info("bullet bank: %d bullets from %s", len(bank),
                 ", ".join(t.kind for t in templates))
        tailor.run(cfg, con, templates, bank, skills,
                   tcfg.get("model", tailor.DEFAULT_MODEL), jobs)
        left = max(pending - len(jobs), 0)
        if left:
            log.info("%d more from this run queued for the next cycle", left)
    except Exception as e:
        log.error("tailor step failed: %s", e)


def fetch_description_map(pending, workers=4):
    """Pull full JDs in parallel. Returns {(company, job_id): text}."""
    def one(item):
        src, job_id = item
        _, desc_fn = ADAPTERS[src["ats"]]
        try:
            return (src["name"], job_id), desc_fn(src, job_id)
        except Exception as e:
            log.warning("[%s] description fetch failed for %s: %s", src["name"], job_id, e)
            return (src["name"], job_id), ""

    started = time.time()
    with ThreadPoolExecutor(max_workers=workers) as pool:
        out = dict(pool.map(one, pending))
    got = sum(1 for v in out.values() if v)
    log.info("fetched %d/%d job descriptions in %.1fs", got, len(pending), time.time() - started)
    return out


def check(cfg, tfilter):
    """Hit every feed once (first page only) and print what came back."""
    probes = [dict(src, pages=1, search_texts=(src.get("search_texts") or [""])[:1])
              for src in enabled_companies(cfg)]
    with ThreadPoolExecutor(max_workers=int(cfg.get("max_parallel", 8))) as pool:
        results = list(pool.map(fetch_company, probes))
    for src, jobs, err in results:
        print(f"\n=== {src['name']} ({src['ats']}) ===")
        if err:
            print(f"  FAILED: {err}")
            continue
        print(f"  OK: {len(jobs)} postings on first page")
        for j in jobs[:8]:
            flag = "MATCH" if tfilter.matches(j["title"]) else "  -  "
            print(f"  {flag}  {j['title']}  |  {j['location']}  |  {j['posted']}")
        if jobs:
            print(f"  sample link: {jobs[0]['url']}")
        time.sleep(REQUEST_GAP)


def serve_live_preview(cfg, preview):
    """Serve the live preview page if nothing else does. Tried every cycle, so the
    watcher takes the port over once a stand-alone live_preview.py lets it go."""
    if not preview["want"] or preview["url"]:
        return
    try:
        import live_preview
        preview["url"] = live_preview.start_in_background(cfg)
    except Exception as e:                      # watching is optional; polling is not
        log.warning("live preview unavailable: %s", e)
        preview["want"] = False
        return
    if preview["url"]:
        log.info("live preview: %s - every scored posting plays there as it is built",
                 preview["url"])
    elif not preview["warned"]:
        log.info("live preview port is taken (live_preview.py running?) - it serves the "
                 "same runs; the watcher takes over if it stops")
        preview["warned"] = True


def main():
    with open(CONFIG_PATH) as f:
        cfg = yaml.safe_load(f)
    tfilter = TitleFilter(cfg["titles"]["include"], cfg["titles"]["exclude"])

    if "--test-notify" in sys.argv:
        n = Notifier(cfg)
        n.add({"job_id": "test", "title": "Test: Senior Software Engineer", "location": "Anywhere",
               "workplace": "", "posted": "today", "url": "https://example.com",
               "summary": "jobwatch is wired up."}, "jobwatch")
        print("delivered:", len(n.flush()))
        return
    if "--explain" in sys.argv:
        con = db_connect()
        rid = sys.argv[sys.argv.index("--explain") + 1]
        r = con.execute("SELECT company, job_id, title, url, sponsorship, notified, tailored,"
                        " description, years_req FROM jobs WHERE rowid=?", (rid,)).fetchone()
        if not r:
            print("no job with that rowid")
            return
        company, job_id, title, url, sp, notified, tailored, desc, yrs = r
        desc = desc or ""
        print(f"{title}\n{company}  {url}\n")
        print(f"  stored description : {len(desc)} chars")
        print(f"  sponsorship        : {sp}   (recomputed now: {sponsorship_status(desc)})")
        print(f"  years required     : {yrs}   (recomputed now: {years_required(desc)})")
        print(f"  alerted            : {'yes' if notified else 'no'}")
        print(f"  tailored           : {tailored or '-'}")
        hits = [x.strip() for x in re.split(r"(?<=[.!?;])\s+|\n+", desc) if SPONSOR_HINT.search(x)]
        print("  sponsorship lines  :", f"{len(hits)} found" if hits else "NONE in stored text")
        for h in hits[:6]:
            print("     -", re.sub(r"\s+", " ", h)[:150])
        yl = [x.strip() for x in re.split(r"(?<=[.!?;])\s+|\n+", _required_section(desc))
              if YEARS_PAT.search(x) and EXP_CONTEXT.search(x)]
        print("  experience lines   :", f"{len(yl)} found" if yl else "none")
        for h in yl[:5]:
            print("     -", re.sub(r"\s+", " ", h)[:150])
        if len(desc) < 400:
            print("\n  NOTE: description looks short or empty - the fetch probably failed,")
            print("        so the posting was treated as 'unknown' and alerted.")
        return
    if "--rescan-sponsorship" in sys.argv:
        con = db_connect()
        rows = con.execute("SELECT company, job_id, title, description FROM jobs "
                           "WHERE description IS NOT NULL AND description != ''").fetchall()
        counts = {}
        cfgm = yaml.safe_load(open(CONFIG_PATH))
        cap = cfgm.get("max_years_experience")
        cap = int(cap) if cap not in (None, "", False) else None
        for company, job_id, title, desc in rows:
            st = sponsorship_status(desc)
            yrs = years_required(desc)
            counts[st] = counts.get(st, 0) + 1
            if cap is not None and yrs is not None and yrs > cap:
                counts["over_experience"] = counts.get("over_experience", 0) + 1
            con.execute("UPDATE jobs SET sponsorship=?, years_req=? WHERE company=? AND job_id=?",
                        (st, yrs, company, job_id))
            skip = ("no sponsorship" if st == "not_offered" else
                    f"{yrs}+ yrs required" if (cap is not None and yrs is not None and yrs > cap)
                    else None)
            if skip:
                con.execute("UPDATE jobs SET tailored=? "
                            "WHERE company=? AND job_id=? AND (tailored IS NULL OR tailored='')",
                            (f"skipped: {skip}", company, job_id))
        con.commit()
        print(f"scanned {len(rows)} stored descriptions: " +
              ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
        return
    if "--mark-delivered" in sys.argv:
        con = db_connect()
        n = con.execute("UPDATE jobs SET notified=1 WHERE matched=1 AND notified=0").rowcount
        con.commit()
        print(f"marked {n} old match(es) as already handled; they will not be re-alerted")
        return
    if "--check" in sys.argv:
        check(cfg, tfilter)
        return

    con = db_connect()
    if "--once" in sys.argv:
        poll_once(cfg, con, tfilter)
        return

    tcfg = cfg.get("tailor") or {}
    preview = {"want": bool(tcfg.get("enabled") and tcfg.get("live_preview", True)),
               "url": None, "warned": False}

    interval = float(cfg.get("poll_interval_minutes", 5)) * 60
    jitter = float(cfg.get("jitter_seconds", 10))
    while True:
        serve_live_preview(cfg, preview)
        started = time.time()
        poll_once(cfg, con, tfilter)
        # Fixed rate: the cycle's own runtime counts toward the interval, so polls
        # happen every `interval`, not every `interval + however long a cycle took`.
        nap = max(5.0, interval - (time.time() - started) + random.uniform(0, jitter))
        log.info("cycle took %.1fs; next poll in %.0fs", time.time() - started, nap)
        time.sleep(nap)


if __name__ == "__main__":
    main()