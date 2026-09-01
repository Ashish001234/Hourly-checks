#!/usr/bin/env python3
"""
Direct polling of company applicant-tracking-system (ATS) job boards.

Why this exists
---------------
The GitHub trackers we also read are themselves scrapers pointed at these very
boards, so they are structurally behind. Indeed and LinkedIn are downstream of
them too -- and both prohibit automated access, LinkedIn explicitly in its
robots.txt. Polling the ATS directly puts us *upstream* of all of them: these
are the systems a company posts into, so a role appears here first.

Every endpoint below is public and unauthenticated. No API keys, no scraping
of rendered HTML -- these are the JSON feeds the companies' own careers pages
call. Requests are pooled with a modest worker count to stay a good citizen.

The board registry (data/boards.json) is bootstrapped from apply URLs already
present in our merged listings, so the target list grows on its own -- see
discover_boards().
"""
import json
import re
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

UA = {"User-Agent": "grad-job-dashboard (github.com/Ashish001234/Hourly-checks)"}
TIMEOUT = 25
WORKERS = 12


def _get(url, payload=None, timeout=TIMEOUT):
    headers = dict(UA)
    body = None
    if payload is not None:
        body = json.dumps(payload).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def _epoch(value, fmt="%Y-%m-%dT%H:%M:%S"):
    if not value:
        return None
    try:
        return int(time.mktime(time.strptime(str(value)[:19], fmt)))
    except (ValueError, OverflowError):
        return None


# --------------------------------------------------------------------------
# Platform adapters. Each takes a board token and returns
# [(title, location, url, posted_epoch_or_None)].
# --------------------------------------------------------------------------

def greenhouse(token):
    d = _get(f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs")
    return [(j.get("title"), (j.get("location") or {}).get("name"),
             j.get("absolute_url"), _epoch(j.get("updated_at")))
            for j in d.get("jobs", [])]


def lever(token):
    d = _get(f"https://api.lever.co/v0/postings/{token}?mode=json")
    return [(j.get("text"), (j.get("categories") or {}).get("location"),
             j.get("hostedUrl"),
             int(j["createdAt"] / 1000) if j.get("createdAt") else None)
            for j in d]


def ashby(token):
    d = _get(f"https://api.ashbyhq.com/posting-api/job-board/{token}")
    return [(j.get("title"), j.get("location"), j.get("jobUrl"),
             _epoch(j.get("publishedAt")))
            for j in d.get("jobs", [])]


def smartrecruiters(token):
    d = _get(f"https://api.smartrecruiters.com/v1/companies/{token}/postings?limit=100")
    out = []
    for j in d.get("content", []):
        loc = j.get("location") or {}
        where = ", ".join(x for x in (loc.get("city"), loc.get("region"),
                                      loc.get("country")) if x)
        out.append((j.get("name"), where,
                    f"https://jobs.smartrecruiters.com/{token}/{j.get('id')}",
                    _epoch(j.get("releasedDate"))))
    return out


def workable(token):
    d = _get(f"https://apply.workable.com/api/v1/widget/accounts/{token}")
    out = []
    for j in d.get("jobs", []):
        where = ", ".join(x for x in (j.get("city"), j.get("state"),
                                      j.get("country")) if x)
        out.append((j.get("title"), where, j.get("url") or j.get("shortlink"),
                    _epoch(j.get("published_on"), "%Y-%m-%d")))
    return out


_WD_DAYS = re.compile(r"(\d+)\+?\s*days?\s*ago", re.I)
_WD_TODAY = re.compile(r"today|just posted", re.I)


def workday(spec):
    """spec is "tenant|wdN|site" -- Workday needs all three to address a board."""
    tenant, wd, site = spec.split("|")
    url = f"https://{tenant}.{wd}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs"
    d = _get(url, payload={"limit": 20, "offset": 0, "searchText": ""})
    out = []
    now = int(time.time())
    for j in d.get("jobPostings", []):
        # Workday reports "Posted 3 Days Ago", never a timestamp.
        posted, ts = j.get("postedOn") or "", None
        m = _WD_DAYS.search(posted)
        if m:
            ts = now - int(m.group(1)) * 86400
        elif _WD_TODAY.search(posted):
            ts = now
        out.append((j.get("title"), j.get("locationsText"),
                    f"https://{tenant}.{wd}.myworkdayjobs.com/{site}"
                    f"{j.get('externalPath') or ''}", ts))
    return out


ADAPTERS = {
    "greenhouse": greenhouse,
    "lever": lever,
    "ashby": ashby,
    "smartrecruiters": smartrecruiters,
    "workable": workable,
    "workday": workday,
}

# --------------------------------------------------------------------------
# Relevance. A raw board returns everything the company is hiring for -- one
# Greenhouse board alone can be 800 roles -- so filtering carries the quality
# of the whole feed. Precision matters more than recall here: a board you stop
# trusting is worse than one that misses a listing.
# --------------------------------------------------------------------------

SENIOR = re.compile(
    r"senior|staff|principal|\blead\b|manager|director|\bsr\.?\b|architect|\bvp\b|"
    r"head of|\bII+\b|\b[3-9]\+?\s*years?\b|executive|chief|\bfellow\b", re.I)

INTERN = re.compile(
    r"\bintern\b|internship|\bco-?op\b|apprentice|placement year|working student", re.I)

# Must read as a software / data / ML role. Matching a bare "engineer" pulls in
# every other engineering discipline, so require a specific term.
SOFTWARE = re.compile(
    r"software|developer|programmer|\bsde\b|web dev|mobile dev|\bios\b|android|"
    r"data (?:scien|engineer|analyst)|analytics engineer|machine learning|\bml\b|"
    r"\bai\b|artificial intelligence|deep learning|computer vision|\bnlp\b|\bllm\b|"
    r"research (?:scientist|engineer)|quant|back[ -]?end|front[ -]?end|full[ -]?stack|"
    r"devops|\bsre\b|site reliability|cloud engineer|platform engineer|"
    r"infrastructure engineer|security engineer|\bqa\b|test engineer|"
    r"robotics software|systems software|embedded software", re.I)

NEWGRAD = re.compile(
    r"new ?grad|university grad|college grad|entry[ -]?level|early[ -]career|"
    r"\bgraduate\b|campus|\bjunior\b|\bjr\.?\b|\bassociate\b|\b20(?:26|27)\b|"
    r"\b(?:engineer|developer|scientist|analyst)\s*(?:i|1)\b", re.I)

# Roles that satisfy the patterns above but are not what we want:
# "Roadway Design Engineer", "Finance Associate, Engineering".
EXCLUDE = re.compile(
    r"counsel|legal|attorney|paralegal|\bsales\b|marketing|recruit|human resources|"
    r"account (?:executive|manager)|\bfinance\b|accountant|nurse|clinical|therapist|"
    r"teacher|driver|mechanic|electrician|welder|custodian|warehouse|"
    r"\belectrical\b|\bmechanical\b|\bcivil\b|structural|roadway|highway|geotechnical|"
    r"manufacturing|flight test|\bcontrols?\b|chemical|industrial|process engineer|"
    r"field engineer|\brf\b|optical|thermal|packaging|petroleum|mining|environmental|"
    r"biomedical|aerospace engineer|\bmaterials\b", re.I)

US_STATES = {
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA", "HI", "ID", "IL",
    "IN", "IA", "KS", "KY", "LA", "ME", "MD", "MA", "MI", "MN", "MS", "MO", "MT",
    "NE", "NV", "NH", "NJ", "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA", "RI",
    "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY",
    "DC", "PR", "VI", "GU", "AS", "MP",
}

# Two-letter suffixes that are unambiguously NOT US states. Deliberately
# excludes codes that collide with real states -- DE is Delaware before it is
# Germany, IN is Indiana before it is India -- since the US check runs first.
NON_US_SUBDIV = {
    "ON", "BC", "QC", "AB", "MB", "SK", "NS", "NB", "PE", "YT", "NT", "NU",  # Canada
    "UK", "GB",                                                              # Britain
}

US_MARKER = re.compile(r"united states|\bU\.?S\.?A\.?\b|\bUS\b", re.I)
STATE_SUFFIX = re.compile(r",\s*([A-Za-z]{2})\b")
REMOTE = re.compile(r"\bremote\b|\banywhere\b", re.I)

# Enumerating every US location is hopeless, so exclude clearly non-US instead.
NON_US = re.compile(
    # Bare abbreviations too: "Remote in UK" has no comma, so the two-letter
    # suffix check never sees it and it would fall through to the remote rule.
    r"\bU\.?K\.?\b|united kingdom|\bengland\b|scotland|\bwales\b|ireland|canada|"
    r"\bindia\b|brazil|"
    r"mexico|germany|france|spain|portugal|netherlands|belgium|poland|romania|"
    r"ukraine|sweden|norway|denmark|finland|switzerland|austria|\bitaly\b|greece|"
    r"czech|hungary|israel|turkey|egypt|nigeria|kenya|south africa|australia|"
    r"new zealand|singapore|\bjapan\b|\bchina\b|korea|taiwan|hong kong|malaysia|"
    r"indonesia|thailand|vietnam|philippines|argentina|colombia|\bchile\b|\bperu\b|"
    r"costa rica|\blondon\b|dublin|manchester|edinburgh|toronto|vancouver|montreal|"
    r"ottawa|bangalore|bengaluru|hyderabad|\bpune\b|chennai|mumbai|\bdelhi\b|noida|"
    r"gurgaon|sao paulo|\bberlin\b|munich|hamburg|\bparis\b|amsterdam|madrid|"
    r"barcelona|lisbon|warsaw|krakow|bucharest|prague|budapest|tel aviv|dubai|"
    r"sydney|melbourne|\btokyo\b|seoul|shanghai|beijing|shenzhen|taipei|manila|"
    r"jakarta|bangkok|kuala lumpur|milton keynes|belfast|stevenage", re.I)


def is_us(location):
    """Is this location in the US?

    Order matters. A positive US signal has to win before any foreign-place
    name is consulted, because plenty of real US towns share a name with a
    foreign city -- "Berlin, NJ", "London, KY", "Vancouver, WA". Checking the
    blocklist first would quietly delete those.

    An unrecognized location with no foreign signal is kept, not dropped:
    boards routinely say just "Sunnyvale" or "SF Office", and silently losing
    those is worse than letting a rare foreign listing through.
    """
    loc = (location or "").strip()
    if not loc or loc == "—":
        return True
    if US_MARKER.search(loc):
        return True
    suffixes = {m.group(1).upper() for m in STATE_SUFFIX.finditer(loc)}
    if suffixes & US_STATES:
        return True
    if suffixes & NON_US_SUBDIV:
        return False
    if NON_US.search(loc):
        return False
    if REMOTE.search(loc):
        return True
    return True


def relevant(title, location):
    t = title or ""
    if not t:
        return False
    if SENIOR.search(t) or INTERN.search(t) or EXCLUDE.search(t):
        return False
    if not SOFTWARE.search(t) or not NEWGRAD.search(t):
        return False
    return is_us(location)


# --------------------------------------------------------------------------

def discover_boards(jobs):
    """Grow the board registry from apply URLs already in our merged listings.

    Every listing we carry links to wherever the company actually accepts
    applications, so the set of boards worth polling is derivable from the data
    itself rather than hand-maintained.
    """
    pats = [
        ("greenhouse", re.compile(r"(?:boards|job-boards)\.greenhouse\.io/([a-z0-9_-]+)", re.I)),
        ("lever", re.compile(r"jobs\.lever\.co/([a-z0-9_-]+)", re.I)),
        ("ashby", re.compile(r"jobs\.ashbyhq\.com/([a-z0-9_.-]+)", re.I)),
        ("smartrecruiters", re.compile(r"(?:careers|jobs)\.smartrecruiters\.com/([a-z0-9_-]+)", re.I)),
        ("workable", re.compile(r"apply\.workable\.com/([a-z0-9_-]+)", re.I)),
        ("workday", re.compile(r"([a-z0-9-]+)\.(wd\d+)\.myworkdayjobs\.com/(?:[a-z]{2}-[A-Z]{2}/)?([^/]+)/", re.I)),
    ]
    found = {name: {} for name, _ in pats}
    for job in jobs:
        url = job.get("link") or ""
        for name, pat in pats:
            m = pat.search(url)
            if not m:
                continue
            if name == "workday":
                token = "|".join(g.lower() for g in m.groups())
            else:
                token = m.group(1).lower()
                if name == "workable" and token == "j":   # /j/ is a job path
                    break
            # Carry the company name across: a board token like
            # "andurilindustries" is not what we want on screen, and the
            # listing that revealed the board already knows the real name.
            found[name].setdefault(token, job.get("company") or token)
            break
    return {k: dict(sorted(v.items())) for k, v in found.items() if v}


def poll(boards, workers=WORKERS):
    """Poll every board. Returns (records, stats).

    A board that errors is skipped, never raised: with ~900 boards some will
    always be renamed, retired, or briefly down, and one bad token must not
    cost us the other 899.
    """
    targets = [(plat, tok) for plat, toks in boards.items()
               if plat in ADAPTERS for tok in toks]
    names = {(p, t): (toks[t] if isinstance(toks, dict) else t)
             for p, toks in boards.items() for t in toks}
    stats = {p: {"ok": 0, "fail": 0, "raw": 0, "kept": 0} for p in ADAPTERS}
    records = []

    def probe(item):
        plat, tok = item
        try:
            return plat, tok, ADAPTERS[plat](tok), None
        except (urllib.error.URLError, ValueError, OSError, TimeoutError) as e:
            return plat, tok, None, e

    with ThreadPoolExecutor(max_workers=workers) as pool:
        for plat, tok, rows, err in pool.map(probe, targets):
            st = stats[plat]
            if err is not None:
                st["fail"] += 1
                continue
            st["ok"] += 1
            st["raw"] += len(rows)
            for title, loc, url, ts in rows:
                if relevant(title, loc):
                    st["kept"] += 1
                    records.append({
                        "platform": plat,
                        "board": tok,
                        "company": names.get((plat, tok)) or tok,
                        "title": (title or "").strip(),
                        "location": (loc or "").strip(),
                        "url": url,
                        "posted_ts": ts,
                    })
    return records, stats
