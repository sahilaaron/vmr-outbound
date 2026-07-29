"""Deterministic company-fact extraction with full provenance.

Every Fact records field, value, fact_type, source_url, a short supporting
excerpt, retrieval timestamp, extraction method, and confidence. No LLM,
no semantic inference: only explicit statements, structured data (JSON-LD /
Open Graph), and clearly-labeled heuristic classification signals.
"""
from __future__ import annotations

import re
from typing import Any, Iterable, Optional

from .models import (
    FACT_TYPE_DETERMINISTIC, FACT_TYPE_EXPLICIT, FACT_TYPE_HEURISTIC,
    Fact, PageExtract,
)

_EXCERPT_LEN = 240

LEADER_TITLES = (
    r"(?:Co[- ]?Founder|Founder|CEO|CTO|CFO|COO|CIO|CMO|CHRO|CSO|CPO|"
    r"Chief [A-Z][a-zA-Z ]{2,30}Officer|President|Vice President|VP|"
    r"Chair(?:man|woman|person)?|Managing Director|Managing Partner|"
    r"General Manager|Executive Director|Director|Head of [A-Z][a-zA-Z ]{2,30}|"
    r"Partner|Principal|Owner)"
)
_NAME = r"[A-Z][a-zA-Z''.\-]+(?: [A-Z][a-zA-Z''.\-]+){1,3}"
LEADER_PATTERNS = [
    re.compile(rf"({_NAME})\s*[,–—\-|:]\s*({LEADER_TITLES}[a-zA-Z &,]*)"),
    re.compile(rf"({LEADER_TITLES}[a-zA-Z &]*)\s*[,–—\-|:]\s*({_NAME})"),
]

FOUNDED_PATTERNS = [
    re.compile(r"(?:founded|established|est\.?|started|incorporated|formed)"
               r"(?:\s+\w+){0,4}?\s+in\s+(\d{4})", re.IGNORECASE),
    re.compile(r"\bsince\s+(\d{4})\b", re.IGNORECASE),
]

HQ_PATTERNS = [
    re.compile(r"headquarter(?:ed|s)?\s+(?:is\s+|are\s+)?(?:in|at|near)\s+"
               r"([A-Z][A-Za-z .,'\-]{2,60}?)(?:[.;\n]|$)"),
    re.compile(r"\bbased\s+in\s+([A-Z][A-Za-z .,'\-]{2,60}?)(?:[.;\n]|,\s+[a-z])"),
]

CERT_PATTERN = re.compile(
    r"\b(ISO[/\- ]?(?:IEC )?\d{4,5}(?::\d{4})?|SOC\s?[123](?: Type [I]{1,2}| Type \d)?|"
    r"HIPAA|GDPR[- ]compliant|CE[- ]certified|CE mark(?:ing|ed)?|UL[- ]listed|"
    r"CMMI(?: Level \d)?|FDA[- ](?:approved|registered|cleared)|GMP[- ]certified|"
    r"HACCP|FSSC\s?22000|AS\s?9100|IATF\s?16949|PCI[- ]DSS|FedRAMP|"
    r"OHSAS\s?18001|Six Sigma)\b", re.IGNORECASE)

PARTNER_PATTERN = re.compile(
    r"(?:partner(?:ship|ed)?\s+with|in partnership with|official partner of|"
    r"authorized (?:dealer|distributor|reseller) (?:of|for)|certified partner of)\s+"
    r"([A-Z][A-Za-z0-9 &.,'\-]{2,50}?)(?:[.;\n]|,? (?:to|for|and|in)\b)")

_YEAR_MIN, _YEAR_MAX = 1750, 2027


def _excerpt(text: str, match_start: int, match_end: int) -> str:
    start = max(0, match_start - 60)
    end = min(len(text), match_end + 120)
    snippet = re.sub(r"\s+", " ", text[start:end]).strip()
    return ("…" if start > 0 else "") + snippet[:_EXCERPT_LEN] + ("…" if end < len(text) else "")


def _fact(field: str, value: Any, page: PageExtract, method: str,
          supporting: str, fact_type: str = FACT_TYPE_EXPLICIT,
          confidence: float = 0.9) -> Fact:
    return Fact(
        field=field, value=value, fact_type=fact_type,
        source_url=page.final_url, supporting_text=supporting[:_EXCERPT_LEN + 2],
        retrieved_at=page.retrieved_at, extraction_method=method,
        confidence=round(confidence, 2),
    )


def _jsonld_orgs(page: PageExtract) -> Iterable[dict[str, Any]]:
    def walk(node: Any):
        if isinstance(node, dict):
            types = node.get("@type", "")
            if isinstance(types, str):
                types = [types]
            if any(t in ("Organization", "Corporation", "LocalBusiness",
                         "ProfessionalService", "Store", "OnlineStore",
                         "MedicalOrganization", "EducationalOrganization",
                         "NGO", "GovernmentOrganization") or "Business" in str(t)
                   for t in types):
                yield node
            for v in node.values():
                yield from walk(v)
        elif isinstance(node, list):
            for item in node:
                yield from walk(item)
    for block in page.json_ld:
        yield from walk(block)


def _pages_of(pages: list[PageExtract], *types: str) -> list[PageExtract]:
    return [p for p in pages
            if p.classification and p.classification.page_type in types]


def _homepage(pages: list[PageExtract]) -> Optional[PageExtract]:
    hp = _pages_of(pages, "homepage")
    return hp[0] if hp else (pages[0] if pages else None)


# --------------------------------------------------------------------------
# individual extractors
# --------------------------------------------------------------------------

def _identity_facts(pages: list[PageExtract]) -> list[Fact]:
    facts: list[Fact] = []
    hp = _homepage(pages)

    for page in pages:
        for org in _jsonld_orgs(page):
            name = org.get("name")
            if isinstance(name, str) and name.strip():
                facts.append(_fact("company_name", name.strip(), page,
                                   "json_ld_organization",
                                   f'JSON-LD Organization name: "{name.strip()}"',
                                   FACT_TYPE_DETERMINISTIC, 0.95))
            legal = org.get("legalName")
            if isinstance(legal, str) and legal.strip():
                facts.append(_fact("legal_name", legal.strip(), page,
                                   "json_ld_organization",
                                   f'JSON-LD legalName: "{legal.strip()}"',
                                   FACT_TYPE_DETERMINISTIC, 0.95))
            alt = org.get("alternateName")
            if isinstance(alt, str) and alt.strip():
                facts.append(_fact("alternate_name", alt.strip(), page,
                                   "json_ld_organization",
                                   f'JSON-LD alternateName: "{alt.strip()}"',
                                   FACT_TYPE_DETERMINISTIC, 0.9))
            logo = org.get("logo")
            if isinstance(logo, dict):
                logo = logo.get("url")
            if isinstance(logo, str) and logo.startswith("http"):
                facts.append(_fact("logo_url", logo, page, "json_ld_organization",
                                   "JSON-LD Organization logo",
                                   FACT_TYPE_DETERMINISTIC, 0.9))
            desc = org.get("description")
            if isinstance(desc, str) and len(desc.strip()) > 20:
                facts.append(_fact("short_description", desc.strip()[:400], page,
                                   "json_ld_organization",
                                   "JSON-LD Organization description",
                                   FACT_TYPE_DETERMINISTIC, 0.85))
            founding = org.get("foundingDate")
            if isinstance(founding, str):
                m = re.match(r"(\d{4})", founding)
                if m and _YEAR_MIN <= int(m.group(1)) <= _YEAR_MAX:
                    facts.append(_fact("founded_year", int(m.group(1)), page,
                                       "json_ld_organization",
                                       f"JSON-LD foundingDate: {founding}",
                                       FACT_TYPE_DETERMINISTIC, 0.95))

    if hp:
        site_name = hp.open_graph.get("og:site_name")
        if site_name:
            facts.append(_fact("company_name", site_name, hp, "open_graph_site_name",
                               f'og:site_name: "{site_name}"',
                               FACT_TYPE_DETERMINISTIC, 0.85))
        og_desc = hp.open_graph.get("og:description")
        if og_desc and len(og_desc) > 20:
            facts.append(_fact("short_description", og_desc[:400], hp,
                               "open_graph_description",
                               "og:description on homepage",
                               FACT_TYPE_DETERMINISTIC, 0.75))
        if hp.meta_description and len(hp.meta_description) > 20:
            facts.append(_fact("short_description", hp.meta_description[:400], hp,
                               "meta_description",
                               "meta description on homepage",
                               FACT_TYPE_DETERMINISTIC, 0.7))
        if hp.title:
            # Title as weak name evidence: split on separators and take the
            # first segment that is not a generic word like "Home".
            generic = {"home", "homepage", "welcome", "index", "start", "official site"}
            segments = [s.strip() for s in re.split(r"\s*[|–—\\/·:]\s*|\s-\s", hp.title)
                        if s.strip()]
            candidate = next((s for s in segments if s.lower() not in generic), "")
            if 1 < len(candidate) < 80:
                facts.append(_fact("company_name", candidate, hp, "homepage_title",
                                   f'Homepage title: "{hp.title}"',
                                   FACT_TYPE_HEURISTIC, 0.5))

    # founded year from about/homepage text
    for page in _pages_of(pages, "about", "homepage") or pages[:2]:
        for rx in FOUNDED_PATTERNS:
            m = rx.search(page.clean_text)
            if m:
                year = int(m.group(1))
                if _YEAR_MIN <= year <= _YEAR_MAX:
                    facts.append(_fact("founded_year", year, page,
                                       "body_text_pattern",
                                       _excerpt(page.clean_text, m.start(), m.end()),
                                       FACT_TYPE_EXPLICIT, 0.85))
                break
    return facts


_GENERIC_HEADINGS = re.compile(
    r"^(related content|what'?s next|learn more|read more|get started|overview|"
    r"resources?|publications?|programs?|company|legal|support|faq|see also|"
    r"latest|featured|explore|discover|more|menu|navigation|footer|"
    r"help and security|newsletter|subscribe|share|table of contents)$",
    re.IGNORECASE)


def _boilerplate_headings(pages: list[PageExtract]) -> set[str]:
    """Heading texts that repeat across many pages are site chrome, not content."""
    from collections import Counter
    counts: Counter[str] = Counter()
    for page in pages:
        for text in {h["text"].strip().lower() for h in page.headings}:
            counts[text] += 1
    threshold = max(3, int(len(pages) * 0.3))
    return {t for t, n in counts.items() if n >= threshold}


def _business_facts(pages: list[PageExtract]) -> list[Fact]:
    facts: list[Fact] = []
    boiler = _boilerplate_headings(pages)
    offering_map = {
        "product": "products", "service": "services", "solution": "solutions",
        "industry": "industries_served", "application": "applications",
        "case_study": "case_studies", "customer": "customer_references",
    }
    for ptype, field in offering_map.items():
        for page in _pages_of(pages, ptype):
            h1 = next((h["text"] for h in page.headings if h["level"] == "h1"), None)
            name = h1 or page.title
            if not name:
                continue
            name = re.split(r"\s*[|–—]\s*", name)[0].strip()
            if not (2 < len(name) < 120):
                continue
            facts.append(_fact(
                field, name, page, "classified_page_heading",
                f"Page classified as {ptype} "
                f"({', '.join(page.classification.signals[:2])}); H1/title: \"{name}\"",
                FACT_TYPE_HEURISTIC,
                min(0.8, page.classification.confidence)))
            # Sub-offerings listed as H2/H3 on category pages
            if len(page.headings) > 2:
                for h in page.headings[1:12]:
                    if h["level"] in ("h2", "h3") and 2 < len(h["text"]) < 90:
                        txt = h["text"].strip()
                        if re.search(r"contact|learn more|why |get started|request|about",
                                     txt, re.IGNORECASE):
                            continue
                        if txt.lower() in boiler or _GENERIC_HEADINGS.match(txt):
                            continue
                        facts.append(_fact(
                            field, txt, page, "classified_page_subheading",
                            f"Sub-heading on {ptype} page: \"{txt}\"",
                            FACT_TYPE_HEURISTIC, 0.55))

    for page in pages:
        for m in CERT_PATTERN.finditer(page.clean_text):
            facts.append(_fact("certifications", m.group(1).upper().replace("  ", " "),
                               page, "body_text_pattern",
                               _excerpt(page.clean_text, m.start(), m.end()),
                               FACT_TYPE_EXPLICIT, 0.85))
        for m in PARTNER_PATTERN.finditer(page.clean_text):
            partner = m.group(1).strip().rstrip(".,")
            if 2 < len(partner) < 60:
                facts.append(_fact("partnerships", partner, page,
                                   "body_text_pattern",
                                   _excerpt(page.clean_text, m.start(), m.end()),
                                   FACT_TYPE_EXPLICIT, 0.7))
    return facts


def _geography_facts(pages: list[PageExtract]) -> list[Fact]:
    facts: list[Fact] = []
    for page in pages:
        for org in _jsonld_orgs(page):
            addr = org.get("address")
            addrs = addr if isinstance(addr, list) else [addr]
            for a in addrs:
                if not isinstance(a, dict):
                    continue
                parts = [a.get(k) for k in ("streetAddress", "addressLocality",
                                            "addressRegion", "postalCode",
                                            "addressCountry")]
                parts = [str(p).strip() for p in parts if p]
                if parts:
                    facts.append(_fact("contact_addresses", ", ".join(parts), page,
                                       "json_ld_postal_address",
                                       "JSON-LD PostalAddress",
                                       FACT_TYPE_DETERMINISTIC, 0.92))
    for page in _pages_of(pages, "about", "homepage", "contact", "location") or pages[:3]:
        for rx in HQ_PATTERNS:
            m = rx.search(page.clean_text)
            if m:
                place = m.group(1).strip().rstrip(".,")
                if 2 < len(place) < 60:
                    facts.append(_fact("headquarters", place, page,
                                       "body_text_pattern",
                                       _excerpt(page.clean_text, m.start(), m.end()),
                                       FACT_TYPE_EXPLICIT, 0.8))
                break
    for page in _pages_of(pages, "location"):
        for h in page.headings:
            if h["level"] in ("h2", "h3") and 2 < len(h["text"]) < 60:
                if re.search(r"contact|form|touch|find", h["text"], re.IGNORECASE):
                    continue
                facts.append(_fact("office_locations", h["text"], page,
                                   "location_page_heading",
                                   f"Heading on locations page: \"{h['text']}\"",
                                   FACT_TYPE_HEURISTIC, 0.6))
    return facts


def _people_facts(pages: list[PageExtract]) -> list[Fact]:
    facts: list[Fact] = []
    seen: set[str] = set()
    candidates = _pages_of(pages, "leadership", "about") or []
    for page in candidates:
        # JSON-LD Person entries
        def walk(node: Any):
            if isinstance(node, dict):
                t = node.get("@type")
                if (t == "Person" or (isinstance(t, list) and "Person" in t)):
                    yield node
                for v in node.values():
                    yield from walk(v)
            elif isinstance(node, list):
                for item in node:
                    yield from walk(item)
        for block in page.json_ld:
            for person in walk(block):
                name = person.get("name")
                title = person.get("jobTitle") or ""
                if isinstance(name, str) and name.strip():
                    key = name.strip().lower()
                    if key in seen:
                        continue
                    seen.add(key)
                    facts.append(_fact(
                        "leadership", {"name": name.strip(), "title": str(title).strip() or None},
                        page, "json_ld_person",
                        f"JSON-LD Person: {name}" + (f", {title}" if title else ""),
                        FACT_TYPE_DETERMINISTIC, 0.92))
        # Text patterns "Name - Title" / "Title - Name"
        source = page.clean_text + "\n" + " \n".join(h["text"] for h in page.headings)
        for idx, rx in enumerate(LEADER_PATTERNS):
            for m in rx.finditer(source):
                name, title = (m.group(1), m.group(2)) if idx == 0 else (m.group(2), m.group(1))
                name, title = name.strip(), re.sub(r"\s+", " ", title.strip())
                if len(name.split()) < 2 or len(name) > 50 or len(title) > 70:
                    continue
                # Organizations/committees/departments masquerading as names
                if re.search(r"\b(trust|benefit|board|committee|team|group|inc|llc|"
                             r"gmbh|ltd|corp|company|division|department|council|"
                             r"foundation|institute|association|assurance|operations|"
                             r"commercial|marketing|sales|finance|quality|regulatory|"
                             r"engineering|product|research|development|resources|"
                             r"business|strategy|technology|manufacturing|clinical|"
                             r"affairs|administration|supply|chain|customer|success|"
                             r"talent|global|corporate|digital|innovation|scientific)\b",
                             name, re.IGNORECASE):
                    continue
                key = name.lower()
                if key in seen:
                    continue
                seen.add(key)
                facts.append(_fact(
                    "leadership", {"name": name, "title": title}, page,
                    "name_title_pattern",
                    _excerpt(source, m.start(), m.end()),
                    FACT_TYPE_DETERMINISTIC, 0.7))
    return facts


def _contact_facts(pages: list[PageExtract]) -> list[Fact]:
    facts: list[Fact] = []
    seen_email: set[str] = set()
    seen_phone: set[str] = set()
    seen_social: set[str] = set()
    for page in pages:
        for email in page.emails:
            if email in seen_email:
                continue
            seen_email.add(email)
            facts.append(_fact("emails", email, page, "email_pattern",
                               f"Email found on page: {email}",
                               FACT_TYPE_DETERMINISTIC, 0.9))
        for phone in page.phones:
            if phone in seen_phone:
                continue
            seen_phone.add(phone)
            facts.append(_fact("phones", phone, page, "phone_pattern",
                               f"Phone found on page: {phone}",
                               FACT_TYPE_DETERMINISTIC, 0.75))
        for social in page.social_links:
            if social in seen_social:
                continue
            seen_social.add(social)
            facts.append(_fact("social_profiles", social, page, "social_link",
                               f"Social profile link: {social}",
                               FACT_TYPE_DETERMINISTIC, 0.9))
        if page.classification and page.classification.page_type == "contact":
            facts.append(_fact("contact_page_urls", page.final_url, page,
                               "page_classification",
                               f"Page classified as contact page "
                               f"({', '.join(page.classification.signals[:2])})",
                               FACT_TYPE_HEURISTIC, page.classification.confidence))
    return facts


NEWS_SIGNAL_PATTERNS = [
    ("product_launches", re.compile(r"\b(launch(?:es|ed)?|introduc(?:es|ed)|unveil(?:s|ed)?|releases?)\b", re.I)),
    ("partnerships", re.compile(r"\b(partner(?:s|ship|ed)?|collaborat(?:es|ion)|teams? up|joins? forces)\b", re.I)),
    ("acquisitions", re.compile(r"\b(acquir(?:es|ed|sition)|merges? with|merger)\b", re.I)),
    ("funding", re.compile(r"\b(funding|investment|raises?|raised|series [a-e]\b|venture capital)\b", re.I)),
    ("expansion", re.compile(r"\b(expand(?:s|ed|ing|sion)|new (?:office|facility|plant|factory|location)|opens? (?:new|its))\b", re.I)),
]


def _activity_facts(pages: list[PageExtract]) -> list[Fact]:
    facts: list[Fact] = []
    for page in _pages_of(pages, "news", "press_release", "blog"):
        title = None
        h1 = next((h["text"] for h in page.headings if h["level"] == "h1"), None)
        title = h1 or page.title
        if title:
            title = re.split(r"\s*[|–—]\s*", title)[0].strip()
        item = {
            "title": title, "url": page.final_url,
            "date": page.published_date or page.modified_date,
            "type": page.classification.page_type,
        }
        facts.append(_fact("recent_news", item, page, "news_page",
                           f"News/blog page: \"{title}\"" if title else "News/blog page",
                           FACT_TYPE_HEURISTIC,
                           min(0.75, page.classification.confidence)))
        haystack = (title or "") + "\n" + page.clean_text[:2000]
        for field, rx in NEWS_SIGNAL_PATTERNS:
            m = rx.search(haystack)
            if m:
                facts.append(_fact(field, {"headline": title, "url": page.final_url},
                                   page, "news_keyword_signal",
                                   _excerpt(haystack, m.start(), m.end()),
                                   FACT_TYPE_HEURISTIC, 0.55))
    for page in _pages_of(pages, "careers"):
        roles = [h["text"] for h in page.headings
                 if h["level"] in ("h2", "h3") and 3 < len(h["text"]) < 80][:15]
        if roles:
            facts.append(_fact("hiring_themes", roles, page, "careers_page_headings",
                               "Headings on careers page: " + "; ".join(roles[:5]),
                               FACT_TYPE_HEURISTIC, 0.6))
        facts.append(_fact("careers_page", page.final_url, page,
                           "page_classification", "Careers page present",
                           FACT_TYPE_HEURISTIC, page.classification.confidence))
    return facts


def extract_facts(pages: list[PageExtract]) -> list[Fact]:
    """Run all fact extractors over the collected pages."""
    facts: list[Fact] = []
    facts.extend(_identity_facts(pages))
    facts.extend(_business_facts(pages))
    facts.extend(_geography_facts(pages))
    facts.extend(_people_facts(pages))
    facts.extend(_contact_facts(pages))
    facts.extend(_activity_facts(pages))
    return facts
