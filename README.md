# jobwatch

Watches 12 company career boards, alerts Discord/Telegram the moment a matching
role is posted, then builds a tailored one-page resume for it.

```
watcher.py      polls all boards in parallel → filters titles → alerts → stores JDs
resume_bank.py  parses the 4 master .docx templates into a 170-bullet bank
tailor.py       JD → Claude picks bullets → tailored .docx + match report
templates/      your master resumes (or point elsewhere with tailor.templates_dir)
applications/   one folder per job: resume, match report, job description
config.yaml     companies, title filters, tailoring settings
jobs.db         every posting ever seen, so each one alerts once
```

## Running

```bash
pip install -r requirements.txt
export DISCORD_WEBHOOK_URL=...            # alerts
export ANTHROPIC_API_KEY=...              # tailoring

python watcher.py --mark-delivered        # run ONCE after upgrading: clears the alert backlog
python watcher.py --check                 # test every board, write nothing
python watcher.py --once                  # one cycle (seeds new companies silently)
caffeinate -i python watcher.py           # run continuously
```

Each cycle: fetch all boards concurrently → fetch the job description for each new
match → screen it → alert on the survivors → build tailored resumes.

### Screening before the alert

Two filters run on the job description before anything reaches Discord.

**Years of experience** — `max_years_experience: 5` skips a posting whose *required*
section asks for more. Preferred / nice-to-have / bonus sections are ignored, so
"3+ years required, 10+ preferred" still alerts. A number only counts when experience
language sits near it, so "175 years of history" and "401(k) after 1 year" don't
register. Leave the setting blank to turn it off.

### Visa sponsorship screening

A posting that rules out sponsorship never reaches Discord and is never tailored. The
detector reads each sentence mentioning sponsorship, STEM OPT, H-1B or work
authorization and looks for a negation near that word, so it catches the many phrasings:

```
... the company will not pursue visa sponsorship for this position.
... will not provide sponsorship for employment visas or participate in STEM OPT.
... must be authorized to work in the U.S. without the need for employer sponsorship.
... we are unable to sponsor or transfer H1B visas.
```

Sentence scoping keeps "we are happy to sponsor; we do not discriminate..." from reading
as a refusal. Each job stores `offered`, `not_offered` or `unknown`; `unknown` still
alerts by default, so an unparsed posting is never silently dropped — set
`on_unknown_sponsorship: skip` to drop those too.

**The tradeoff:** screening needs the job description, so descriptions are now fetched
*before* alerting rather than after. That adds a couple of seconds per cycle. Set
`skip_no_sponsorship: false` to go back to alerting first and screening never.

Run `python watcher.py --rescan-sponsorship` after changing anything here, to re-screen
descriptions already stored.

If a no-sponsorship role still alerts, ask why:

```
python tailor.py --list                 # find the job's #
python watcher.py --explain 512
```

That prints the stored description length, the sponsorship verdict, and every sentence
in the stored text that mentions sponsorship. A short or empty description means the
fetch failed, the posting was treated as `unknown`, and it alerted by design — the
screen can only read text it actually has. Oracle boards scatter a posting across many
fields and often bury the visa clause — or the preferred qualifications — in a trailing
one, so the Oracle and Workday adapters now collect *every* prose string in the
response rather than a hand-picked set of fields.

### Alert delivery

Alerts are batched, paced and confirmed. Discord allows ~5 requests per 2s per webhook
and 10 embeds per message, so 37 matches go out as 4 messages, not 37. A 429 is honoured
(`retry_after`) and retried rather than dropped.

A match is written with `notified=0` and only flipped to `1` once its message is
confirmed delivered, so an alert that fails is re-queued on the next cycle instead of
vanishing. Only failures from the last 24 hours are retried, so an old backlog never
replays.

`max_alerts_per_company: 12` caps how many separate alerts one company can produce per
cycle; the rest arrive as a single digest of links. Amazon alone posts ~40 US software
roles a day, so either keep the cap or narrow it in `config.yaml` with `keyword:` or
`location_regex:`.

## Tailoring

Runs automatically after alerts when `tailor.enabled: true`, **only for jobs found in
that cycle**. Older untailored matches are left alone on purpose — each resume costs an
API call, and a backlog would otherwise be chewed through quietly, cycle after cycle,
stretching every cycle to several minutes. The log says how many are waiting:

```
no new jobs to tailor (47 older untailored match(es); run `python tailor.py --run` ...)
```

Work through the backlog deliberately with `tailor.py --run --limit N`. Set
`only_new: false` to go back to auto-tailoring the backlog too.

Manually:

```bash
python tailor.py --list                   # matched jobs and tailor status
python tailor.py --job 42                 # tailor one job by #
python tailor.py --run                    # tailor everything not yet done
python tailor.py --url <posting url>      # tailor any posting, even off-watchlist
python tailor.py --jd-file jd.txt         # tailor from a pasted JD
```

### How a resume gets built

1. Claude reads the JD and the full bullet bank (170 bullets, all 4 templates).
2. It picks the best-fit template as the base — that sets the skills line, framing
   and section structure.
3. It selects bullet **ids** for each block, free to pull from any template. A
   Kubernetes-heavy full-stack job can take React bullets from `fullstack` and
   Docker bullets from `cloud_devops` in the same resume.
4. The `.docx` is your chosen template with unselected paragraphs removed and the
   selected ones reordered strongest-first. Formatting is untouched.

**Claude never writes bullet text.** It returns ids; ids not in the bank are
discarded and logged in the report under "Discarded ids". Every line in the output
is verbatim from a file in `templates/`.

`max_tokens` is the whole response budget, and a reasoning model spends part of it
before writing any output — set too low, the budget is gone before the JSON starts and
the call fails with `hit max_tokens before any JSON was produced`. 16000 leaves room;
an unused ceiling costs nothing, since billing counts tokens actually generated.

If a reply still isn't JSON, the call is retried once with a bigger budget and the
contract restated as a follow-up user turn. If that fails too, both raw responses land
in `applications/_debug/` and the error says what came back.

### Score gate — the recruiter's reading

The first person reading a resume is a recruiter, and a recruiter reads literally. A
requirement only counts as a qualification when the posting's **own words** appear in a
bullet that sits **inside a job**. A SKILLS line or a summary names no employer, so
anything that lives only there reads as a claim. Nothing is inferred: TypeScript does
not prove JavaScript, cloud does not prove AWS.

The tailor scores every posting that way:

1. Claude copies the posting's required terms **verbatim**, most central first, and
   separates out what no bullet can say word-for-word (years, degrees, certifications).
   Any "required term" not literally in the job description is discarded.
2. Claude proposes a selection; the code then **repairs coverage** inside the page
   budget — for each required term the page doesn't prove yet, it swaps in a bullet
   from your bank that says it, without dropping anything already proven.
3. Each block is **ordered** so bullets carrying the most-asked-for terms print first —
   the first bullet of your most recent job is the most-read line on the page.
4. The **recruiter score** is the share of required terms proven in the posting's words
   inside a job. It alone decides whether a resume is written and an alert sent.
   Claude's semantic score is kept in the report for context and never gates.

`min_match_score: 65` is that recruiter-score bar. Literal scores run lower than the old
semantic ones, so once postings have accumulated, set the bar from data:

```
python tailor.py --scores        # distribution + how many alerts each bar would send
```

Below the bar no resume is written and nothing reaches Discord, but the score and the
gaps are still recorded — a weak match is exactly where the gap list earns its keep.

**This costs alert speed.** Each new match waits on one API call (~20–45s with Sonnet,
less with Haiku). `max_per_cycle` caps how many are scored per poll; the rest are picked
up next cycle. Delete `min_match_score` to alert the moment a title matches. Scoring is
skipped — and everything alerts unscored — when tailoring is off or `ANTHROPIC_API_KEY`
is missing, so a misconfiguration can never silently swallow every alert.

### What only you can fix

The code can enforce *what* (their words) and *where* (inside a job). It cannot supply
*how* you used something, *why* it mattered in plain English, or a number with real
scale — those are facts only you have, and the tailor never rewrites a bullet.

```
python tailor.py --audit         # writes applications/_gaps/BANK_AUDIT.md
```

lists every bullet with no plain-English reason, a percentage with nothing behind it, or
a verb that would be true of a different job — most-printed bullets first, so the
rewrites that reach the most recruiters come first. A passing bullet reads like:
*Built REST APIs in Python with FastAPI, PostgreSQL and AWS so customers could schedule
their own email briefings instead of asking our team to pull the data by hand.*

### Keyword gaps

Every requirement that no bullet in the bank evidences is pooled into
`applications/_gaps/`, accumulating across all tailored jobs:

```
python tailor.py --gaps
```

```
  4x  Terraform                     (cloud_devops)
  3x  Artifactory                   (cloud_devops)
  1x  Splunk                        (cloud_devops)
```

`KEYWORD_GAPS.md` ranks them by how often employers ask, names the template those roles
usually map to, and links an example role. Nothing is written into your templates — the
file tells you which real experience is costing you the most matches, and which bullets
are worth writing yourself if the experience exists and the bank just never captured it.

`match_report.md` gives the recruiter score, each proven term with the job and bullet
that proves it, a first-bullet check, claims with no proof (SKILLS or Key Achievements
only), **page gaps** (your bank says it, but it didn't fit this page) and **bank gaps**
(no bullet anywhere says it). Only bank gaps pool into `KEYWORD_GAPS.md` — those are the
ones to write, in the posting's own words, wherever the experience is real.

### Tuning

```yaml
tailor:
  enabled: true
  model: claude-sonnet-5        # claude-haiku-4-5-20251001 is cheaper and faster
  max_per_cycle: 5              # cap resumes per poll
  compact: true                 # tighten margins so the selection fits one page
  page_budget:                  # bullets kept per block
    achievements: 4
    community dreams foundation: 7
    medical informatics engineering: 6
    accenture: 5
```

`line_spacing: 1.15` is forced onto every paragraph. Word resolves spacing from three
places — document defaults, the paragraph style, and direct paragraph formatting — so
setting it in one place alone gets overridden elsewhere; the renderer rewrites all
three, and adds an explicit value to any paragraph that had none.

19 bullets is the one-page maximum at 1.15 across all four templates (`cloud_devops`
has the longest bullets and sets the limit). Raising `page_budget` or `line_spacing`
spills onto a second page — re-check the page count after either change. Setting
`compact: false` keeps your templates' original margins, which fits far fewer.

Templates live wherever `tailor.templates_dir` points (`~/Downloads/comet` right now);
leave it unset to use the `templates/` folder next to the scripts.

`templates_include` lists exactly which files count as master templates. This matters:
the folder also holds resumes this tool generated, and without the filter their bullets
join the bank and can be selected into new resumes. Anything skipped is logged:

```
ignoring 3 non-template .docx in ~/Downloads/comet: jpmc_software_engineer_resume.docx, ...
bullet bank: 170 bullets from backend, cloud_devops, frontend, fullstack
```

Check that second line after any change — if it names more than your four templates, fix
`templates_include` before trusting the output. Employers are matched across templates by name, so keep the
company lines spelled the same way in each file.

## Boards

| Platform | Companies |
|---|---|
| Workday (11) | PNC, Bank of America, Citi, Wells Fargo, Visa, Tempus AI, Fractal Analytics, Synechron, Grubhub, Elanco, Viavi Solutions |
| Oracle Recruiting (6) | American Express, JPMorgan Chase, Goldman Sachs, EXL, Grant Thornton, Emerson |
| Greenhouse (5) | Stripe, SeatGeek, Squarespace, xAI, MNTN |
| Lever | Cirrus Logic |
| Ashby | Gen Digital |
| Eightfold | HSBC |
| Radancy | Barclays |
| amazon.jobs | Amazon |

Workday boards normally resolve a `country:` name into a facet id on first run. When
that name doesn't match, copy the id straight out of the board's own URL instead:

```yaml
    facets:
      locationCountry: bc33aa3152ec42d4995f4791a106ed09
```

Lever boards on `jobs.eu.lever.co` need `eu: true`. Ashby and Lever both return full
job descriptions in the listing call, so they cost one request per cycle.

To add a company, find its platform (usually visible in the careers URL) and copy an
existing block in `config.yaml`. For an unknown platform, open the careers page with
DevTools → Network → Fetch/XHR, find the request returning the job list, and
"Copy as cURL" — that has everything needed to write an adapter.