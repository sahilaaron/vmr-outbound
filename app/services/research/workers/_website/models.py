"""Dataclasses shared across the application."""
from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class FetchResult:
    requested_url: str
    final_url: str
    status_code: Optional[int]
    ok: bool
    method: str  # "http" | "playwright"
    content_type: Optional[str] = None
    html: Optional[str] = None
    error: Optional[str] = None
    fetched_at: str = field(default_factory=utcnow_iso)
    warnings: list[str] = field(default_factory=list)


@dataclass
class URLCandidate:
    url: str
    priority: float
    discovery_source: str  # homepage_link | nav | footer | sitemap | robots | guess | link
    depth: int = 0
    anchor_text: str = ""
    processed: bool = False
    skip_reason: Optional[str] = None
    retrieval_method: Optional[str] = None
    http_status: Optional[int] = None
    page_type: Optional[str] = None
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Classification:
    page_type: str
    confidence: float
    confidence_label: str  # high | medium | low
    signals: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class PageExtract:
    requested_url: str
    final_url: str
    http_status: Optional[int]
    retrieval_method: str
    retrieved_at: str
    title: Optional[str] = None
    meta_description: Optional[str] = None
    canonical_url: Optional[str] = None
    language: Optional[str] = None
    headings: list[dict[str, str]] = field(default_factory=list)
    clean_text: str = ""
    internal_links: list[dict[str, str]] = field(default_factory=list)
    external_links: list[dict[str, str]] = field(default_factory=list)
    emails: list[str] = field(default_factory=list)
    phones: list[str] = field(default_factory=list)
    social_links: list[str] = field(default_factory=list)
    json_ld: list[Any] = field(default_factory=list)
    open_graph: dict[str, str] = field(default_factory=dict)
    published_date: Optional[str] = None
    modified_date: Optional[str] = None
    classification: Optional[Classification] = None
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        return d


# Fact types, in decreasing order of certainty.
FACT_TYPE_EXPLICIT = "explicit"                  # stated verbatim on the site
FACT_TYPE_DETERMINISTIC = "deterministic_extraction"  # regex/structured-data extraction
FACT_TYPE_HEURISTIC = "heuristic_classification"      # derived from page classification
FACT_TYPE_INFERENCE = "inference"                # semantic inference (avoided in v1)
FACT_TYPE_UNKNOWN = "unknown"


@dataclass
class Fact:
    field: str
    value: Any
    fact_type: str
    source_url: str
    supporting_text: str
    retrieved_at: str
    extraction_method: str
    confidence: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class JsonEncoder(json.JSONEncoder):
    def default(self, o: Any) -> Any:  # pragma: no cover - trivial
        if isinstance(o, Enum):
            return o.value
        if isinstance(o, datetime):
            return o.isoformat()
        if hasattr(o, "to_dict"):
            return o.to_dict()
        return super().default(o)


def dump_json(data: Any, path: str) -> None:
    """Write JSON deterministically, UTF-8, Windows-safe."""
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        json.dump(data, f, cls=JsonEncoder, indent=2, ensure_ascii=False, sort_keys=False)
        f.write("\n")
