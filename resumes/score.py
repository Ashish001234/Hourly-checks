#!/usr/bin/env python3
"""
Score a resume against one job description.

This is NOT an ATS score. Workday, Greenhouse and the rest do not expose one,
and anything claiming to reproduce theirs is guessing. This is our own proxy,
called `jd_match` everywhere so it is never mistaken for a vendor number. What
it actually measures: does the resume use the vocabulary this JD uses, name the
role, carry the sections a parser expects, and survive text extraction.

Useful as a relative signal -- "this draft covers more of the JD than that one"
-- not as a prediction of what any particular employer's software will do.
"""
import os
import re
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from build import TECH_VOCAB, _mentions   # noqa: E402  one vocabulary, one matcher

# Weights sum to 100.
W_KEYWORDS = 55
W_TITLE = 15
W_SECTIONS = 15
W_PARSE = 10
W_LENGTH = 5

REQUIRED_SECTIONS = ["education", "experience", "skills"]

PDFTOTEXT_CANDIDATES = [
    r"C:\Users\ashis\AppData\Local\Programs\MiKTeX\miktex\bin\x64\pdftotext.exe",
    "pdftotext",
]

STOP_TITLE = {"new", "grad", "graduate", "entry", "level", "early", "career", "junior",
              "jr", "senior", "the", "and", "for", "with", "a", "an", "of", "i", "ii",
              "2026", "2027", "campus", "full", "time", "intern"}


def find_pdftotext():
    for c in PDFTOTEXT_CANDIDATES:
        if os.path.isabs(c):
            if os.path.exists(c):
                return c
        elif shutil.which(c):
            return c
    return None


def pdf_text(pdf_path):
    exe = find_pdftotext()
    if not exe or not os.path.exists(pdf_path):
        return ""
    try:
        p = subprocess.run([exe, "-layout", pdf_path, "-"],
                           capture_output=True, text=True, errors="replace", timeout=60)
        return p.stdout or ""
    except (OSError, subprocess.SubprocessError):
        return ""


def jd_terms(jd_text):
    low = " " + re.sub(r"\s+", " ", (jd_text or "").lower()) + " "
    return {t for t in TECH_VOCAB if _mentions(t, low)}


def title_terms(role):
    """Meaningful words from the role title, lightly stemmed.

    Stemming matters here: a JD titled "Core Development Program" should credit
    a resume that says "Developer". Exact matching scored that 0, which reads as
    "wrong role" when it means "different suffix".
    """
    words = re.findall(r"[a-z][a-z0-9+#]{1,}", (role or "").lower())
    out = set()
    for w in words:
        if w in STOP_TITLE:
            continue
        out.add(w[:6] if len(w) > 6 else w)
    return out


def score(resume_text, jd_text, job, pages=None):
    """-> {score, breakdown, missing, matched}"""
    low_resume = " " + re.sub(r"\s+", " ", (resume_text or "").lower()) + " "

    # 1. Keyword coverage.
    terms = jd_terms(jd_text)
    matched = {t for t in terms if _mentions(t, low_resume)}
    missing = sorted(terms - matched)
    if terms:
        kw = W_KEYWORDS * len(matched) / len(terms)
    else:
        # A JD that names no technologies (plenty of quant and consulting posts
        # read "quick learner, detail-oriented") tells us nothing about keyword
        # fit. Award the neutral midpoint rather than zero, which would make the
        # score say "bad resume" when it means "uninformative JD".
        kw = W_KEYWORDS * 0.7

    # 2. Title alignment.
    tt = title_terms(job.get("role", ""))
    thit = {w for w in tt if _mentions(w, low_resume)}
    title = W_TITLE * (len(thit) / len(tt)) if tt else W_TITLE * 0.7

    # 3. Sections a resume parser expects to find.
    present = [s for s in REQUIRED_SECTIONS if s in low_resume]
    sections = W_SECTIONS * len(present) / len(REQUIRED_SECTIONS)

    # 4. Parseability: if text extraction lost the contact block, an ATS will
    #    lose it too, and a resume nobody can contact you from scores zero here.
    parse = 0.0
    if resume_text.strip():
        parse += W_PARSE * 0.4
        if re.search(r"[\w.+-]+@[\w-]+\.[\w.]+", resume_text):
            parse += W_PARSE * 0.3
        if re.search(r"\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}", resume_text):
            parse += W_PARSE * 0.3

    # 5. Length sanity.
    words = len(re.findall(r"\w+", resume_text))
    length = W_LENGTH if (600 <= words <= 1400 and (pages in (None, 2))) else W_LENGTH * 0.5

    total = kw + title + sections + parse + length
    return {
        "score": round(total, 1),
        "breakdown": {
            "keywords": round(kw, 1), "title": round(title, 1),
            "sections": round(sections, 1), "parse": round(parse, 1),
            "length": round(length, 1),
        },
        "jd_term_count": len(terms),
        "matched": sorted(matched),
        "missing": missing,
        "words": words,
    }


def score_pdf(pdf_path, jd_text, job, pages=None):
    return score(pdf_text(pdf_path), jd_text, job, pages)


if __name__ == "__main__":
    import argparse
    import json
    ap = argparse.ArgumentParser()
    ap.add_argument("pdf")
    ap.add_argument("--jd", required=True)
    ap.add_argument("--role", default="")
    args = ap.parse_args()
    jd = open(args.jd, encoding="utf-8").read()
    r = score_pdf(args.pdf, jd, {"role": args.role})
    print(json.dumps(r, indent=1)[:1800])
