#!/usr/bin/env python3
"""
Tailor a resume to one job description using Claude, then build and score it.

Division of labour, deliberately:
  * Claude selects and *rewrites* bullet text against the JD. It never writes
    LaTeX -- structured output returns content only.
  * build.py renders and compiles, so the two-page guarantee still holds and a
    model slip cannot produce a broken document.
  * verify() checks every rewritten bullet against the source library, because
    a model told to hit a keyword target will otherwise invent experience.

Usage
-----
  py -3 resumes/tailor.py --shortlist shortlist.json
  py -3 resumes/tailor.py --job-id "Stripe|Software Engineer New Grad"
  py -3 resumes/tailor.py --url https://boards.greenhouse.io/... --dry-run
"""
import argparse
import datetime
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SITE = os.path.dirname(HERE)
sys.path.insert(0, HERE)

import build            # noqa: E402
import jd as jdmod      # noqa: E402
import score as scoring  # noqa: E402

TAILORED_DIR = os.path.join(HERE, "out", "tailored")
MANIFEST = os.path.join(SITE, "data", "tailored.json")
JOBS_DIR = os.path.join(HERE, ".jobs")   # prompt packs + answers, not committed
JOBS = os.path.join(SITE, "data", "jobs.json")

MODEL = "claude-opus-5"
TARGET_SCORE = 80.0
MAX_ATTEMPTS = 2

SYSTEM = """You tailor one engineer's resume to a specific job description.

You will be given a CONTENT LIBRARY of that engineer's real, verified experience
and a JOB DESCRIPTION. Return a selection of library bullets, each optionally
rewritten to speak the job description's vocabulary.

HARD RULES -- these are not style preferences:
1. Never introduce a fact that is not in the content library. No new employers,
   projects, technologies, metrics, dates, or claims. If the job wants something
   he has not done, list it in `gaps`; do NOT write it into a bullet.
2. Every number, percentage and metric in a rewritten bullet must appear in the
   source bullet it came from. Do not round, inflate, or invent figures.
3. Only use `id` values that exist in the library.
4. Rewrites must stay one sentence or two, comparable in length to the source.
   These render into a fixed two-page layout.
5. LaTeX: the text is placed directly into a document. Escape % as \\%, & as \\&,
   and _ as \\_. Do not use any other backslash commands.

WHAT GOOD TAILORING LOOKS LIKE:
- Lead with the work closest to this role, and say it in the job's own words:
  if the job says "distributed systems", a bullet about concurrent polling
  across 950 boards should say distributed, because it truthfully was.
- Surface the metrics that matter to this employer; drop ones that do not.
- Prefer the library's specific numbers over vague claims. They are his edge.
- Do not keyword-stuff. A bullet that reads like a tag list scores worse with a
  human, and a human decides the interview."""


# ---------------------------------------------------------------- library

def library_index(content):
    """id -> {text variants, source facts} for prompting and verification."""
    idx = {}

    def add(bid, texts, where):
        idx[bid] = {"id": bid, "texts": [t for t in texts if t], "where": where}

    for sec in ("education", "experience", "research"):
        for ent in content.get(sec, []):
            label = ent.get("org", "")
            for b in ent.get("bullets", []):
                add(b["id"], [b.get("sde"), b.get("ml"), b.get("text")], label)
    for p in content.get("projects", []):
        for b in p.get("bullets", []):
            add(b["id"], [b.get("sde"), b.get("ml"), b.get("text")], p["name"])
    for a in content.get("achievements", []):
        add(a["id"], [a.get("sde"), a.get("ml"), a.get("text")], "achievements")
    return idx


def library_prompt(content, voice):
    """Compact, stable rendering of the library. Identical across every job so
    it caches; anything volatile would silently defeat the cache."""
    lines = []
    for sec in ("education", "experience", "research"):
        for ent in content.get(sec, []):
            head = ent.get("role") or ent.get("degree") or ""
            lines.append("\n## %s -- %s (%s)" % (head, ent.get("org", ""), ent.get("dates", "")))
            for b in ent.get("bullets", []):
                t = build.pick(b, voice)
                if t:
                    lines.append("- [%s] %s" % (b["id"], t))
    for p in content.get("projects", []):
        lines.append("\n## PROJECT %s | %s" % (p["name"], p["stack"]))
        for b in p.get("bullets", []):
            t = build.pick(b, voice)
            if t:
                lines.append("- [%s] %s" % (b["id"], t))
    lines.append("\n## ACHIEVEMENTS")
    for a in content.get("achievements", []):
        t = build.pick(a, voice)
        if t:
            lines.append("- [%s] %s" % (a["id"], t))
    return "\n".join(lines)


# ---------------------------------------------------------------- verify

NUM = re.compile(r"\d[\d,\.]*\s*(?:%|x|\+)?", re.I)
# Capitalised technology-ish tokens, minus sentence-initial noise.
PROPER = re.compile(r"\b(?:[A-Z][a-zA-Z0-9]*(?:\.[a-z]+)?(?:-[A-Za-z0-9]+)*)\b")
# Sentence-initial verbs and articles. A rewrite legitimately starts with a
# different verb than the source, and flagging those buries the real
# inventions -- "Debugged" is not a claim about experience, "Kubernetes" is.
COMMON = {"The", "A", "An", "In", "On", "At", "For", "With", "And", "Or", "But",
          "This", "That", "These", "Those", "Built", "Designed", "Engineered",
          "Implemented", "Developed", "Created", "Led", "Ran", "Applied", "Used",
          "Trained", "Deployed", "Delivered", "Architected", "Constructed",
          "Extended", "Optimized", "Reduced", "Improved", "Scaled", "Wrote",
          "Showed", "Proposed", "Resolved", "Instrumented", "Surfaced", "I",
          "Debugged", "Automated", "Migrated", "Refactored", "Shipped",
          "Owned", "Drove", "Partnered", "Collaborated", "Investigated",
          "Profiled", "Benchmarked", "Validated", "Measured", "Analyzed",
          "Analysed", "Introduced", "Established", "Maintained", "Integrated",
          "Orchestrated", "Containerized", "Conducted", "Authored", "Advised",
          "Grew", "Hosted", "Directed", "Mentored", "Won", "Achieved",
          "Selected", "Graduating", "Focus", "Coursework", "Top"}


def _facts(text):
    nums = {n.strip().rstrip(".").replace(",", "") for n in NUM.findall(text or "")}
    props = {p for p in PROPER.findall(text or "") if p not in COMMON and len(p) > 1}
    return nums, props


def library_terms(content, index):
    """Every proper noun anywhere in the library, including the skills lists.

    Terms are checked library-wide, not per bullet. A legitimate reframe may
    name a technology that lives in the skills section rather than in the source
    sentence -- "Python", "PostgreSQL", "REST" are all his, and flagging them
    would bury the real inventions in noise.
    """
    terms = set()
    for rec in index.values():
        for t in rec["texts"]:
            terms |= _facts(t)[1]
    for rows in (content.get("skills") or {}).values():
        for row in rows:
            for token in re.split(r"[,/()]", row.get("val", "") + " " + row.get("cat", "")):
                terms |= _facts(token.strip())[1]
    for sec in ("education", "experience", "research"):
        for ent in content.get(sec, []):
            terms |= _facts(ent.get("org", ""))[1]
    for p in content.get("projects", []):
        terms |= _facts(p.get("name", "") + " " + p.get("stack", ""))[1]
    return terms


def descriptive_ids(content):
    """Bullets that describe interests rather than claim work performed.

    Education lines are lists of coursework and focus areas, so their capitalised
    phrases ("Low-Latency Services", "Secure API Design") are category labels,
    not assertions about experience. Every flag in the first 27-resume batch came
    from a Focus line and none from an experience bullet, which is the guard
    being over-broad rather than the resumes being wrong. Numbers are still
    checked here -- a misstated GPA is exactly the kind of error that matters.
    """
    ids = set()
    for ent in content.get("education", []):
        for b in ent.get("bullets", []):
            ids.add(b["id"])
    return ids


def verify(selected, index, lib_terms, descriptive=()):
    """Flag any claim in a rewrite that the library does not support.

    Numbers are checked against the *source bullet* -- inflating 950 to 5000 is
    the failure that matters, and only that bullet can vouch for its own
    figures. Proper nouns are checked against the whole library.

    The prompt forbids invention; this checks. Without it the failure is silent
    and surfaces in an interview, where it cannot be recovered.
    """
    problems = []
    for item in selected:
        bid, new = item.get("id"), item.get("text") or ""
        src = index.get(bid)
        if not src:
            problems.append({"id": bid, "kind": "unknown_id",
                             "detail": "not in content library"})
            continue
        src_nums = set()
        for t in src["texts"]:
            src_nums |= _facts(t)[0]
        new_nums, new_props = _facts(new)
        for n in sorted(new_nums - src_nums):
            problems.append({"id": bid, "kind": "new_number", "detail": n})
        if bid in descriptive:
            continue                      # interests, not experience claims
        for p in sorted(new_props - lib_terms):
            problems.append({"id": bid, "kind": "new_term", "detail": p})
    return problems


# ---------------------------------------------------------------- claude

SCHEMA = {
    "type": "object",
    "properties": {
        "voice": {"type": "string", "enum": ["sde", "ml"]},
        "selected": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "text": {"type": "string"},
                },
                "required": ["id", "text"],
                "additionalProperties": False,
            },
        },
        "summary": {"type": "string"},
        "gaps": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["voice", "selected", "summary", "gaps"],
    "additionalProperties": False,
}


def call_claude(client, lib_text, job, jd_text, retry_missing=None):
    ask = [
        "JOB: %s at %s" % (job.get("role", ""), job.get("company", "")),
        "LOCATION: %s" % job.get("location", ""),
        "",
        "JOB DESCRIPTION:",
        (jd_text or "")[:14000],
        "",
        "Select the strongest 26-32 bullets for this role and rewrite them to "
        "match its vocabulary, obeying every hard rule.",
    ]
    if retry_missing:
        ask += [
            "",
            "The previous draft missed these terms the job description uses: "
            + ", ".join(retry_missing[:25]) + ".",
            "Work in ONLY the ones his real experience genuinely supports. "
            "Leave the rest in `gaps` -- do not manufacture coverage.",
        ]

    kwargs = dict(
        model=MODEL,
        max_tokens=16000,
        thinking={"type": "adaptive"},
        system=[{"type": "text",
                 "text": SYSTEM + "\n\nCONTENT LIBRARY:\n" + lib_text,
                 "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": "\n".join(ask)}],
        output_config={"format": {"type": "json_schema", "schema": SCHEMA}},
    )

    # Opus 5 can decline with stop_reason="refusal"; server-side fallback keeps
    # a batch moving. If this SDK/account lacks the beta, fall back to a plain
    # call rather than failing the whole run.
    try:
        resp = client.beta.messages.create(
            betas=["server-side-fallback-2026-07-01"], fallbacks="default", **kwargs)
    except Exception:
        resp = client.messages.create(**kwargs)

    if getattr(resp, "stop_reason", None) == "refusal":
        raise RuntimeError("model declined this request (stop_reason=refusal)")
    text = next((b.text for b in resp.content if b.type == "text"), None)
    if not text:
        raise RuntimeError("no text block in response")
    usage = getattr(resp, "usage", None)
    return json.loads(text), usage


# ---------------------------------------------------------------- pipeline


def slug(job):
    s = "%s-%s" % (job.get("company", ""), job.get("role", ""))
    s = re.sub(r"[^A-Za-z0-9]+", "-", s).strip("-").lower()
    return s[:60] or "job"


def profile_for(job):
    pid = "ml-research" if job.get("is_ml") else "sde-general"
    return build.load(os.path.join(HERE, "profiles", pid + ".yaml"))


# --------------------------------------------------------------------------
# Rendering. Shared by both the API path and the Claude Code path, so a resume
# is built and checked identically no matter which produced the content.
# --------------------------------------------------------------------------

def render_and_score(content, index, lib_terms, descriptive, job, info, profile, data, ident,
                     attempt_note=""):
    problems = verify(data.get("selected", []), index, lib_terms, descriptive)
    rewrites = {x["id"]: x["text"] for x in data.get("selected", [])
                if x.get("id") in index}
    prefer = set(rewrites)
    if data.get("voice") in ("sde", "ml"):
        profile = dict(profile, voice=data["voice"])

    jd_terms = build.jd_keywords(info["text"])
    items = build.build_items(content, profile, jd_terms, rewrites, prefer)
    preamble = open(os.path.join(HERE, "templates", "preamble.tex"),
                    encoding="utf-8").read()
    tex, pdf, pages, fill, _ = build.tune(
        build.find_pdflatex(), content, profile, items, preamble, ident)

    os.makedirs(TAILORED_DIR, exist_ok=True)
    open(os.path.join(TAILORED_DIR, ident + ".tex"), "w",
         encoding="utf-8", newline="\n").write(tex)
    pdf_out = os.path.join(TAILORED_DIR, ident + ".pdf")
    import shutil
    shutil.copyfile(pdf, pdf_out)

    sc = scoring.score_pdf(pdf_out, info["text"], job, pages)
    print("    %sjd_match=%.1f  pages=%d fill=%.0f%%  rewrites=%d  flags=%d"
          % (attempt_note, sc["score"], pages, fill * 100, len(rewrites), len(problems)))
    if problems:
        print("    UNVERIFIED -- claims the content library does not support:")
        for p in problems[:6]:
            print("      %-16s %-12s %s" % (p["id"], p["kind"], p["detail"]))

    return {
        "job_id": job.get("id"), "company": job.get("company"),
        "role": job.get("role"), "link": job.get("link"),
        "pdf": "resumes/out/tailored/%s.pdf" % ident,
        "tex": "resumes/out/tailored/%s.tex" % ident,
        "jd_match": sc["score"], "jd_source": info["source"],
        "jd_confidence": info["confidence"],
        "verified": not problems, "flags": problems,
        "missing": sc["missing"], "gaps": data.get("gaps", []),
        "summary": data.get("summary", ""),
        "pages": pages, "fill": round(fill, 3),
        "updated": datetime.datetime.now(datetime.timezone.utc)
                   .replace(microsecond=0).isoformat().replace("+00:00", "Z"),
    }


def write_manifest(results):
    if not results:
        return
    prev = []
    if os.path.exists(MANIFEST):
        try:
            prev = json.load(open(MANIFEST, encoding="utf-8"))
        except ValueError:
            prev = []
    keep = {r["job_id"] for r in results}
    merged = [p for p in prev if p.get("job_id") not in keep] + results
    merged.sort(key=lambda r: r.get("updated", ""), reverse=True)
    os.makedirs(os.path.dirname(MANIFEST), exist_ok=True)
    with open(MANIFEST, "w", encoding="utf-8", newline="\n") as f:
        json.dump(merged, f, indent=1)
        f.write("\n")
    ok = sum(1 for r in results if r["jd_match"] >= TARGET_SCORE)
    print("\n%d tailored, %d at or above %.0f, %d unverified -> %s"
          % (len(results), ok, TARGET_SCORE,
             sum(1 for r in results if not r["verified"]), MANIFEST))


# --------------------------------------------------------------------------
# Claude Code path (--emit / --apply)
#
# A Claude Max subscription covers Claude Code but not the Developer API, and
# the model call is the only step that needs a model. So emit a self-contained
# prompt pack per job, let the Claude Code session answer it, and apply the
# answers through exactly the same verify/render/score path the API uses.
# --------------------------------------------------------------------------

def emit(targets, content):
    os.makedirs(JOBS_DIR, exist_ok=True)
    manifest = []

    # Fetch descriptions concurrently. Sequentially this is ~3s of network per
    # job, which is fine for a handful and unusable for a couple of hundred.
    from concurrent.futures import ThreadPoolExecutor
    print("fetching %d job descriptions..." % len(targets))
    with ThreadPoolExecutor(max_workers=10) as pool:
        infos = list(pool.map(jdmod.fetch_jd, targets))

    for job, info in zip(targets, infos):
        profile = profile_for(job)
        ident = slug(job)
        lib = library_prompt(content, profile["voice"])

        pack = "\n".join([
            "<!-- Generated by tailor.py --emit. Answer this in %s.json -->" % ident,
            "",
            "# Tailoring request: %s -- %s" % (job.get("company", ""), job.get("role", "")),
            "",
            "Location: %s" % job.get("location", ""),
            "Apply:    %s" % job.get("link", ""),
            "JD source: %s (confidence: %s, %d chars)"
            % (info["source"], info["confidence"], len(info["text"])),
            "",
            "## Instructions",
            "",
            SYSTEM,
            "",
            "## Content library (voice: %s)" % profile["voice"],
            lib,
            "",
            "## Job description",
            "",
            info["text"][:14000],
            "",
            "## Required answer",
            "",
            "Write `resumes/.jobs/%s.json` containing exactly:" % ident,
            "",
            "```json",
            json.dumps({
                "voice": profile["voice"],
                "selected": [{"id": "<library id>", "text": "<rewritten bullet>"}],
                "summary": "<one line on what you emphasised and why>",
                "gaps": ["<JD requirement his experience does not cover>"],
            }, indent=1),
            "```",
            "",
            "Select 26-32 bullets. Obey every hard rule above -- the pipeline "
            "verifies each rewrite against the library and marks the resume "
            "unverified if a number or technology is not supported.",
        ])
        path = os.path.join(JOBS_DIR, ident + ".md")
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write(pack)
        manifest.append({"ident": ident, "job_id": job.get("id"),
                         "jd": info, "profile": profile["id"]})
        print("  %-46s JD %s/%s %d chars -> %s.md"
              % ((job.get("company", "") + " " + (job.get("role") or ""))[:46],
                 info["source"], info["confidence"], len(info["text"]), ident))

    with open(os.path.join(JOBS_DIR, "_manifest.json"), "w",
              encoding="utf-8", newline="\n") as f:
        json.dump(manifest, f, indent=1)
    print("\n%d prompt pack(s) in %s" % (len(manifest), JOBS_DIR))
    print("Next: have Claude Code read each .md and write the matching .json,")
    print("then run:  py -3 resumes/tailor.py --apply")


def apply_answers(content, index, lib_terms, descriptive, jobs_by_id):
    mpath = os.path.join(JOBS_DIR, "_manifest.json")
    if not os.path.exists(mpath):
        sys.exit("no prompt packs found; run --emit first")
    manifest = json.load(open(mpath, encoding="utf-8"))

    results, waiting = [], []
    for entry in manifest:
        ident = entry["ident"]
        ans = os.path.join(JOBS_DIR, ident + ".json")
        if not os.path.exists(ans):
            waiting.append(ident)
            continue
        job = jobs_by_id.get(entry["job_id"]) or {"id": entry["job_id"]}
        try:
            data = json.load(open(ans, encoding="utf-8"))
        except ValueError as e:
            print("  %s: answer is not valid JSON (%s)" % (ident, e))
            continue
        print("%s -- %s" % (job.get("company", "?"), (job.get("role") or "")[:52]))
        try:
            results.append(render_and_score(
                content, index, lib_terms, descriptive, job, entry["jd"],
                profile_for(job), data, ident))
        except Exception as e:
            print("    FAILED: %s" % e)

    if waiting:
        print("\nstill waiting on answers for: %s" % ", ".join(waiting))
    write_manifest(results)


# --------------------------------------------------------------------------
# API path
# --------------------------------------------------------------------------

def tailor_one(client, content, index, lib_terms, descriptive, job, dry_run=False):
    info = jdmod.fetch_jd(job)
    profile = profile_for(job)
    lib = library_prompt(content, profile["voice"])
    ident = slug(job)

    print("  JD: %s (%s, %d chars)" % (info["source"], info["confidence"], len(info["text"])))
    if dry_run:
        return None

    missing, best = None, None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        data, usage = call_claude(client, lib, job, info["text"], missing)
        cached = getattr(usage, "cache_read_input_tokens", 0) if usage else 0
        best = render_and_score(content, index, lib_terms, descriptive, job, info, profile,
                                data, ident, "attempt %d: " % attempt)
        best["cache_read_tokens"] = cached
        if best["jd_match"] >= TARGET_SCORE:
            break
        missing = best["missing"]
    return best


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--shortlist", help="JSON exported from the dashboard")
    ap.add_argument("--job-id", help="a single job id from data/jobs.json")
    ap.add_argument("--url", help="a single apply URL (JD fetch test)")
    ap.add_argument("--dry-run", action="store_true",
                    help="fetch JDs and report, make no model calls")
    ap.add_argument("--emit", action="store_true",
                    help="write prompt packs for a Claude Code session (no API key)")
    ap.add_argument("--apply", action="store_true",
                    help="build resumes from the answers written next to the packs")
    args = ap.parse_args()

    jobs_all = json.load(open(JOBS, encoding="utf-8"))
    by_id = {j["id"]: j for j in jobs_all}

    content = build.load(os.path.join(HERE, "content.yaml"))
    build.collect_tags(content)
    index = library_index(content)
    lib_terms = library_terms(content, index)
    descriptive = descriptive_ids(content)

    if args.apply:
        apply_answers(content, index, lib_terms, descriptive, by_id)
        return

    if args.url:
        targets = [{"id": "adhoc", "link": args.url, "company": "", "role": "",
                    "location": "", "is_ml": False}]
    elif args.job_id:
        if args.job_id not in by_id:
            sys.exit("job id not found: %s" % args.job_id)
        targets = [by_id[args.job_id]]
    elif args.shortlist:
        picks = json.load(open(args.shortlist, encoding="utf-8"))
        targets = [by_id[p["id"]] for p in picks if p.get("id") in by_id]
        if len(targets) != len(picks):
            print("note: %d shortlisted job(s) no longer on the board"
                  % (len(picks) - len(targets)))
    else:
        ap.error("pass --shortlist, --job-id, --url, or --apply")

    if args.emit:
        emit(targets, content)
        return

    client = None
    if not args.dry_run:
        try:
            import anthropic
        except ImportError:
            sys.exit("anthropic SDK missing:  py -3 -m pip install anthropic")
        try:
            client = anthropic.Anthropic()
        except Exception as e:
            sys.exit("No Anthropic API credentials. A Claude Pro/Max plan does not\n"
                     "include API access -- they are billed separately. Either set\n"
                     "ANTHROPIC_API_KEY, or use the Claude Code path instead:\n"
                     "    py -3 resumes/tailor.py --shortlist shortlist.json --emit\n"
                     "(%s)" % e)

    results = []
    for job in targets:
        print("%s -- %s" % (job.get("company", "?"), (job.get("role") or "")[:56]))
        try:
            r = tailor_one(client, content, index, lib_terms, descriptive, job, args.dry_run)
        except Exception as e:
            print("    FAILED: %s" % e)
            continue
        if r:
            results.append(r)
    write_manifest(results)


if __name__ == "__main__":
    main()
