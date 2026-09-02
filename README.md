# Grad Job Dashboard

A self-refreshing dashboard of new-grad SDE/AI-ML listings, plus a personal
pipeline tracker for interviews and offers.

Built around one goal: **apply within 12–24 hours of a role being posted.**
That target drives the refresh cadence, the multi-source design, and the NEW
tracking described below.

See **SETUP.md** for the 5-minute, no-coding setup.

- `index.html` — the dashboard itself
- `data/jobs.json` — merged listings, rewritten by the scheduled job
- `scripts/fetch_jobs.py` — pulls, merges, and de-duplicates the sources
- `.github/workflows/refresh.yml` — runs `fetch_jobs.py` hourly and
  redeploys the site automatically

## Where the listings come from

Two layers. The important one is first.

### 1. Company ATS boards, polled directly (`scripts/ats.py`)

The trackers below are themselves scrapers pointed at company applicant-tracking
systems, so they are structurally behind. Indeed and LinkedIn are downstream of
the same boards, and both prohibit automated access -- LinkedIn says so outright
in its robots.txt -- so neither is scraped here. Polling the ATS directly puts us
*upstream* of all of them: a role appears in the system the company posts into
before it appears anywhere else.

Six platforms, all public and unauthenticated, no API keys:

| Platform | Endpoint |
|---|---|
| Greenhouse | `boards-api.greenhouse.io/v1/boards/{co}/jobs` |
| Lever | `api.lever.co/v0/postings/{co}?mode=json` |
| Ashby | `api.ashbyhq.com/posting-api/job-board/{co}` |
| SmartRecruiters | `api.smartrecruiters.com/v1/companies/{co}/postings` |
| Workable | `apply.workable.com/api/v1/widget/accounts/{co}` |
| Workday | `{tenant}.{wd}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs` |

**The board registry builds itself.** Every listing links to wherever the company
actually accepts applications, so `discover_boards()` mines `data/jobs.json` for
ATS URLs and grows `data/boards.json` on its own -- currently ~890 boards, none
hand-entered.

A full sweep reads ~52,000 raw postings and keeps ~520 after filtering. Roughly a
third of those appear in no tracker at all.

**Filtering carries the whole feed's quality.** One Greenhouse board can be 800
roles across every function, so `relevant()` demands a specific software/data/ML
term (a bare "engineer" pulls in civil and mechanical), requires a new-grad
signal, and rejects senior titles, interns, other engineering disciplines, and
non-US locations. Precision is favoured over recall: a board you stop trusting is
worse than one that misses a listing.

### 2. Community trackers, as a safety net

Four community trackers, merged. No single one is dependable: in Aug 2026
`vanshb03` went a full week without an update while the other three posted
daily. A one-source setup cannot tell "the source is stale" apart from
"nobody is hiring" — exactly the failure that hides a fresh posting.

| Source | Format | Contributes |
|---|---|---|
| [SimplifyJobs/New-Grad-Positions](https://github.com/SimplifyJobs/New-Grad-Positions) | **JSON** | Bulk of the board; true epoch post dates, category + sponsorship fields |
| [speedyapply/2027-SWE-College-Jobs](https://github.com/speedyapply/2027-SWE-College-Jobs) | Markdown | Age-in-days column and **salary** figures |
| [jobright-ai/2026-Software-Engineer-New-Grad](https://github.com/jobright-ai/2026-Software-Engineer-New-Grad) | Markdown | Extra breadth; links bounce through a jobright redirect |
| [vanshb03/New-Grad-2027](https://github.com/vanshb03/New-Grad-2027) | Markdown | The only source carrying 🛂 / 🇺🇸 sponsorship markers |

Each source is fetched independently. One failing degrades coverage for that
run; it never fails the refresh and never overwrites good data — `main()`
refuses to write unless at least two sources succeeded and the merged result
clears a volume floor.

Note that `speedyapply` publishes several tables whose columns differ (some
carry Salary, some do not), so its parser locates fields by shape rather than
index. A positional parse silently drops every row of the narrower tables.

## How merging works

Two passes, because trackers word the same job differently:

1. **By normalized company + role.** Catches the easy overlaps.
2. **By canonical apply URL** — but *only* for URLs carrying a job id
   (Greenhouse/Lever/Ashby/Workday all embed one). This catches
   `Software Engineer 1 New Grad - QA` vs `Software Engineer I - QA - New Grad`,
   which pass 1 misses despite the two sharing a Greenhouse job id. Generic
   careers landing pages like `/open-vacancies` are deliberately excluded:
   they host genuinely different roles, and merging on them would lose
   listings.

When records fold together the merge keeps the *earliest* posting date, the
most direct apply link, any known sponsorship value over `unknown`, and any
salary. `2× confirmed` in the UI means more than one tracker carries the role.

## Dates and freshness

Sources disagree wildly: Simplify gives Unix epochs, speedyapply gives `"3d"`,
the others give a bare `"Aug 28"` with no year. All are normalized to an ISO
`posted` date. For the bare month/day tables, `walk_years()` exploits the fact
that they are maintained newest-first and steps the year back at each upward
month jump (`Jan 01` → `Dec 12`). Only a *month* increase counts, so a day
wobble inside one month cannot shift a whole year of listings. If a source
ever abandons that ordering, the years degrade quietly rather than failing
loudly — worth re-checking if dates start looking off.

**`first_seen` is ours, not the sources'.** It records when this project first
saw a listing, carried forward across runs from the previous `data/jobs.json`.
Because upstream dates are inconsistent and sometimes missing, `first_seen` —
not `posted` — is what the NEW badge keys off. The dashboard stores your
"Mark all seen" timestamp in browser local storage and flags anything first
seen after it. That marker is deliberately sticky, so reloading the page
cannot silently wipe the list of roles you have not reviewed yet.

Output is byte-stable across runs when nothing upstream changed, so the hourly
schedule does not generate churn commits.

## Scheduling

Two cadences. Hourly at `:17`, polling only the ~220 boards that actually
produced a new-grad role on the last full sweep -- same yield as sweeping all
890, at a quarter of the requests. Once a day at 06:17 UTC the run passes
`--full`, sweeps every board, and recomputes that productive set, so companies
that start or stop hiring grads move in and out on their own.

`:17` rather than `:00`: GitHub documents that scheduled runs are delayed under
load and that "high load times include the start of every hour", so the top of
the hour is the worst slot to ask for. Pages is redeployed only when the data
actually changed, or on a push / manual run.

⚠️ GitHub **auto-disables scheduled workflows after 60 days of no repository
activity** on public repos. Whether the bot's own commits reset that clock is
undocumented, so if refreshes ever stop, check the Actions tab and re-enable.

## Per-job résumé tailoring

`run.bat tailor` builds one résumé per starred listing, rewritten against that
job's actual description.

**Why not the pre-built variants.** They select from a fixed library, and
selection cannot reframe. Measured: scoring a real InterSystems JD moved project
ranks (jobboard 39 → 47.5) but produced a *byte-identical* PDF, because with six
projects and four slots the same four always win. Only a model rewriting bullet
text does what hand-tailoring does.

**Division of labour.** Claude selects and rewrites bullet *content* via
structured output; it never emits LaTeX. `build.py` still renders and compiles,
so the two-page guarantee holds and a model slip cannot break the document.

**Truthfulness is enforced, not requested.** `tailor.py:verify()` extracts every
number and proper noun from each rewrite and checks it against the library:
figures against the source bullet (so 950 cannot become 5000), technology names
against the whole library including the skills list (so a legitimate reframe
naming Python is not flagged). Anything unsupported marks the résumé
**unverified** and is listed in the output. Five guard cases are covered by tests
— inflated metric, invented technology, invented employer, unknown bullet id, and
a faithful reframe that must stay clean.

**JD fetching** (`resumes/jd.py`) reaches ~79% of listings — measured, not
estimated: Greenhouse/Lever/SmartRecruiters 100%, Workday and Ashby ~83%, and a
scrape fallback that recovers ~83% of the arbitrary career pages. Workable is the
one gap (no public per-job endpoint, ~2% of the board). Anything thin is labelled
`low` or `none` confidence rather than silently tailored against nothing.

**`jd_match` is our own score, not an ATS score.** No major ATS publishes one.
It measures JD keyword coverage, title alignment, section presence,
`pdftotext` parseability, and length. Useful for comparing two drafts of the same
résumé; not a prediction of any employer's software. Untailored résumés score
64–73 against real JDs, which is the headroom tailoring closes. A JD naming no
technologies scores the neutral midpoint rather than zero — that says
"uninformative JD", not "bad résumé".

Cost is ~$0.05–0.11 per résumé on Claude Opus 5, with the system prompt and
content library cached across jobs.

## Adding a source

Each source lives in its own `from_*()` returning records through the shared
`rec()` helper, so a new one is a single function plus an entry in `SOURCES`.

The highest-value next step is polling company ATS boards directly. Greenhouse
(`boards-api.greenhouse.io/v1/boards/{co}/jobs`), Lever
(`api.lever.co/v0/postings/{co}?mode=json`), and Ashby
(`api.ashbyhq.com/posting-api/job-board/{co}`) are all public and
unauthenticated, and Lever/Ashby expose exact publish timestamps. Those boards
are what these trackers scrape, so polling a target company list directly
would surface roles hours earlier than any aggregator.
