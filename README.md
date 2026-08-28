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

Hourly, at `:17`. GitHub documents that scheduled runs are delayed under load
and that "high load times include the start of every hour", so the top of the
hour is the worst slot to ask for. Pages is redeployed only when the data
actually changed, or on a push / manual run.

⚠️ GitHub **auto-disables scheduled workflows after 60 days of no repository
activity** on public repos. Whether the bot's own commits reset that clock is
undocumented, so if refreshes ever stop, check the Actions tab and re-enable.

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
