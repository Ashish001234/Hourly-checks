#!/usr/bin/env python3
"""
Pulls new-grad listings from several trackers, merges them, and writes
data/jobs.json. Run by .github/workflows/refresh.yml on a schedule.

Design notes
------------
No single tracker is reliable on its own: vanshb03 went a full week without
an update in Aug 2026 while the others posted daily, and a one-source setup
cannot tell "source is stale" apart from "nobody is hiring". So we read four
and merge them.

Every source is independently fault-tolerant. One 500, or one upstream format
change, degrades coverage for that run; it never takes the whole refresh down
and never clobbers good data (see the guards in main()).

first_seen is ours, not the source's. It records when *we* first saw a
listing, carried forward across runs from the previous data/jobs.json. Source
dates are wildly inconsistent (epoch, "3d ago", bare "Aug 28", or missing
entirely), so first_seen is what the dashboard's NEW badge keys off.
"""
import datetime
import json
import os
import re
import sys
import urllib.error
import urllib.request

UA = {"User-Agent": "grad-job-dashboard"}
TIMEOUT = 60

SIMPLIFY_URL = "https://raw.githubusercontent.com/SimplifyJobs/New-Grad-Positions/dev/.github/scripts/listings.json"
SPEEDY_URL = "https://raw.githubusercontent.com/speedyapply/2027-SWE-College-Jobs/main/NEW_GRAD_USA.md"
JOBRIGHT_URL = "https://raw.githubusercontent.com/jobright-ai/2026-Software-Engineer-New-Grad/master/README.md"
VANSH_URL = "https://raw.githubusercontent.com/vanshb03/New-Grad-2027/dev/README.md"

OUT_PATH = os.path.join("data", "jobs.json")

FAANG = {"google", "meta", "facebook", "apple", "amazon", "netflix", "microsoft", "nvidia"}
BIGTECH = {
    "oracle", "salesforce", "adobe", "cisco", "tesla", "uber technologies, inc.", "uber",
    "ibm", "intel", "qualcomm", "servicenow", "sap", "vmware", "palantir technologies",
    "palantir", "snowflake", "datadog", "airbnb", "linkedin", "paypal", "stripe", "block",
    "doordash", "spotify", "twitch", "twitch interactive, inc.",
}

ML_KEYWORDS = [
    "ai", "ml", "machine learning", "data scientist", "nlp",
    "perception", "computer vision", "quant",
]

# Simplify carries its whole board; keep only the categories worth seeing.
SIMPLIFY_CATEGORIES = re.compile(r"Software|AI/ML/Data|Quant|Data Science", re.I)

# Deliberately narrow. These lists are already new-grad scoped, so we only drop
# what is unmistakably senior, rather than requiring a "new grad" keyword --
# that would throw away plainly-titled "Software Engineer" roles.
SENIOR = re.compile(
    r"senior|staff|principal|\blead\b|manager|director|\bsr\.?\b|architect|\bvp\b|head of",
    re.I,
)

MONTHS = {m: i for i, m in enumerate(
    "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split(), start=1)}


def get(url, as_json=False):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        raw = resp.read()
    return json.loads(raw) if as_json else raw.decode("utf-8", "replace")


def tier(company):
    c = (company or "").lower().strip()
    if c in FAANG:
        return 1
    if c in BIGTECH:
        return 2
    return 3


def is_ml(role):
    r = (role or "").lower()
    return any(k in r for k in ML_KEYWORDS)


def norm_key(s):
    """Aggressive normalization, used only to match the same job across sources."""
    s = (s or "").lower()
    s = re.sub(r"[^a-z0-9]+", " ", s)
    s = re.sub(r"\b(inc|llc|ltd|corp|corporation|co|the)\b", " ", s)
    return " ".join(s.split())


def strip_tags(s):
    return re.sub(r"<[^>]+>", "", s or "").strip()


def clean_loc(loc):
    loc = (loc or "").replace("</br>", ", ").replace("<br>", ", ")
    loc = loc.replace("<details><summary>", "").replace("</summary>", "").replace("</details>", "")
    return " ".join(strip_tags(loc).split())


def walk_years(md_dates):
    """Assign years to a reverse-chronological list of bare "Aug 28" dates.

    These tables omit the year but are maintained newest-first, so step the
    year back at each upward month jump. Only a *month* increase counts: a day
    wobble inside one month must not shift a whole year of listings.
    """
    today = datetime.date.today()
    parsed = []
    for d in md_dates:
        m = re.match(r"([A-Za-z]{3})\s+(\d{1,2})$", (d or "").strip())
        month = MONTHS.get(m.group(1).title()) if m else None
        parsed.append((month, int(m.group(2))) if month else None)

    first = next((p for p in parsed if p), None)
    year = today.year
    if first and (first[0], first[1]) > (today.month, today.day):
        year -= 1

    out, prev_month = [], None
    for p in parsed:
        if not p:
            out.append(None)
            continue
        month, day = p
        if prev_month is not None and month > prev_month:
            year -= 1
        prev_month = month
        try:
            out.append(datetime.date(year, month, day).isoformat())
        except ValueError:
            out.append(None)
    return out


# An apply link must be absolute with a real hostname. Anything else becomes a
# dead "Apply" button, and a relative one would resolve against the dashboard's
# own domain and 404 on our own site rather than going anywhere.
LINK_OK = re.compile(
    r"^https?://[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?\.[A-Za-z]{2,}(?::\d+)?(?:[/?#]|$)")


def fix_link(u):
    u = (u or "").strip()
    # vanshb03 publishes "https:/.workable.com/..." for what is really
    # apply.workable.com -- an upstream typo, verified to repair to live pages.
    u = u.replace("https:/.workable.com/", "https://apply.workable.com/")
    return re.sub(r"[?&]utm_source=[^&]*", "", u)


def rec(company, role, location, link, posted, source,
        sponsorship="unknown", salary=None):
    company = " ".join((company or "").split())
    role = " ".join((role or "").split())
    link = fix_link(link)
    if not company or not role or not LINK_OK.match(link):
        return None
    return {
        "company": company,
        "role": role,
        "location": clean_loc(location) or "—",
        "link": link,
        "posted": posted,
        "salary": salary,
        "sponsorship": sponsorship,
        "sources": [source],
        "tier": tier(company),
        "is_ml": is_ml(role),
    }


# --------------------------------------------------------------------------
# Sources
# --------------------------------------------------------------------------

def from_simplify():
    """Structured JSON -- the only source with true posting timestamps."""
    data = get(SIMPLIFY_URL, as_json=True)
    smap = {
        "Does Not Offer Sponsorship": "none",
        "U.S. Citizenship is Required": "citizen",
        "Offers Sponsorship": "ok",
    }
    out = []
    for x in data:
        if not (x.get("active") and x.get("is_visible")):
            continue
        if not SIMPLIFY_CATEGORIES.search(x.get("category") or ""):
            continue
        title = x.get("title") or ""
        if SENIOR.search(title):
            continue
        posted = None
        if x.get("date_posted"):
            try:
                posted = datetime.datetime.fromtimestamp(
                    int(x["date_posted"]), datetime.timezone.utc).date().isoformat()
            except (ValueError, OSError, OverflowError):
                posted = None
        r = rec(x.get("company_name"), title, ", ".join(x.get("locations") or []),
                x.get("url"), posted, "simplify",
                sponsorship=smap.get(x.get("sponsorship"), "unknown"))
        if r:
            out.append(r)
    return out


def from_speedyapply():
    """Markdown table with an explicit age-in-days column, plus salary.

    The file holds several tables whose columns differ -- some carry a Salary
    column, some do not -- so locate each field by shape rather than by index.
    A positional parse silently drops every row of the narrower tables.
    """
    md = get(SPEEDY_URL)
    today = datetime.date.today()
    out = []
    for line in md.split("\n"):
        if not line.startswith("| <a"):
            continue
        parts = [p.strip() for p in line.strip().strip("|").split("|")]
        if len(parts) < 4:
            continue
        company = strip_tags(parts[0])
        role = strip_tags(parts[1])
        if SENIOR.search(role):
            continue
        location = parts[2]
        salary = next((p for p in parts if re.match(r"^\$[\d.]+k?", p)), None)
        link = next((m.group(1) for m in
                     (re.search(r'href="([^"]+)"', p) for p in parts[3:]) if m), None)
        posted = None
        age = next((re.match(r"^(\d+)d$", strip_tags(p)) for p in reversed(parts)
                    if re.match(r"^(\d+)d$", strip_tags(p))), None)
        if age:
            posted = (today - datetime.timedelta(days=int(age.group(1)))).isoformat()
        r = rec(company, role, location, link, posted, "speedyapply", salary=salary)
        if r:
            out.append(r)
    return out


def from_jobright():
    """Markdown table, bare month/day dates, newest-first."""
    md = get(JOBRIGHT_URL)
    rows = []
    for line in md.split("\n"):
        if not line.startswith("| **["):
            continue
        parts = line.strip().strip("|").split("|")
        if len(parts) < 5:
            continue
        cm = re.search(r"\*\*\[([^\]]+)\]", parts[0])
        rm = re.search(r"\*\*\[([^\]]+)\]\(([^)]+)\)", parts[1])
        if not cm or not rm:
            continue
        rows.append((cm.group(1), rm.group(1), rm.group(2), parts[2], parts[4].strip()))

    years = walk_years([r[4] for r in rows])
    out = []
    for (company, role, link, loc, _), posted in zip(rows, years):
        if SENIOR.search(role):
            continue
        r = rec(company, role, loc, link, posted, "jobright")
        if r:
            out.append(r)
    return out


def from_vanshb03():
    """Markdown table. Carries sponsorship markers the other sources lack."""
    md = get(VANSH_URL)
    rows = []
    for line in md.split("\n"):
        if not line.startswith("| **"):
            continue
        parts = line.strip().strip("|").split("|")
        if len(parts) < 5:
            continue
        company = re.sub(r"\*\*", "", parts[0]).strip()
        role = parts[1].strip()
        if "\U0001F512" in role or "\U0001F512" in company:   # lock = closed
            continue
        m = re.search(r'href="([^"]+)"', parts[3])
        rows.append((company, role, parts[2], m.group(1) if m else None, parts[4].strip()))

    years = walk_years([r[4] for r in rows])
    out = []
    for (company, role, loc, link, _), posted in zip(rows, years):
        # Passport marker = no sponsorship, US flag = citizenship required.
        # These were previously stripped and discarded, silently hiding ~145
        # dead-end roles among the applicable ones.
        sponsorship = "unknown"
        if "\U0001F6C2" in role:
            sponsorship = "none"
        if "\U0001F1FA\U0001F1F8" in role:
            sponsorship = "citizen"
        role = re.sub(r"\s*[\U0001F393\U0001F6C2]|\s*\U0001F1FA\U0001F1F8", "", role).strip()
        if SENIOR.search(role):
            continue
        r = rec(company, role, loc, link, posted, "vanshb03", sponsorship=sponsorship)
        if r:
            out.append(r)
    return out


SOURCES = [
    ("simplify", from_simplify),
    ("speedyapply", from_speedyapply),
    ("jobright", from_jobright),
    ("vanshb03", from_vanshb03),
]

# jobright links bounce through a redirect; prefer a direct employer URL.
LINK_RANK = {"simplify": 0, "speedyapply": 1, "vanshb03": 2, "jobright": 3}


def canon_url(u):
    return (u or "").split("?")[0].split("#")[0].rstrip("/").lower()


# A job-specific URL carries an id (Greenhouse/Lever/Ashby/Workday all do).
# A bare careers landing page does not, and must never be used to merge.
JOB_URL_ID = re.compile(r"/[^/]*(\d{5,}|[0-9a-f]{8}-[0-9a-f]{4})", re.I)


def fold(cur, r):
    """Fold duplicate record r into the record we are keeping."""
    src = r["sources"][0]
    for s in r["sources"]:
        if s not in cur["sources"]:
            cur["sources"].append(s)
    # Earliest known posting date wins: it is the true first appearance.
    if r["posted"] and (not cur["posted"] or r["posted"] < cur["posted"]):
        cur["posted"] = r["posted"]
    if LINK_RANK.get(src, 9) < LINK_RANK.get(cur["link_src"], 9):
        cur["link"] = r["link"]
        cur["link_src"] = src
    if cur["sponsorship"] == "unknown" and r["sponsorship"] != "unknown":
        cur["sponsorship"] = r["sponsorship"]
    if not cur.get("salary") and r.get("salary"):
        cur["salary"] = r["salary"]
    cur["tier"] = min(cur["tier"], r["tier"])
    cur["is_ml"] = cur["is_ml"] or r["is_ml"]


def merge(all_rows):
    """Collapse the same job seen in multiple trackers into one record."""
    merged, order = {}, []
    for r in all_rows:
        key = norm_key(r["company"]) + "|" + norm_key(r["role"])
        if key not in merged:
            r["id"] = key
            r["link_src"] = r["sources"][0]
            merged[key] = r
            order.append(key)
        else:
            fold(merged[key], r)
    jobs = [merged[k] for k in order]

    # Second pass. Trackers word the same posting differently -- "Software
    # Engineer 1 New Grad - QA" vs "Software Engineer I - QA - New Grad" --
    # so title matching misses them, but they share one apply URL. Only fold
    # URLs carrying a job id: shared careers landing pages such as
    # /open-vacancies host genuinely distinct roles and must stay separate.
    by_url, out = {}, []
    for r in jobs:
        cu = canon_url(r["link"])
        if cu and JOB_URL_ID.search(cu):
            if cu in by_url:
                fold(by_url[cu], r)
                continue
            by_url[cu] = r
        out.append(r)

    for r in out:
        r["sources"].sort()
        r.pop("link_src", None)
    return out


def carry_first_seen(jobs):
    """Preserve when we first saw each listing; stamp genuinely new ones."""
    now = datetime.datetime.now(datetime.timezone.utc).replace(
        microsecond=0).isoformat().replace("+00:00", "Z")
    previous, by_url = {}, {}
    if os.path.exists(OUT_PATH):
        try:
            with open(OUT_PATH, encoding="utf-8") as f:
                for old in json.load(f):
                    seen = old.get("first_seen")
                    if not seen:
                        continue
                    if old.get("id"):
                        previous[old["id"]] = seen
                    # Secondary index, see the fallback below.
                    cu = canon_url(old.get("link"))
                    if cu and JOB_URL_ID.search(cu):
                        if cu not in by_url or seen < by_url[cu]:
                            by_url[cu] = seen
        except (ValueError, OSError) as e:
            print(f"Warning: could not read previous {OUT_PATH}: {e}", file=sys.stderr)

    fresh = 0
    for j in jobs:
        seen = previous.get(j["id"])
        if seen is None:
            # The id is derived from company+role, so it moves when a tracker
            # retitles a role, and when a source outage makes a differently
            # worded duplicate the surviving record. Both would otherwise
            # forge a NEW badge for a job we have had all along, so fall back
            # to the apply URL, which is stable across both.
            cu = canon_url(j["link"])
            if cu and JOB_URL_ID.search(cu):
                seen = by_url.get(cu)
        if seen is not None:
            j["first_seen"] = seen
        else:
            j["first_seen"] = now
            fresh += 1
    if not previous:
        # On a first-ever run everything is "new", which is noise, not signal.
        print("No previous data: first_seen seeded for all listings")
    else:
        print(f"New since last run: {fresh}")
    return jobs


def main():
    all_rows, ok = [], []
    for name, fn in SOURCES:
        try:
            rows = fn()
        except (urllib.error.URLError, ValueError, OSError) as e:
            # Degrade, never fail: one bad source must not lose the other three.
            print(f"  {name:<12} FAILED: {e}", file=sys.stderr)
            continue
        print(f"  {name:<12} {len(rows):>5} rows")
        all_rows.extend(rows)
        ok.append(name)

    if len(ok) < 2:
        print(f"Only {len(ok)} source(s) succeeded; refusing to overwrite "
              f"{OUT_PATH}", file=sys.stderr)
        sys.exit(1)

    jobs = merge(all_rows)
    if len(jobs) < 200:
        print(f"Only {len(jobs)} merged jobs, far below expected volume; "
              f"refusing to overwrite {OUT_PATH}", file=sys.stderr)
        sys.exit(1)

    jobs = carry_first_seen(jobs)
    jobs.sort(key=lambda j: (j["posted"] or "0000-00-00", j["first_seen"]), reverse=True)

    with open(OUT_PATH, "w", encoding="utf-8", newline="\n") as f:
        json.dump(jobs, f, indent=1)
        f.write("\n")

    print(f"Wrote {len(jobs)} jobs from {len(ok)} sources "
          f"({', '.join(ok)}) to {OUT_PATH}")


if __name__ == "__main__":
    main()
