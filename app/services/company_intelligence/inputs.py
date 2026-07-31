"""What a Company Intelligence producer is allowed to read (CI-001).

This module is the boundary. Everything the producer may consider is assembled
here, from persisted artifacts only, and handed over as one frozen object. The
producer cannot reach past it: it receives an :class:`IntelligenceInput` and has
no session, no network and no way to ask for anything that is not in it.

Three consequences worth being explicit about.

**No browsing, structurally.** The first release classifies evidence somebody
already gathered and committed. A producer that could fetch would be a second
Research implementation with none of Research's guarantees — no retrieval time,
no stored source, no reviewable dossier — and the classification it produced
would be indistinguishable from one that read a real page.

**Idempotency is a property of this object.** :attr:`IntelligenceInput.digest`
is a SHA-256 over the exact dossier version, the exact set of sourced facts (by
id and content hash), the active taxonomy editions, and the producer and policy
versions. Same digest means the same question was already answered; a new
dossier version, a new fact, a new vocabulary or a new producer changes it. That
is what makes "create a new version when the input changes, and only then"
checkable rather than aspirational.

**Sufficiency is decided here, once.** :func:`assemble` returns a reason code
instead of an input when a Company has nothing to classify, so the job runner,
the backfill planner and the Admin screen all give an operator the same answer
to "why not this company?".
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.company import Company
from app.models.company_dossier import CompanyDossierVersion
from app.models.enums import InsightState, InsightSubject
from app.models.insight import Insight, InsightEvidence
from app.services.companies import dossiers
from app.services.company_intelligence import taxonomy as taxonomy_service
from app.services.resolution import store as resolution_store

#: How many sourced facts are offered to a producer. A bound is required — the
#: prompt has a finite budget and an unbounded input silently truncates
#: somewhere less visible. Facts are ordered deterministically, so the same
#: company always offers the same subset and the digest stays stable.
MAX_SOURCED_FACTS = 120

#: How many evidence rows are carried per fact.
MAX_EVIDENCE_PER_FACT = 3

#: Truthful reasons a Company cannot be classified. Shared by the job runner,
#: the backfill planner and the Admin surface so one condition has one name.
REASON_NO_DOSSIER = "no_current_dossier"
REASON_NO_EVIDENCE = "no_sourced_facts"
REASON_COMPANY_MISSING = "company_missing"


class IntelligenceInputError(ValueError):
    """A Company that cannot be classified, with a reason code attached."""

    def __init__(self, reason_code: str, message: str) -> None:
        super().__init__(message)
        self.reason_code = reason_code
        self.message = message


@dataclass(frozen=True)
class EvidenceRef:
    """One persisted observation behind one sourced fact."""

    evidence_id: uuid.UUID
    source_url: str
    excerpt: str | None
    retrieved_at: str | None

    def as_payload(self) -> dict[str, Any]:
        return {
            "evidence_id": str(self.evidence_id),
            "source_url": self.source_url,
            "excerpt": self.excerpt,
            "retrieved_at": self.retrieved_at,
        }


@dataclass(frozen=True)
class SourcedFactRef:
    """One committed INS-001 claim, with the evidence that supports it.

    ``ref`` is the short handle the producer is asked to cite — ``"F3"`` rather
    than a UUID. Short handles are not decoration: a model asked to echo a UUID
    gets one character wrong often enough to matter, and a citation that does not
    resolve is indistinguishable from a fabricated one.
    """

    ref: str
    insight_id: uuid.UUID
    claim: str
    content_hash: str | None
    evidence: tuple[EvidenceRef, ...] = ()

    def as_payload(self) -> dict[str, Any]:
        return {
            "ref": self.ref,
            "claim": self.claim,
            "sources": [item.source_url for item in self.evidence],
        }


@dataclass(frozen=True)
class IntelligenceInput:
    """Everything one production run may read, and nothing else."""

    company_id: uuid.UUID
    company_name: str
    company_domain: str | None
    #: ``confirmed`` / ``provisional`` / ``unresolved`` / ``None`` when the
    #: automatic resolution policy never decided. Carried so a classification
    #: made against a provisional domain can be read as exactly that.
    domain_authority: str | None
    dossier_version_id: uuid.UUID
    dossier_version_number: int
    dossier_sections: dict[str, Any]
    facts: tuple[SourcedFactRef, ...]
    taxonomy_versions: dict[str, str]
    producer: str
    producer_version: str
    policy_version: str
    digest: str = field(default="", compare=False)

    @property
    def fact_ids(self) -> tuple[str, ...]:
        return tuple(str(fact.insight_id) for fact in self.facts)

    def fact_by_ref(self, ref: str) -> SourcedFactRef | None:
        needle = ref.strip().upper()
        for fact in self.facts:
            if fact.ref == needle:
                return fact
        return None

    @property
    def source_urls(self) -> frozenset[str]:
        """Every URL the producer was actually shown.

        A citation to a URL outside this set did not come from the evidence, and
        the producer's answer is refused for it rather than stored with a
        source nobody can check.
        """

        urls = {item.source_url for fact in self.facts for item in fact.evidence if item.source_url}
        for entry in self.dossier_sections.get("sources") or []:
            if isinstance(entry, dict):
                url = entry.get("url") or entry.get("source_url")
                if isinstance(url, str) and url:
                    urls.add(url)
        return frozenset(urls)


def compute_digest(
    *,
    dossier_version_id: uuid.UUID,
    fact_fingerprints: tuple[str, ...],
    taxonomy_versions: dict[str, str],
    producer: str,
    producer_version: str,
    policy_version: str,
) -> str:
    """The stable identity of one production question.

    Deterministic: sorted keys, fixed separators, facts in the order the
    assembler produced them (which is itself deterministic). Two runs that would
    ask the identical question of the identical vocabulary produce the identical
    digest, on any machine.
    """

    material = {
        "dossier_version_id": str(dossier_version_id),
        "facts": list(fact_fingerprints),
        "taxonomy_versions": dict(sorted(taxonomy_versions.items())),
        "producer": producer,
        "producer_version": producer_version,
        "policy_version": policy_version,
    }
    encoded = json.dumps(material, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def assemble(
    session: Session,
    *,
    company: Company,
    producer: str,
    producer_version: str,
    policy_version: str,
    max_facts: int = MAX_SOURCED_FACTS,
) -> IntelligenceInput:
    """Gather one Company's committed evidence, or raise with a reason code."""

    current = dossiers.current_version(session, company_id=company.id)
    if current is None:
        raise IntelligenceInputError(
            REASON_NO_DOSSIER,
            f"{company.name!r} has no current research dossier, so there is nothing "
            "to classify. Run the Research Agent first.",
        )

    facts = _facts(session, company_id=company.id, limit=max_facts)
    sections = _sections(current)
    if not facts and not sections:
        raise IntelligenceInputError(
            REASON_NO_EVIDENCE,
            f"{company.name!r} has a dossier but it carries no sourced facts and no "
            "populated sections; a classification would rest on nothing.",
        )

    taxonomy_versions = taxonomy_service.active_versions(session)
    state = resolution_store.company_state(session, company.id)

    digest = compute_digest(
        dossier_version_id=current.id,
        fact_fingerprints=tuple(f"{fact.insight_id}:{fact.content_hash or ''}" for fact in facts),
        taxonomy_versions=taxonomy_versions,
        producer=producer,
        producer_version=producer_version,
        policy_version=policy_version,
    )

    return IntelligenceInput(
        company_id=company.id,
        company_name=company.name,
        company_domain=company.domain,
        domain_authority=state.value if state is not None else None,
        dossier_version_id=current.id,
        dossier_version_number=current.version_number,
        dossier_sections=sections,
        facts=facts,
        taxonomy_versions=taxonomy_versions,
        producer=producer,
        producer_version=producer_version,
        policy_version=policy_version,
        digest=digest,
    )


def _sections(version: CompanyDossierVersion) -> dict[str, Any]:
    """The dossier sections this version actually addressed.

    A NULL section is omitted rather than sent as an empty one. "Did not look"
    and "looked and found nothing" are different facts, and flattening them here
    would teach the producer that a gap is an absence.
    """

    return {
        name: getattr(version, name)
        for name in dossiers.SECTION_COLUMNS
        if getattr(version, name) is not None
    }


def _facts(session: Session, *, company_id: uuid.UUID, limit: int) -> tuple[SourcedFactRef, ...]:
    """Committed, supported claims about this Company, deterministically ordered.

    Only ``SUPPORTED`` claims. An ``UNKNOWN`` insight records a gap the Insights
    Agent named, and a ``CONFLICTING`` one records a claim already known to be
    disputed; neither is material a classifier should treat as a fact about the
    company. Both remain visible in the workspace where they belong.
    """

    rows = list(
        session.scalars(
            select(Insight)
            .where(
                Insight.company_id == company_id,
                Insight.subject == InsightSubject.COMPANY,
                Insight.state == InsightState.SUPPORTED,
            )
            # Deterministic and stable across runs: creation order, then id as
            # the tie-break, so two facts written in the same transaction never
            # swap places and change the digest.
            .order_by(Insight.created_at.asc(), Insight.id.asc())
            .limit(limit)
        ).all()
    )
    if not rows:
        return ()

    evidence_by_insight: dict[uuid.UUID, list[EvidenceRef]] = {}
    for evidence in session.scalars(
        select(InsightEvidence)
        .where(InsightEvidence.insight_id.in_([row.id for row in rows]))
        .order_by(InsightEvidence.created_at.asc(), InsightEvidence.id.asc())
    ).all():
        bucket = evidence_by_insight.setdefault(evidence.insight_id, [])
        if len(bucket) >= MAX_EVIDENCE_PER_FACT:
            continue
        bucket.append(
            EvidenceRef(
                evidence_id=evidence.id,
                source_url=evidence.source_url,
                excerpt=(evidence.excerpt or evidence.evidence_summary or None),
                retrieved_at=(evidence.retrieved_at.isoformat() if evidence.retrieved_at else None),
            )
        )

    return tuple(
        SourcedFactRef(
            ref=f"F{index}",
            insight_id=row.id,
            claim=row.claim,
            content_hash=row.content_hash,
            evidence=tuple(evidence_by_insight.get(row.id, ())),
        )
        for index, row in enumerate(rows, start=1)
    )
