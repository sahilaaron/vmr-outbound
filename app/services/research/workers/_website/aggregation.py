"""Aggregate individual facts into the consolidated company dossier.

Fields that cannot be established stay null / empty lists, with an explicit
"unknown_fields" list so absence is visible rather than silent.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Any, Optional

from .models import Fact


def _best(facts: list[Fact]) -> Optional[Fact]:
    return max(facts, key=lambda f: f.confidence) if facts else None


def _sourced(value: Any, fact: Fact) -> dict[str, Any]:
    return {"value": value, "source_url": fact.source_url,
            "confidence": fact.confidence, "fact_type": fact.fact_type}


def _collect_list(by_field: dict[str, list[Fact]], field: str,
                  min_confidence: float = 0.0, limit: int = 40) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    for fact in sorted(by_field.get(field, []), key=lambda f: -f.confidence):
        if fact.confidence < min_confidence:
            continue
        key = str(fact.value).strip().lower()
        if not key or key in seen:
            continue
        seen.add(key)
        items.append(_sourced(fact.value, fact))
        if len(items) >= limit:
            break
    return items


def build_dossier(domain: str, facts: list[Fact],
                  page_summaries: list[dict[str, Any]]) -> dict[str, Any]:
    by_field: dict[str, list[Fact]] = defaultdict(list)
    for f in facts:
        by_field[f.field].append(f)

    def single(field: str) -> Optional[dict[str, Any]]:
        best = _best(by_field.get(field, []))
        return _sourced(best.value, best) if best else None

    dossier: dict[str, Any] = {
        "domain": domain,
        "identity": {
            "company_name": single("company_name"),
            "legal_name": single("legal_name"),
            "alternate_names": _collect_list(by_field, "alternate_name"),
            "logo_url": single("logo_url"),
            "short_description": single("short_description"),
            "founded_year": single("founded_year"),
            "company_type": single("company_type"),
        },
        "business": {
            "products": _collect_list(by_field, "products"),
            "services": _collect_list(by_field, "services"),
            "solutions": _collect_list(by_field, "solutions"),
            "industries_served": _collect_list(by_field, "industries_served"),
            "applications": _collect_list(by_field, "applications"),
            "customer_references": _collect_list(by_field, "customer_references"),
            "case_studies": _collect_list(by_field, "case_studies"),
            "certifications": _collect_list(by_field, "certifications"),
            "partnerships": _collect_list(by_field, "partnerships"),
        },
        "geography": {
            "headquarters": single("headquarters"),
            "office_locations": _collect_list(by_field, "office_locations"),
            "contact_addresses": _collect_list(by_field, "contact_addresses"),
        },
        "people": {
            "leadership": _collect_list(by_field, "leadership", limit=30),
        },
        "contact": {
            "emails": _collect_list(by_field, "emails"),
            "phones": _collect_list(by_field, "phones"),
            "social_profiles": _collect_list(by_field, "social_profiles"),
            "contact_page_urls": _collect_list(by_field, "contact_page_urls"),
        },
        "activity": {
            "recent_news": _collect_list(by_field, "recent_news", limit=20),
            "product_launches": _collect_list(by_field, "product_launches", limit=10),
            "partnership_signals": _collect_list(by_field, "partnerships", limit=10),
            "acquisition_signals": _collect_list(by_field, "acquisitions", limit=10),
            "funding_signals": _collect_list(by_field, "funding", limit=10),
            "expansion_signals": _collect_list(by_field, "expansion", limit=10),
            "hiring_themes": _collect_list(by_field, "hiring_themes", limit=5),
            "careers_page": single("careers_page"),
        },
        "source_pages": page_summaries,
    }

    # Make unknowns explicit.
    unknown: list[str] = []

    def scan(prefix: str, section: dict[str, Any]) -> None:
        for key, val in section.items():
            if val is None or val == []:
                unknown.append(f"{prefix}.{key}")

    for section_name in ("identity", "business", "geography", "people",
                         "contact", "activity"):
        scan(section_name, dossier[section_name])
    dossier["unknown_fields"] = unknown
    dossier["fact_count"] = len(facts)
    return dossier
