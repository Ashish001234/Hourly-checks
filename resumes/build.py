#!/usr/bin/env python3
"""
Build a two-page resume from content.yaml for a given profile.

The hard requirement is a resume that fills exactly two pages with no slack at
the bottom of page two. That cannot be eyeballed from a template, so this
compiles the document, measures where the content actually ends, and iterates:
shed the weakest bullets when it spills onto page three, add the next-best ones
back when page two is short, and only then nudge spacing within a bounded range.

Usage
-----
  py -3 build.py --profile sde-general
  py -3 build.py --all
  py -3 build.py --profile ml-applied --jd jd.txt --out-id stripe-swe
      (--jd re-scores bullets against a pasted job description)
"""
import argparse
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import datetime

try:
    import yaml
except ImportError:
    sys.exit("pyyaml is required:  py -3 -m pip install pyyaml")

HERE = os.path.dirname(os.path.abspath(__file__))
SITE = os.path.dirname(HERE)
OUT = os.path.join(HERE, "out")
BUILD = os.path.join(HERE, ".build")          # scratch; not committed
MANIFEST = os.path.join(SITE, "data", "resumes.json")

# Local MiKTeX is not on PATH; fall back to whatever is, so CI works too.
PDFLATEX_CANDIDATES = [
    r"C:\Users\ashis\AppData\Local\Programs\MiKTeX\miktex\bin\x64\pdflatex.exe",
    "pdflatex",
]

# Layout knobs the tuner is allowed to move, in the order it should try them.
# Each is (name, default, minimum, maximum). Ranges are deliberately tight:
# past these the resume stops looking like the hand-written original.
KNOBS = [
    ("ITEMSEP", 1.2, 0.4, 3.2),
    ("ENTRYSPACE", 4.0, 2.4, 7.0),
    ("PROJSPACE", 5.0, 3.0, 8.0),
    ("SECSPACE", 8.0, 5.0, 11.0),
    ("BODYSIZE", 9.6, 9.1, 10.1),
]
BODY_LEAD_RATIO = 12.6 / 9.6          # keep leading proportional to body size
MARGIN = 0.55

TARGET_PAGES = 2
MIN_FILL = 0.93                        # page two must be at least this full


# ---------------------------------------------------------------- helpers

def find_pdflatex():
    for cand in PDFLATEX_CANDIDATES:
        if os.path.isabs(cand):
            if os.path.exists(cand):
                return cand
        elif shutil.which(cand):
            return cand
    sys.exit("pdflatex not found. Install MiKTeX/TeX Live or add it to PATH.")


def load(path):
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def pick(entry, voice):
    """Audience-specific phrasing, falling back to the shared one."""
    return entry.get(voice) or entry.get("text")


def score(item, emphasis, jd, text=""):
    """Higher is more likely to survive the trim."""
    s = float(item.get("weight", 5))
    for t in (item.get("tags") or []):
        s += emphasis.get(t, 0)
        if jd and t in jd:
            s += 4
    # Reward bullets whose own text speaks the JD's vocabulary. Scoring tags
    # alone was near useless: internal tags are implementation jargon ("lora",
    # "gbdt", "websockets") while a JD says "Python", "distributed systems",
    # "data structures". Capped so one keyword-dense bullet cannot crowd out
    # everything else on the page.
    if jd and text:
        low = text.lower()
        s += min(sum(1 for t in jd if t in low) * 1.5, 7.0)
    return s


MONTHS = {m: i for i, m in enumerate(
    "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split(), start=1)}


def end_date_key(dates):
    """Sortable key for the *end* of a range like 'May 2025 -- Aug 2025'.

    Derived from the text rather than trusting content.yaml to stay in order,
    so a reverse-chronological resume cannot silently drift when someone adds
    an entry in the wrong place. 'Present' sorts newest.
    """
    tail = (dates or "").split("--")[-1].strip()
    if tail.lower().startswith("present"):
        return (9999, 12)
    m = re.search(r"([A-Za-z]{3})\w*\s+(\d{4})", tail)
    if m:
        return (int(m.group(2)), MONTHS.get(m.group(1).title(), 0))
    m = re.search(r"(\d{4})", tail)
    return (int(m.group(1)), 0) if m else (0, 0)


# Vocabulary a job description actually uses. Curated rather than "every token
# in the JD" -- raw tokens match common English and turn scoring into noise.
# Multi-word entries are matched as substrings.
TECH_VOCAB = [
    # languages / runtimes
    "python", "c++", "java", "javascript", "typescript", "golang", "rust", "scala",
    "kotlin", "swift", "ruby", "matlab", "bash", "sql", "cuda",
    # web / backend
    "rest", "api", "microservice", "backend", "back-end", "frontend", "front-end",
    "full stack", "full-stack", "react", "node.js", "django", "flask", "spring",
    "graphql", "grpc", "websocket",
    # data / storage
    "postgres", "mysql", "database", "nosql", "mongodb", "redis", "kafka", "spark",
    "hadoop", "etl", "data pipeline", "data warehouse", "indexing", "transactions",
    # cloud / infra
    "aws", "azure", "gcp", "cloud", "docker", "kubernetes", "terraform", "ci/cd",
    "linux", "unix", "distributed", "scalab", "high availability", "reliability",
    "observability", "monitoring", "latency", "throughput", "concurrency",
    "concurrent", "multithread", "async", "infrastructure", "devops",
    "site reliability", "load balanc", "caching",
    # cs fundamentals
    "algorithms", "data structures", "operating systems", "computer science",
    "system design", "networking", "compilers", "optimization",
    # ml / ai
    "machine learning", "deep learning", "neural", "pytorch", "tensorflow",
    "huggingface", "hugging face", "transformer", "llm", "large language model",
    "nlp", "natural language", "computer vision", "reinforcement learning",
    "fine-tun", "finetun", "lora", "rag", "retrieval augmented", "embedding",
    "inference", "benchmark", "statistics", "statistical", "probability",
    "regression", "classification", "clustering", "feature engineering", "scikit",
    "numpy", "pandas", "xgboost", "gpu", "quantization", "recommendation",
    "ranking", "anomaly detection", "time series", "evaluation",
    # quant
    "quantitative", "trading", "market", "risk", "portfolio", "derivatives",
    "signal", "backtest", "low latency", "high frequency",
    # practice
    "testing", "unit test", "code review", "agile", "git", "version control",
    "debugging", "profiling", "security", "authentication",
]


# Technology names that are also ordinary English or prefixes of other words:
# "react" in *reactions*, "spring" in *spring/summer 2027*, "scala" in
# *scalability*. These need a closing boundary as well as an opening one.
AMBIGUOUS = {"react", "spring", "scala", "rust", "swift", "ruby", "signal",
             "alpha", "node", "spark", "go", "market", "risk", "code", "data"}


def _mentions(term, low):
    """Does the JD really use this term?

    Word boundaries matter more than they look: a bare substring test made
    "ci" match *de-ci-sion*, "rag" match *prog-ram*, and "cv" match *cvs*,
    which quietly poisoned the scoring with terms the JD never used.
    """
    if re.search(r"[^a-z0-9 \-]", term):        # c++, ci/cd, node.js
        return term in low
    if len(term) <= 3 or term in AMBIGUOUS:     # need both edges
        return re.search(r"\b" + re.escape(term) + r"\b", low) is not None
    # Leading boundary only, so stems like "scalab" still catch "scalability".
    return re.search(r"\b" + re.escape(term), low) is not None


def jd_keywords(text):
    """Technical terms the JD actually mentions, plus any internal tags present."""
    low = " " + re.sub(r"\s+", " ", (text or "").lower()) + " "
    found = {t for t in TECH_VOCAB if _mentions(t, low)}
    for t in ALL_TAGS:
        if _mentions(t.replace("-", " "), low) or _mentions(t, low):
            found.add(t)
    return found


ALL_TAGS = set()


def collect_tags(content):
    for section in ("education", "experience", "research"):
        for ent in content.get(section, []):
            for b in ent.get("bullets", []):
                ALL_TAGS.update(b.get("tags") or [])
    for p in content.get("projects", []):
        ALL_TAGS.update(p.get("tags") or [])
        for b in p.get("bullets", []):
            ALL_TAGS.update(b.get("tags") or [])
    for a in content.get("achievements", []):
        ALL_TAGS.update(a.get("tags") or [])


def paper_text(content, key):
    p = content["paper"]
    return (p[key]
            .replace("@@VENUE@@", p["venue"])
            .replace("@@TITLE@@", p["title"])
            .replace("@@COLLAB@@", p["collaborators"]))


def resolve_paper(text, content):
    if not text:
        return text
    if "@@PAPER_BULLET@@" in text:
        return paper_text(content, "bullet")
    if "@@PAPER_ACHIEVEMENT@@" in text:
        return paper_text(content, "achievement")
    return text


# ---------------------------------------------------------------- assembly

def build_items(content, profile, jd_terms, rewrites=None, prefer=None):
    """Flatten everything into scored, renderable units, honouring the profile.

    `rewrites` maps bullet id -> replacement text, and `prefer` is a set of ids
    to keep. Together they let tailor.py hand back JD-specific phrasings while
    this module still owns layout, so an LLM cannot emit broken LaTeX and the
    two-page guarantee is unaffected.
    """
    rewrites = rewrites or {}
    prefer = set(prefer or ())
    voice = profile["voice"]
    emphasis = profile.get("emphasis", {})
    exclude = set(profile.get("exclude", []))
    out = {}

    for section in ("education", "experience", "research"):
        blocks = []
        for ent in content.get(section, []):
            if ent["id"] in exclude:
                continue
            # An experience entry without a title for this voice does not belong
            # on this resume at all (e.g. CDAC has no SDE framing).
            titles = ent.get("titles")
            if titles is not None:
                role = titles.get(voice)
                if not role:
                    continue
            else:
                role = ent.get("role") or ent.get("degree")
            bullets = []
            for bi, b in enumerate(ent.get("bullets", [])):
                txt = resolve_paper(pick(b, voice), content)
                if not txt:
                    continue
                txt = rewrites.get(b["id"], txt)
                sc = score(b, emphasis, jd_terms, txt)
                if b["id"] in prefer:
                    sc += 100          # keep what the tailoring step chose
                bullets.append({"id": b["id"], "text": txt, "idx": bi, "score": sc})
            if not bullets:
                continue
            bullets.sort(key=lambda x: -x["score"])
            blocks.append({
                "id": ent["id"], "kind": "entry", "role": role,
                "org": ent["org"], "location": ent["location"],
                "dates": ent["dates"], "bullets": bullets,
                "score": max(b["score"] for b in bullets),
            })
        # idx encodes reverse-chronological display order; score decides only
        # which blocks survive a trim.
        blocks.sort(key=lambda b: end_date_key(b["dates"]), reverse=True)
        for i, b in enumerate(blocks):
            b["idx"] = i
        blocks.sort(key=lambda b: -b["score"])
        out[section] = blocks

    projects = []
    for p in content.get("projects", []):
        if p["id"] in exclude:
            continue
        bullets = []
        for bi, b in enumerate(p.get("bullets", [])):
            txt = resolve_paper(pick(b, voice), content)
            if not txt:
                continue
            txt = rewrites.get(b["id"], txt)
            sc = score(b, emphasis, jd_terms, txt)
            if b["id"] in prefer:
                sc += 100
            bullets.append({"id": b["id"], "text": txt, "idx": bi, "score": sc})
        if not bullets:
            continue
        bullets.sort(key=lambda x: -x["score"])
        projects.append({
            "id": p["id"], "kind": "proj", "name": p["name"], "stack": p["stack"],
            "idx": len(projects),
            "bullets": bullets,
            "score": score(p, emphasis, jd_terms, p["name"] + " " + p["stack"]) + max(b["score"] for b in bullets),
        })
    projects.sort(key=lambda b: -b["score"])
    out["projects"] = projects

    ach = []
    for a in content.get("achievements", []):
        txt = resolve_paper(pick(a, voice), content)
        if not txt:
            continue
        txt = rewrites.get(a["id"], txt)
        sc = score(a, emphasis, jd_terms, txt)
        if a["id"] in prefer:
            sc += 100
        ach.append({"id": a["id"], "text": txt, "score": sc})
    ach.sort(key=lambda x: -x["score"])
    out["achievements"] = ach
    out["skills"] = content["skills"][voice]
    return out


SECTION_TITLES = {
    "education": "Education",
    "experience": "Industry Experience",
    "research": "Research Experience",
    "projects": "Projects",
    "skills": "Technical Skills",
    "achievements": "Achievements \\& Publications",
}


def render(content, profile, items, budget):
    """budget maps section -> how many bullets/blocks to keep."""
    p = content["profile"]
    L = []
    L.append("\\begin{document}")
    L.append("\\begin{center}")
    L.append("  {\\fontsize{23pt}{27pt}\\selectfont\\bfseries\\color{navy} "
             + p["name"] + "}")
    L.append("  \\vspace{5pt}\\\\")
    L.append("  {\\fontsize{9pt}{11pt}\\selectfont\\textcolor{lightgray}{%")
    L.append("    \\href{mailto:%s}{%s}%%" % (p["email"], p["email"]))
    L.append("    \\enspace\\textcolor{rulecol}{|}\\enspace")
    L.append("    " + p["phone"] + "%")
    L.append("    \\enspace\\textcolor{rulecol}{|}\\enspace")
    L.append("    \\href{https://%s}{%s}%%" % (p["linkedin"], p["linkedin"]))
    L.append("    \\enspace\\textcolor{rulecol}{|}\\enspace")
    L.append("    \\href{https://%s}{%s}%%" % (p["github"], p["github"]))
    L.append("  }}")
    L.append("\\end{center}")
    L.append("\\vspace{3pt}")

    for section in profile["sections"]:
        L.append("")
        L.append("\\rsection{%s}" % SECTION_TITLES[section])
        if section == "skills":
            for row in items["skills"]:
                L.append("\\sk{%s}{%s}" % (row["cat"], row["val"]))
            continue
        if section == "achievements":
            keep = items["achievements"][: budget.get("achievements", 99)]
            L.append("\\begin{itemize}")
            for a in keep:
                L.append("  \\item " + a["text"])
            L.append("\\end{itemize}")
            continue

        # Score decides *what* survives the trim; authored order decides what
        # the reader sees. A resume must stay reverse-chronological, so restore
        # the content.yaml order after selecting.
        chosen = items[section][: budget.get(section + "_blocks", 99)]
        blocks = sorted(chosen, key=lambda b: b["idx"])
        per = budget.get(section + "_bullets", 99)
        for blk in blocks:
            if blk["kind"] == "entry":
                L.append("")
                L.append("\\entry{%s}{%s}{%s}{%s}" %
                         (blk["role"], blk["org"], blk["location"], blk["dates"]))
            else:
                L.append("")
                L.append("\\proj{%s}{%s}" % (blk["name"], blk["stack"]))
            L.append("\\begin{itemize}")
            for b in sorted(blk["bullets"][:per], key=lambda x: x["idx"]):
                L.append("  \\item " + b["text"])
            L.append("\\end{itemize}")

    # Marker for the fill measurement. Must be the last thing on the page.
    L.append("")
    L.append("\\zsaveposy{endofcontent}")
    L.append("\\end{document}")
    return "\n".join(L)


def apply_knobs(preamble, knobs, pdf_title):
    out = preamble
    for name, val in knobs.items():
        out = out.replace("@@%s@@" % name, "%.2f" % val)
    out = out.replace("@@BODYLEAD@@", "%.2f" % (knobs["BODYSIZE"] * BODY_LEAD_RATIO))
    out = out.replace("@@MARGIN@@", "%.2f" % MARGIN)
    out = out.replace("@@PDFTITLE@@", pdf_title)
    return out


# ---------------------------------------------------------------- compile

PAGES_RE = re.compile(r"Output written on .*?\((\d+) pages?", re.S)
ZPOS_RE = re.compile(r"\\zref@newlabel\{endofcontent\}\{[^}]*\\posy\{(-?\d+)\}")


def compile_tex(pdflatex, tex, jobname):
    os.makedirs(BUILD, exist_ok=True)
    # Clear stale artifacts first. pdflatex reads .aux back on the next run, and
    # a run killed mid-write leaves a NUL-filled .aux that then fails every
    # subsequent compile with "Text line contains an invalid character".
    for ext in (".aux", ".log", ".out", ".pdf"):
        stale = os.path.join(BUILD, jobname + ext)
        if os.path.exists(stale):
            os.remove(stale)
    texpath = os.path.join(BUILD, jobname + ".tex")
    with open(texpath, "w", encoding="utf-8", newline="\n") as f:
        f.write(tex)
    # Twice: zref writes positions to .aux on the first pass.
    for _ in range(2):
        proc = subprocess.run(
            [pdflatex, "-interaction=nonstopmode", "-halt-on-error",
             "-output-directory", BUILD, texpath],
            capture_output=True, text=True, errors="replace")
    log = ""
    logpath = os.path.join(BUILD, jobname + ".log")
    if os.path.exists(logpath):
        log = open(logpath, encoding="utf-8", errors="replace").read()
    if proc.returncode != 0:
        errs = [l for l in log.splitlines() if l.startswith("!")][:6]
        raise RuntimeError("pdflatex failed:\n  " + "\n  ".join(errs or ["see " + logpath]))

    m = PAGES_RE.search(log)
    pages = int(m.group(1)) if m else 0

    auxpath = os.path.join(BUILD, jobname + ".aux")
    aux = open(auxpath, encoding="utf-8", errors="replace").read() if os.path.exists(auxpath) else ""
    zm = ZPOS_RE.search(aux)

    # zref y is measured from the page bottom in sp (65536 sp = 1pt).
    fill = 0.0
    if zm:
        end_y_pt = int(zm.group(1)) / 65536.0
        # Text block height: 11in page - top and bottom margins (0.5in each).
        text_h_pt = (11.0 - 1.0) * 72.27
        bottom_margin_pt = 0.5 * 72.27
        used = text_h_pt - (end_y_pt - bottom_margin_pt)
        fill = max(0.0, min(1.0, used / text_h_pt))
    return pages, fill, os.path.join(BUILD, jobname + ".pdf")


# ---------------------------------------------------------------- tuning

def total_bullets(items, budget):
    n = 0
    for s in ("education", "experience", "research", "projects"):
        for blk in items.get(s, [])[: budget.get(s + "_blocks", 99)]:
            n += len(blk["bullets"][: budget.get(s + "_bullets", 99)])
    n += min(len(items.get("achievements", [])), budget.get("achievements", 99))
    return n


def tune(pdflatex, content, profile, items, preamble, jobname):
    """Find a layout that lands on exactly two full pages.

    Two phases, deliberately. A single loop that both adds content and adjusts
    spacing oscillates: content is added a whole block at a time (~4 bullets),
    which jumps straight from "page two 90% full" to "spills onto page three",
    so it removes the block, is short again, and re-adds it forever.

    Phase 1 settles the content -- the most that fits on two pages. Phase 2
    freezes it and opens up spacing to absorb the remainder. Neither phase can
    undo the other, so the search always terminates.
    """
    knobs = {n: d for n, d, _, _ in KNOBS}
    budget = {
        "education_blocks": 2, "education_bullets": 2,
        "experience_blocks": 4, "experience_bullets": 4,
        "research_blocks": 2, "research_bullets": 4,
        "projects_blocks": 4, "projects_bullets": 2,
        "achievements": 4,
    }
    trail = []

    def attempt():
        tex = apply_knobs(preamble, knobs, profile["pdf_title"]) + "\n" + \
            render(content, profile, items, budget)
        pages, fill, pdf = compile_tex(pdflatex, tex, jobname)
        trail.append((len(trail), pages, round(fill, 3), total_bullets(items, budget)))
        return tex, pages, fill, pdf

    tex, pages, fill, pdf = attempt()

    # --- Phase 1: content -------------------------------------------------
    while pages > TARGET_PAGES:
        if not shrink_content(budget):
            break
        tex, pages, fill, pdf = attempt()

    while pages == TARGET_PAGES and fill < MIN_FILL:
        saved = dict(budget)
        if not grow_content(budget, items):
            break
        t2, p2, f2, d2 = attempt()
        if p2 > TARGET_PAGES:
            budget.clear()
            budget.update(saved)          # that block overshot; content is settled
            break
        tex, pages, fill, pdf = t2, p2, f2, d2

    # --- Phase 2: spacing, content frozen ---------------------------------
    while not (pages == TARGET_PAGES and fill >= MIN_FILL):
        saved = dict(knobs)
        moved = shrink_knobs(knobs) if pages > TARGET_PAGES else grow_knobs(knobs)
        if not moved:
            break
        t2, p2, f2, d2 = attempt()
        if p2 > TARGET_PAGES and pages == TARGET_PAGES:
            knobs.clear()
            knobs.update(saved)           # last nudge spilled; keep the best fit
            break
        tex, pages, fill, pdf = t2, p2, f2, d2

    # The final state may be one attempt behind if we reverted; recompile so the
    # returned tex/pdf match the knobs and budget we actually settled on.
    tex, pages, fill, pdf = attempt()
    if pages != TARGET_PAGES:
        raise RuntimeError("could not reach %d pages; trail=%s" % (TARGET_PAGES, trail))
    return tex, pdf, pages, fill, trail


def shrink_content(budget):
    """Drop the weakest content, lowest-value first."""
    for key, floor in [("projects_bullets", 1), ("achievements", 3),
                       ("projects_blocks", 2), ("experience_bullets", 2),
                       ("research_bullets", 2), ("education_bullets", 1),
                       ("experience_blocks", 2)]:
        if budget[key] > floor:
            budget[key] -= 1
            return True
    return False


def grow_content(budget, items):
    """Add the next-best content. Single bullets before whole blocks, so the
    search approaches a full page gradually instead of leaping past it."""
    caps = {
        "experience_bullets": max((len(b["bullets"]) for b in items.get("experience", [])), default=0),
        "research_bullets": max((len(b["bullets"]) for b in items.get("research", [])), default=0),
        "projects_bullets": max((len(b["bullets"]) for b in items.get("projects", [])), default=0),
        "education_bullets": max((len(b["bullets"]) for b in items.get("education", [])), default=0),
        "achievements": len(items.get("achievements", [])),
        "projects_blocks": len(items.get("projects", [])),
        "experience_blocks": len(items.get("experience", [])),
        "research_blocks": len(items.get("research", [])),
    }
    for key in ["experience_bullets", "research_bullets", "achievements",
                "projects_bullets", "education_bullets",
                "projects_blocks", "experience_blocks", "research_blocks"]:
        if budget[key] < caps.get(key, 0):
            budget[key] += 1
            return True
    return False


def shrink_knobs(knobs):
    for name, default, lo, hi in KNOBS:
        if knobs[name] > lo:
            knobs[name] = max(lo, knobs[name] - (0.1 if name == "BODYSIZE" else 0.2))
            return True
    return False


def grow_knobs(knobs):
    """Spacing first -- the least visible lever -- then type size as a last
    resort for profiles that genuinely run out of bullets to add."""
    for name, default, lo, hi in KNOBS:
        if name == "BODYSIZE":
            continue
        if knobs[name] < hi:
            knobs[name] = min(hi, knobs[name] + 0.2)
            return True
    for name, default, lo, hi in KNOBS:
        if name == "BODYSIZE" and knobs[name] < hi:
            knobs[name] = min(hi, knobs[name] + 0.1)
            return True
    return False


# ---------------------------------------------------------------- main

def build_profile(pdflatex, content, profile, jd_text=None, out_id=None):
    jd_terms = jd_keywords(jd_text) if jd_text else set()
    items = build_items(content, profile, jd_terms)
    preamble = open(os.path.join(HERE, "templates", "preamble.tex"),
                    encoding="utf-8").read()
    ident = out_id or profile["id"]
    tex, pdf, pages, fill, trail = tune(
        pdflatex, content, profile, items, preamble, ident)

    os.makedirs(OUT, exist_ok=True)
    tex_out = os.path.join(OUT, ident + ".tex")
    pdf_out = os.path.join(OUT, ident + ".pdf")
    with open(tex_out, "w", encoding="utf-8", newline="\n") as f:
        f.write(tex)
    shutil.copyfile(pdf, pdf_out)

    ok = pages == TARGET_PAGES and fill >= MIN_FILL
    flag = "OK " if ok else "WARN"
    print(f"  {flag} {ident:<22} pages={pages}  page2 fill={fill*100:.1f}%  "
          f"({len(trail)} passes)")
    if not ok:
        # Show how the search moved so a stall is diagnosable rather than mute.
        print("       trail (pass, pages, fill, bullets):")
        for row in trail[-8:]:
            print("        ", row)
    if jd_terms:
        print(f"       matched JD tags: {', '.join(sorted(jd_terms)) or 'none'}")
    return {
        "id": ident,
        "label": profile["label"],
        "pdf": "resumes/out/%s.pdf" % ident,
        "tex": "resumes/out/%s.tex" % ident,
        "match": profile.get("match", []),
        "pages": pages,
        "fill": round(fill, 3),
        "updated": datetime.datetime.now(datetime.timezone.utc)
                   .replace(microsecond=0).isoformat().replace("+00:00", "Z"),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--profile")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--jd", help="path to a job description; re-scores bullets")
    ap.add_argument("--out-id", help="output filename stem for a bespoke build")
    args = ap.parse_args()

    pdflatex = find_pdflatex()
    content = load(os.path.join(HERE, "content.yaml"))
    collect_tags(content)

    if args.all:
        paths = sorted(glob.glob(os.path.join(HERE, "profiles", "*.yaml")))
    elif args.profile:
        paths = [os.path.join(HERE, "profiles", args.profile + ".yaml")]
    else:
        ap.error("pass --profile NAME or --all")

    jd_text = open(args.jd, encoding="utf-8").read() if args.jd else None

    print(f"pdflatex: {pdflatex}")
    entries, failures = [], 0
    for path in paths:
        profile = load(path)
        try:
            entries.append(build_profile(pdflatex, content, profile,
                                         jd_text, args.out_id))
        except RuntimeError as e:
            failures += 1
            print(f"  FAIL {profile['id']}: {e}")

    if args.all and entries:
        os.makedirs(os.path.dirname(MANIFEST), exist_ok=True)
        with open(MANIFEST, "w", encoding="utf-8", newline="\n") as f:
            json.dump(entries, f, indent=1)
            f.write("\n")
        print(f"wrote {MANIFEST} ({len(entries)} variants)")

    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
