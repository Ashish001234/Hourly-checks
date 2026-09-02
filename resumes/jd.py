#!/usr/bin/env python3
"""
Fetch the job description for a listing.

Roughly half of the board links to an applicant-tracking system with a public
JSON endpoint; the rest link to arbitrary career pages. This tries the
structured endpoint first and falls back to scraping visible text, reporting a
confidence level either way so downstream steps know how much to trust it.

Probe coverage across the live board:

  py -3 resumes/jd.py --probe 40
"""
import argparse
import html
import json
import os
import re
import sys
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
SITE = os.path.dirname(HERE)

UA = {"User-Agent": "Mozilla/5.0 (compatible; grad-job-dashboard/1.0)"}
TIMEOUT = 30

# Below this a "description" is a cookie banner or a nav bar, not a JD.
MIN_USABLE = 600


def _get(url, payload=None, timeout=TIMEOUT):
    headers = dict(UA)
    body = None
    if payload is not None:
        body = json.dumps(payload).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", "replace")


def _json(url, payload=None):
    return json.loads(_get(url, payload))


def clean(raw):
    """HTML (or plain text) to readable prose.

    Unescape *before* stripping tags, and repeat. Greenhouse returns the
    description double-encoded -- the JSON string holds `&lt;p&gt;` -- so a
    strip-then-unescape order leaves live <p> and <strong> tags in the output,
    which then reach the model as noise and waste prompt budget.
    """
    if not raw:
        return ""
    txt = raw
    for _ in range(3):                       # &amp;lt; -> &lt; -> <
        nxt = html.unescape(txt)
        if nxt == txt:
            break
        txt = nxt
    txt = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", txt)
    txt = re.sub(r"(?i)<(br|/p|/div|/li|/h[1-6]|/tr)[^>]*>", "\n", txt)
    txt = re.sub(r"<[^>]+>", " ", txt)
    txt = html.unescape(txt)                 # entities revealed by tag removal
    # Smart punctuation often arrives as Windows-1252 mislabelled UTF-8 and
    # renders as U+FFFD mid-sentence; normalise so the model sees clean prose.
    for a, b in (("\u00a0", " "), ("\u2019", "'"), ("\u2018", "'"),
                 ("\u201c", '"'), ("\u201d", '"'),
                 ("\u2013", "-"), ("\u2014", " -- "), ("\ufffd", "")):
        txt = txt.replace(a, b)
    txt = re.sub(r"[ \t]+", " ", txt)
    txt = re.sub(r"\n\s*\n\s*\n+", "\n\n", txt)
    return txt.strip()


# --------------------------------------------------------------------------
# Platform adapters. Each takes the apply URL and returns raw description text
# (HTML or plain), or None when this platform does not apply / has no JD.
# --------------------------------------------------------------------------

def _greenhouse(url):
    m = re.search(r"(?:boards|job-boards)\.greenhouse\.io/([a-z0-9_-]+)/jobs/(\d+)", url, re.I)
    if not m:
        return None
    d = _json("https://boards-api.greenhouse.io/v1/boards/%s/jobs/%s?content=true"
              % (m.group(1), m.group(2)))
    return d.get("content")


def _lever(url):
    m = re.search(r"jobs\.lever\.co/([a-z0-9_-]+)/([0-9a-f-]{8,})", url, re.I)
    if not m:
        return None
    d = _json("https://api.lever.co/v0/postings/%s/%s" % (m.group(1), m.group(2)))
    return d.get("descriptionPlain") or d.get("description")


def _ashby(url):
    m = re.search(r"jobs\.ashbyhq\.com/([a-z0-9_.-]+)/([0-9a-f-]{8,})", url, re.I)
    if not m:
        return None
    # Ashby has no per-job endpoint; the board response carries every
    # description, so pull the board and pick the one we want.
    d = _json("https://api.ashbyhq.com/posting-api/job-board/%s" % m.group(1))
    for job in d.get("jobs", []):
        if job.get("id") == m.group(2):
            return job.get("descriptionPlain") or job.get("descriptionHtml")
    return None


def _smartrecruiters(url):
    m = re.search(r"jobs\.smartrecruiters\.com/([a-z0-9_-]+)/(\d+)", url, re.I)
    if not m:
        return None
    d = _json("https://api.smartrecruiters.com/v1/companies/%s/postings/%s"
              % (m.group(1), m.group(2)))
    ad = d.get("jobAd") or {}
    secs = (ad.get("sections") or {})
    parts = []
    for key in ("companyDescription", "jobDescription", "qualifications", "additionalInformation"):
        s = secs.get(key) or {}
        if s.get("text"):
            parts.append(s["text"])
    return "\n\n".join(parts) or None


def _workday(url):
    # https://{tenant}.{wd}.myworkdayjobs.com[/{locale}]/{site}/job/{rest}
    #   -> https://{tenant}.{wd}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/job/{rest}
    m = re.search(
        r"https://([a-z0-9-]+)\.(wd\d+)\.myworkdayjobs\.com/(?:[a-z]{2}-[A-Z]{2}/)?([^/]+)/job/(.+)",
        url, re.I)
    if not m:
        return None
    tenant, wd, site, rest = m.groups()
    d = _json("https://%s.%s.myworkdayjobs.com/wday/cxs/%s/%s/job/%s"
              % (tenant, wd, tenant, site, rest.split("?")[0]))
    return ((d.get("jobPostingInfo") or {}).get("jobDescription")) or None


def _oracle(url):
    """Oracle Cloud HCM (CandidateExperience) -- American Express and many
    other large employers. The careers page is a JS app that scrapes to
    nothing, but the REST API behind it is public."""
    m = re.search(
        r"https://([^/]+\.oraclecloud\.com)/hcmUI/CandidateExperience/[^/]+/sites/([^/]+)/job/(\d+)",
        url, re.I)
    if not m:
        return None
    host, site, jid = m.groups()
    d = _json("https://%s/hcmRestApi/resources/latest/"
              "recruitingCEJobRequisitionDetails?expand=all&"
              "finder=ById;Id=%%22%s%%22,siteNumber=%s" % (host, jid, site))
    items = d.get("items") or []
    if not items:
        return None
    it = items[0]
    return it.get("ExternalDescriptionStr") or it.get("ShortDescriptionStr")


ADAPTERS = [
    ("greenhouse", _greenhouse),
    ("lever", _lever),
    ("ashby", _ashby),
    ("smartrecruiters", _smartrecruiters),
    ("workday", _workday),
    ("oracle", _oracle),
]


# Pages that return HTTP 200 but carry no job description: bot walls, JS
# shells, login gates, dead links. These are worse than an empty result --
# jobright's bot check reads as a JD full of "JavaScript ... security", which
# scored as a real keyword profile and would have tailored 31 resumes to a
# CAPTCHA page.
JUNK = re.compile(
    r"security check|are you a human|enable javascript|javascript is required"
    r"|please turn on javascript|verify you are human|checking your browser"
    r"|cloudflare|access denied|403 forbidden|404 not found|page not found"
    r"|sign in to continue|log in to view|cookies? (are )?(required|disabled)"
    r"|this job is no longer|position (has been )?filled|no longer accepting",
    re.I)


def looks_like_jd(txt):
    """Reject pages that loaded fine but contain no job description."""
    if not txt or len(txt) < MIN_USABLE:
        return False
    if JUNK.search(txt[:1200]):
        return False
    # A real posting names responsibilities or requirements somewhere.
    return bool(re.search(
        r"responsibilit|qualificat|requirement|you will|we are looking"
        r"|what you|experience with|skills|role|team|degree",
        txt, re.I))


def _scrape(url):
    """Last resort for the ~50% of listings on arbitrary career pages."""
    raw = _get(url)
    txt = clean(raw)
    # Career pages are mostly chrome. Keep the longest run of real prose.
    blocks = [b.strip() for b in txt.split("\n\n") if len(b.strip()) > 80]
    return "\n\n".join(blocks) if blocks else txt


def fetch_jd(job):
    """-> {text, source, confidence}. Never raises."""
    url = job.get("link") or ""
    for name, fn in ADAPTERS:
        try:
            raw = fn(url)
        except (urllib.error.URLError, ValueError, OSError, TimeoutError):
            continue
        if raw:
            txt = clean(raw)
            if len(txt) >= MIN_USABLE:
                return {"text": txt, "source": name, "confidence": "high"}
            if txt:
                return {"text": txt, "source": name, "confidence": "low"}
    try:
        txt = _scrape(url)
        if looks_like_jd(txt):
            return {"text": txt, "source": "scrape", "confidence": "medium"}
        # Short, or a bot wall / JS shell / dead link. Either way it is not a
        # job description, and passing it downstream is worse than admitting we
        # have nothing: the tailoring would target whatever words the wall used.
    except (urllib.error.URLError, ValueError, OSError, TimeoutError):
        pass

    # Nothing fetchable. Tailoring still runs, but off the title alone, and the
    # result is labelled so it is never mistaken for a JD-matched resume.
    stub = "%s at %s. Location: %s." % (
        job.get("role", ""), job.get("company", ""), job.get("location", ""))
    return {"text": stub, "source": "none", "confidence": "none"}


# --------------------------------------------------------------------------

def _probe(n):
    jobs = json.load(open(os.path.join(SITE, "data", "jobs.json"), encoding="utf-8"))
    pats = {
        "greenhouse": r"greenhouse\.io", "lever": r"jobs\.lever\.co",
        "ashby": r"ashbyhq\.com", "smartrecruiters": r"smartrecruiters\.com",
        "workday": r"myworkdayjobs\.com", "workable": r"workable\.com",
    }
    buckets, other = {k: [] for k in pats}, []
    for j in jobs:
        link = j.get("link") or ""
        for k, p in pats.items():
            if re.search(p, link, re.I):
                buckets[k].append(j)
                break
        else:
            other.append(j)
    buckets["other"] = other

    per = max(2, n // (len(buckets) or 1))
    sample = [(k, j) for k, v in buckets.items() for j in v[:per]]
    print("probing %d listings across %d groups\n" % (len(sample), len(buckets)))

    def one(item):
        k, j = item
        return k, j, fetch_jd(j)

    stats = {}
    with ThreadPoolExecutor(max_workers=8) as pool:
        for k, j, res in pool.map(one, sample):
            s = stats.setdefault(k, {"n": 0, "ok": 0, "lens": [], "src": {}})
            s["n"] += 1
            s["src"][res["source"]] = s["src"].get(res["source"], 0) + 1
            if res["confidence"] in ("high", "medium"):
                s["ok"] += 1
                s["lens"].append(len(res["text"]))

    print("%-16s %5s %6s %9s  %s" % ("group", "n", "usable", "median", "sources"))
    for k in sorted(stats):
        s = stats[k]
        med = sorted(s["lens"])[len(s["lens"]) // 2] if s["lens"] else 0
        print("%-16s %5d %5d%%  %8d  %s" % (
            k, s["n"], round(100 * s["ok"] / max(s["n"], 1)), med,
            ", ".join("%s=%d" % kv for kv in sorted(s["src"].items()))))
    tot_n = sum(s["n"] for s in stats.values())
    tot_ok = sum(s["ok"] for s in stats.values())
    print("\noverall usable: %d/%d (%d%%)" % (tot_ok, tot_n, round(100 * tot_ok / max(tot_n, 1))))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--probe", type=int, metavar="N", help="sample N listings and report coverage")
    ap.add_argument("--url", help="fetch one apply URL and print the JD")
    args = ap.parse_args()
    if args.url:
        r = fetch_jd({"link": args.url, "role": "", "company": "", "location": ""})
        print("source=%s confidence=%s chars=%d" % (r["source"], r["confidence"], len(r["text"])))
        print("-" * 60)
        print(r["text"][:2000])
    elif args.probe:
        _probe(args.probe)
    else:
        ap.error("pass --probe N or --url URL")


if __name__ == "__main__":
    main()
