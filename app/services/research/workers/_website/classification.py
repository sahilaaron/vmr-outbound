"""Deterministic page-type classification.

Combines URL path patterns, page title, and heading text. Returns the type,
a confidence score with a label, and the signals that fired - uncertain
classification is reported as such rather than hidden.
"""
from __future__ import annotations

import re
from urllib.parse import urlsplit

from .models import Classification

PAGE_TYPES = [
    "homepage", "about", "product", "service", "solution", "industry",
    "application", "customer", "case_study", "leadership", "location",
    "contact", "news", "press_release", "blog", "careers", "investor",
    "legal", "other",
]

# type -> (url regex, title/heading regex)
_RULES: dict[str, tuple[str, str]] = {
    "about": (
        r"about|who[-_ ]we[-_ ]are|our[-_ ]story|company[-_ ]profile|/company/?$|overview|mission|history",
        r"\babout\b|who we are|our story|our company|our mission|company profile|our history",
    ),
    "leadership": (
        r"leadership|management[-_ ]team|board[-_ ]of|executives?|our[-_ ]team|/team/?$|founders?",
        r"leadership|management team|board of directors|executive|our team|meet the team|founders?",
    ),
    "product": (r"/products?\b|/catalog", r"\bproducts?\b|product range|catalog"),
    "service": (r"/services?\b", r"\bservices?\b|what we do"),
    "solution": (r"/solutions?\b", r"\bsolutions?\b"),
    "industry": (r"/industri|/sectors?\b|/markets?\b|/verticals?", r"industries|sectors we serve|markets\b|verticals"),
    "application": (r"/applications?\b|/use[-_ ]cases?", r"applications\b|use cases"),
    "customer": (r"/customers?\b|/clients?\b|/references", r"our (customers|clients)|trusted by|references"),
    "case_study": (r"case[-_ ]stud|success[-_ ]stor|testimonial", r"case stud|success stor|testimonial"),
    "location": (r"/locations?\b|/offices?\b|/facilities|/global\b|/branches|/where", r"our (locations|offices|facilities)|where (we are|to find)"),
    "contact": (r"/contact|/get[-_ ]in[-_ ]touch|/reach[-_ ]us|/enquir|/inquir", r"contact (us)?|get in touch|reach us|enquir|inquir"),
    "press_release": (r"press[-_ ]?release|/press\b|newsroom|/media\b", r"press release|newsroom|media cent"),
    "news": (r"/news\b|/announcements?|/updates\b", r"\bnews\b|announcements|latest updates"),
    "blog": (r"/blog\b|/insights?\b|/articles?\b|/posts?\b", r"\bblog\b|insights|articles"),
    "careers": (r"/careers?\b|/jobs?\b|/join[-_ ]us|/vacanc|/work[-_ ]with", r"careers?|join (us|our team)|we'?re hiring|open positions|vacanc"),
    "investor": (r"/investors?\b|/shareholders?|/annual[-_ ]report|/financial", r"investor relations|shareholders|annual report|financial results"),
    "legal": (r"/privacy|/terms|/legal\b|/imprint|/impressum|/cookie|/disclaimer|/gdpr", r"privacy policy|terms (of|and)|legal notice|imprint|impressum|cookie policy|disclaimer"),
}
_COMPILED = {
    t: (re.compile(u, re.IGNORECASE), re.compile(x, re.IGNORECASE))
    for t, (u, x) in _RULES.items()
}


def _confidence_label(score: float) -> str:
    if score >= 0.8:
        return "high"
    if score >= 0.55:
        return "medium"
    return "low"


def classify_page(url: str, title: str | None = None,
                  headings: list[str] | None = None,
                  anchor_text: str = "") -> Classification:
    path = urlsplit(url).path or "/"
    title = title or ""
    headings = headings or []
    heading_text = " | ".join(headings[:6])

    if path in ("", "/"):
        return Classification("homepage", 0.99, "high", ["root path"])

    segments = [s for s in path.split("/") if s]
    first_segment = "/" + segments[0] if segments else "/"

    scores: dict[str, tuple[float, list[str]]] = {}
    for ptype, (url_rx, text_rx) in _COMPILED.items():
        score = 0.0
        signals: list[str] = []
        if url_rx.search(path):
            score += 0.6
            signals.append(f"url matches /{url_rx.pattern}/")
            # The leading path segment is the strongest section indicator
            # (/news/why-our-leadership-... is news, not leadership).
            if url_rx.search(first_segment):
                score += 0.2
                signals.append(f"first path segment '{first_segment}' matches")
        if title and text_rx.search(title):
            score += 0.25
            signals.append(f"title matches ({title[:60]!r})")
        if heading_text and text_rx.search(heading_text):
            score += 0.15
            signals.append("heading matches")
        if anchor_text and text_rx.search(anchor_text):
            score += 0.1
            signals.append(f"anchor text matches ({anchor_text[:40]!r})")
        if score > 0:
            scores[ptype] = (min(score, 0.98), signals)

    if not scores:
        return Classification("other", 0.3, "low", ["no rule matched"])

    best_type, (best_score, best_signals) = max(
        scores.items(), key=lambda kv: kv[1][0])
    # Ambiguity: if a second type scores nearly the same, lower confidence.
    runners = sorted((s for t, (s, _) in scores.items() if t != best_type), reverse=True)
    if runners and best_score - runners[0] < 0.15:
        best_score = max(0.35, best_score - 0.2)
        best_signals = best_signals + ["ambiguous: close competing type"]
    return Classification(best_type, round(best_score, 2),
                          _confidence_label(best_score), best_signals)
