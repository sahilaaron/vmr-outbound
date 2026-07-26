"""The unified CRM list: canonical contacts and pending captures in one table.

A person the operator saved must not vanish from the workspace because the
system has not finished resolving their company. So the list is a union of two
record kinds:

* ``contact`` — a permanent ``contacts`` row;
* ``pending_capture`` — an immutable capture whose outcome is
  ``unmatched_staged`` or ``ambiguous_review``, so no contact row exists yet.

The union happens in SQL, not in Python. Merging two result sets after the fact
would make ``LIMIT``/``OFFSET`` lie: page 2 would be computed from two
independently-paginated queries and would silently skip or repeat people. The
projection below gives both legs the same column shape so one ``UNION ALL`` can
be ordered, counted and paged correctly, and the rows are hydrated afterwards.

**No function in this module accepts a campaign identifier.** Campaign
membership is downstream execution state; it has no bearing on who exists.

The filter predicates are deliberately built as composable pieces rather than
inlined, because APP-006's saved audiences are the same predicates over the same
columns. Duplicating this query later would guarantee the two drift apart.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

from sqlalchemy import Select, and_, exists, func, literal, or_, select, union_all
from sqlalchemy.orm import Session

from app.models.contact import Contact
from app.models.contact_capture import ContactLabel, ContactLabelAssignment
from app.models.enums import (
    CaptureIdentityState,
    LinkedInSnapshotOutcome,
    SuppressionType,
)
from app.models.linkedin_profile import (
    LinkedInProfileExperienceObservation,
    LinkedInProfileSnapshot,
)
from app.models.suppression import Suppression
from app.services.crm.states import PENDING_OUTCOMES, WorkflowStates, states_for_capture
from app.services.crm.states import states_for_contact as _states_for_contact

RecordKind = Literal["contact", "pending_capture"]

# The four operator views #158 asks for. "all" is the default working set: it
# shows canonical contacts and pending captures together, because that is the
# whole point of the workspace.
VIEW_ALL = "all"
VIEW_AWAITING_COMPANY = "awaiting_company"
VIEW_AMBIGUOUS = "ambiguous"
VIEW_SUPPRESSED = "suppressed"
VIEWS: tuple[str, ...] = (VIEW_ALL, VIEW_AWAITING_COMPANY, VIEW_AMBIGUOUS, VIEW_SUPPRESSED)

SORT_RECENT = "recent"
SORT_NAME = "name"
SORT_COMPANY = "company"
SORTS: tuple[str, ...] = (SORT_RECENT, SORT_NAME, SORT_COMPANY)

MAX_PAGE_SIZE = 200


@dataclass(frozen=True)
class CrmRow:
    """One row of the CRM list, whichever kind of record produced it.

    ``kind`` is the discriminator the template branches on. A pending capture
    has no ``contact_id`` and usually no company domain — that absence is the
    information, not a gap to paper over.
    """

    kind: RecordKind
    record_id: uuid.UUID
    full_name: str
    title: str | None
    company_name: str | None
    company_domain: str | None
    location: str | None
    linkedin_url: str | None
    email: str | None
    source: str
    states: WorkflowStates
    labels: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    last_updated: datetime | None = None

    @property
    def contact_id(self) -> uuid.UUID | None:
        return self.record_id if self.kind == "contact" else None

    @property
    def capture_id(self) -> uuid.UUID | None:
        return self.record_id if self.kind == "pending_capture" else None

    @property
    def detail_url(self) -> str:
        if self.kind == "contact":
            return f"/contacts/{self.record_id}"
        return f"/captures/{self.record_id}"


@dataclass(frozen=True)
class CrmFilters:
    """Every filter the list supports. All optional; none is a campaign.

    Held as one object so the same shape can be handed to a saved-audience rule
    later without re-deriving it from query-string parsing.
    """

    view: str = VIEW_ALL
    search: str | None = None
    label_slug: str | None = None
    company: str | None = None
    source: str | None = None
    has_linkedin: bool | None = None
    has_email: bool | None = None
    identity: CaptureIdentityState | None = None
    sort: str = SORT_RECENT

    def normalized(self) -> CrmFilters:
        """Coerce anything unrecognised back to a safe default.

        Query strings are operator input and may be edited by hand; an unknown
        view should show the default working set rather than an error page.
        """

        return CrmFilters(
            view=self.view if self.view in VIEWS else VIEW_ALL,
            search=(self.search or "").strip() or None,
            label_slug=(self.label_slug or "").strip() or None,
            company=(self.company or "").strip() or None,
            source=(self.source or "").strip() or None,
            has_linkedin=self.has_linkedin,
            has_email=self.has_email,
            identity=self.identity,
            sort=self.sort if self.sort in SORTS else SORT_RECENT,
        )


# --------------------------------------------------------------------------
# Suppression as a SQL predicate
# --------------------------------------------------------------------------
# The ledger is authoritative, and `evaluate_suppression` is the right call for
# a single identity. It cannot be used to filter a list, so the same rule is
# expressed once here as an EXISTS: an active email suppression on the address,
# or an active domain suppression on the company domain. Values are stored
# lower-cased (see services/suppressions.py), so both sides are lowered.
def _suppressed_predicate(email_col: Any, domain_col: Any) -> Any:
    return exists(
        select(literal(1))
        .select_from(Suppression)
        .where(
            Suppression.is_active.is_(True),
            or_(
                and_(
                    Suppression.suppression_type == SuppressionType.EMAIL,
                    email_col.is_not(None),
                    Suppression.value == func.lower(email_col),
                ),
                and_(
                    Suppression.suppression_type == SuppressionType.DOMAIN,
                    domain_col.is_not(None),
                    Suppression.value == func.lower(domain_col),
                ),
            ),
        )
        .correlate_except(Suppression)
    )


def _label_predicate(anchor_col: Any, slug: str, *, capture_anchor: bool) -> Any:
    """EXISTS a label assignment with this slug, on the appropriate anchor."""

    anchor_match = (
        and_(
            ContactLabelAssignment.contact_id.is_(None),
            ContactLabelAssignment.capture_id == anchor_col,
        )
        if capture_anchor
        else ContactLabelAssignment.contact_id == anchor_col
    )
    return exists(
        select(literal(1))
        .select_from(ContactLabelAssignment)
        .join(ContactLabel, ContactLabel.id == ContactLabelAssignment.label_id)
        .where(anchor_match, ContactLabel.slug == slug)
        .correlate_except(ContactLabelAssignment, ContactLabel)
    )


# --------------------------------------------------------------------------
# The two legs of the union
# --------------------------------------------------------------------------
# Both project to the same column shape: kind, id, sort_name, sort_company,
# last_updated. Only what ORDER BY and COUNT need is carried through the union;
# everything else is hydrated afterwards from the real rows.
def _contact_leg(filters: CrmFilters) -> Select[Any] | None:
    """Canonical contacts, or None when the view excludes them entirely."""

    if filters.view in (VIEW_AWAITING_COMPANY, VIEW_AMBIGUOUS):
        return None
    if filters.identity is not None and filters.identity != CaptureIdentityState.CANONICAL:
        return None

    sort_name = func.lower(Contact.first_name + literal(" ") + Contact.last_name)
    leg = select(
        literal("contact").label("kind"),
        Contact.id.label("record_id"),
        sort_name.label("sort_name"),
        func.lower(func.coalesce(Contact.company_name, literal(""))).label("sort_company"),
        Contact.updated_at.label("last_updated"),
    ).where(Contact.merged_into_id.is_(None))  # tombstones are not people

    suppressed = _suppressed_predicate(Contact.email, Contact.company_domain)
    if filters.view == VIEW_SUPPRESSED:
        leg = leg.where(suppressed)

    if filters.search:
        needle = f"%{filters.search.lower()}%"
        leg = leg.where(
            or_(
                func.lower(Contact.first_name).like(needle),
                func.lower(Contact.last_name).like(needle),
                func.lower(Contact.first_name + literal(" ") + Contact.last_name).like(needle),
                func.lower(func.coalesce(Contact.company_name, literal(""))).like(needle),
                func.lower(func.coalesce(Contact.company_domain, literal(""))).like(needle),
                func.lower(func.coalesce(Contact.email, literal(""))).like(needle),
                func.lower(func.coalesce(Contact.title, literal(""))).like(needle),
            )
        )
    if filters.company:
        leg = leg.where(
            or_(
                func.lower(func.coalesce(Contact.company_name, literal(""))).like(
                    f"%{filters.company.lower()}%"
                ),
                func.lower(func.coalesce(Contact.company_domain, literal(""))).like(
                    f"%{filters.company.lower()}%"
                ),
            )
        )
    if filters.label_slug:
        leg = leg.where(_label_predicate(Contact.id, filters.label_slug, capture_anchor=False))
    if filters.has_linkedin is True:
        leg = leg.where(Contact.linkedin_url.is_not(None))
    elif filters.has_linkedin is False:
        leg = leg.where(Contact.linkedin_url.is_(None))
    if filters.has_email is True:
        leg = leg.where(Contact.email.is_not(None))
    elif filters.has_email is False:
        leg = leg.where(Contact.email.is_(None))
    if filters.source:
        # A contact's acquisition source is "capture" when any capture resolved
        # to it, and "import" otherwise. Expressed as EXISTS so it stays a
        # single query.
        from_capture = exists(
            select(literal(1))
            .select_from(LinkedInProfileSnapshot)
            .where(LinkedInProfileSnapshot.matched_contact_id == Contact.id)
            .correlate_except(LinkedInProfileSnapshot)
        )
        if filters.source == "capture":
            leg = leg.where(from_capture)
        elif filters.source == "import":
            leg = leg.where(~from_capture)
    return leg


def _current_experience_col(attribute: str) -> Any:
    """The current role's ``attribute`` for the snapshot in the enclosing query.

    ``is_current`` is set by the extraction adapter; when more than one role
    carries it the lowest ``position_index`` wins, which is the same
    first-listed-role rule the QA policy uses. Correlated so it can be used
    inside the capture leg's SELECT and WHERE.
    """

    column = getattr(LinkedInProfileExperienceObservation, attribute)
    return (
        select(column)
        .where(
            LinkedInProfileExperienceObservation.snapshot_id == LinkedInProfileSnapshot.id,
            LinkedInProfileExperienceObservation.is_current.is_(True),
        )
        .order_by(LinkedInProfileExperienceObservation.position_index.asc())
        .limit(1)
        .correlate(LinkedInProfileSnapshot)
        .scalar_subquery()
    )


def _capture_leg(filters: CrmFilters) -> Select[Any] | None:
    """Pending captures, or None when the view excludes them entirely."""

    if filters.view == VIEW_SUPPRESSED:
        # Captures suppressed at intake carry outcome SUPPRESSED, which is not a
        # pending outcome; they are shown through the capture record, not here.
        return None
    if filters.identity == CaptureIdentityState.CANONICAL:
        return None
    if filters.has_email is True:
        return None  # a pending capture never has an address
    if filters.source == "import":
        return None  # every pending capture came from a capture

    wanted: tuple[LinkedInSnapshotOutcome, ...] = PENDING_OUTCOMES
    if filters.view == VIEW_AWAITING_COMPANY:
        wanted = (LinkedInSnapshotOutcome.UNMATCHED_STAGED,)
    elif filters.view == VIEW_AMBIGUOUS:
        wanted = (LinkedInSnapshotOutcome.AMBIGUOUS_REVIEW,)
    if filters.identity == CaptureIdentityState.AWAITING_COMPANY:
        wanted = tuple(set(wanted) & {LinkedInSnapshotOutcome.UNMATCHED_STAGED})
    elif filters.identity == CaptureIdentityState.AMBIGUOUS_IDENTITY:
        wanted = tuple(set(wanted) & {LinkedInSnapshotOutcome.AMBIGUOUS_REVIEW})
    elif filters.identity == CaptureIdentityState.REJECTED:
        return None
    if not wanted:
        return None

    # The captured person's identity fields live in the JSONB projection the
    # intake path writes; ->> yields text, which is what ORDER BY and LIKE need.
    fields = LinkedInProfileSnapshot.profile_fields
    full_name = func.lower(func.coalesce(fields["full_name"].astext, literal("")))
    # Current employment is NOT in that projection — it is decided from the
    # experience observations, where the first row flagged is_current wins. A
    # correlated scalar subquery keeps that determination in one place instead
    # of denormalising it onto the snapshot.
    company = func.lower(func.coalesce(_current_experience_col("company_name"), literal("")))

    leg = select(
        literal("pending_capture").label("kind"),
        LinkedInProfileSnapshot.id.label("record_id"),
        full_name.label("sort_name"),
        company.label("sort_company"),
        LinkedInProfileSnapshot.ingested_at.label("last_updated"),
    ).where(
        LinkedInProfileSnapshot.outcome.in_(wanted),
        LinkedInProfileSnapshot.matched_contact_id.is_(None),
        LinkedInProfileSnapshot.duplicate_of_id.is_(None),
    )

    if filters.search:
        needle = f"%{filters.search.lower()}%"
        leg = leg.where(
            or_(
                full_name.like(needle),
                company.like(needle),
                func.lower(func.coalesce(fields["headline"].astext, literal(""))).like(needle),
                func.lower(
                    func.coalesce(LinkedInProfileSnapshot.normalized_profile_url, literal(""))
                ).like(needle),
            )
        )
    if filters.company:
        leg = leg.where(company.like(f"%{filters.company.lower()}%"))
    if filters.label_slug:
        leg = leg.where(
            _label_predicate(LinkedInProfileSnapshot.id, filters.label_slug, capture_anchor=True)
        )
    if filters.has_linkedin is True:
        leg = leg.where(LinkedInProfileSnapshot.normalized_profile_url.is_not(None))
    elif filters.has_linkedin is False:
        leg = leg.where(LinkedInProfileSnapshot.normalized_profile_url.is_(None))
    if filters.has_email is False:
        pass  # every pending capture qualifies
    return leg


def list_crm_rows(
    session: Session,
    *,
    filters: CrmFilters | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[CrmRow], int]:
    """One page of the CRM list, plus the total matching count.

    Takes no campaign. Returns canonical contacts and pending captures together,
    ordered and paged as a single result set.
    """

    active = (filters or CrmFilters()).normalized()
    limit = max(1, min(limit, MAX_PAGE_SIZE))
    offset = max(0, offset)

    legs = [leg for leg in (_contact_leg(active), _capture_leg(active)) if leg is not None]
    if not legs:
        return [], 0

    unified = legs[0] if len(legs) == 1 else union_all(*legs)
    sub = unified.subquery("crm")

    total = session.scalar(select(func.count()).select_from(sub)) or 0
    if not total:
        return [], 0

    order: tuple[Any, ...]
    if active.sort == SORT_NAME:
        order = (sub.c.sort_name.asc(), sub.c.record_id.asc())
    elif active.sort == SORT_COMPANY:
        order = (sub.c.sort_company.asc(), sub.c.sort_name.asc(), sub.c.record_id.asc())
    else:
        order = (sub.c.last_updated.desc(), sub.c.record_id.asc())

    page = session.execute(
        select(sub.c.kind, sub.c.record_id).order_by(*order).limit(limit).offset(offset)
    ).all()

    return _hydrate(session, page), total


def _hydrate(session: Session, page: Sequence[Any]) -> list[CrmRow]:
    """Turn (kind, id) pairs back into full rows, preserving page order.

    Two queries regardless of page size, then a dict lookup — the ordering from
    the union is authoritative and is not recomputed here.
    """

    contact_ids = [rid for kind, rid in page if kind == "contact"]
    capture_ids = [rid for kind, rid in page if kind == "pending_capture"]

    contacts = {
        c.id: c
        for c in (
            session.scalars(select(Contact).where(Contact.id.in_(contact_ids))).all()
            if contact_ids
            else []
        )
    }
    captures = {
        s.id: s
        for s in (
            session.scalars(
                select(LinkedInProfileSnapshot).where(LinkedInProfileSnapshot.id.in_(capture_ids))
            ).all()
            if capture_ids
            else []
        )
    }
    labels = _labels_for(session, contact_ids=contact_ids, capture_ids=capture_ids)

    rows: list[CrmRow] = []
    for kind, record_id in page:
        if kind == "contact":
            contact = contacts.get(record_id)
            if contact is not None:
                rows.append(_contact_row(session, contact, labels.get(("contact", record_id), [])))
        else:
            snapshot = captures.get(record_id)
            if snapshot is not None:
                rows.append(_capture_row(session, snapshot, labels.get(("capture", record_id), [])))
    return rows


def _labels_for(
    session: Session,
    *,
    contact_ids: list[uuid.UUID],
    capture_ids: list[uuid.UUID],
) -> dict[tuple[str, uuid.UUID], list[str]]:
    """Every label name on this page, in one query, keyed by anchor."""

    out: dict[tuple[str, uuid.UUID], list[str]] = {}
    if not contact_ids and not capture_ids:
        return out

    clauses: list[Any] = []
    if contact_ids:
        clauses.append(ContactLabelAssignment.contact_id.in_(contact_ids))
    if capture_ids:
        clauses.append(
            and_(
                ContactLabelAssignment.contact_id.is_(None),
                ContactLabelAssignment.capture_id.in_(capture_ids),
            )
        )
    rows = session.execute(
        select(
            ContactLabelAssignment.contact_id,
            ContactLabelAssignment.capture_id,
            ContactLabel.name,
        )
        .join(ContactLabel, ContactLabel.id == ContactLabelAssignment.label_id)
        .where(or_(*clauses))
        .order_by(ContactLabel.name.asc())
    ).all()
    for contact_id, capture_id, name in rows:
        key = ("contact", contact_id) if contact_id is not None else ("capture", capture_id)
        out.setdefault(key, []).append(name)
    return out


def _contact_row(session: Session, contact: Contact, labels: list[str]) -> CrmRow:
    states = _states_for_contact(session, contact)
    return CrmRow(
        kind="contact",
        record_id=contact.id,
        full_name=f"{contact.first_name} {contact.last_name}".strip(),
        title=contact.title,
        company_name=contact.company_name,
        company_domain=contact.company_domain,
        location=contact.country,
        linkedin_url=contact.linkedin_url,
        email=contact.email,
        source="capture" if _has_capture(session, contact.id) else "import",
        states=states,
        labels=labels,
        warnings=[],
        last_updated=contact.updated_at,
    )


def _capture_row(session: Session, snapshot: LinkedInProfileSnapshot, labels: list[str]) -> CrmRow:
    fields: dict[str, Any] = snapshot.profile_fields or {}
    warnings = [str(w) for w in (snapshot.page_warnings or [])]
    if snapshot.outcome == LinkedInSnapshotOutcome.UNMATCHED_STAGED:
        warnings.insert(0, "Awaiting company-domain resolution — not yet a canonical contact.")
    elif snapshot.outcome == LinkedInSnapshotOutcome.AMBIGUOUS_REVIEW:
        warnings.insert(0, "Matched more than one existing contact — needs an identity decision.")
    current = current_experience(snapshot)
    return CrmRow(
        kind="pending_capture",
        record_id=snapshot.id,
        full_name=str(fields.get("full_name") or "(name not captured)"),
        title=(current.job_title if current else None) or fields.get("headline"),
        company_name=current.company_name if current else None,
        company_domain=None,  # by definition — that is why it is pending
        location=fields.get("displayed_location"),
        linkedin_url=snapshot.normalized_profile_url,
        email=None,
        source="capture",
        states=states_for_capture(session, snapshot),
        labels=labels,
        warnings=warnings,
        last_updated=snapshot.ingested_at,
    )


def current_experience(
    snapshot: LinkedInProfileSnapshot,
) -> LinkedInProfileExperienceObservation | None:
    """The observation representing this person's current role, if any.

    Same rule as ``_current_experience_col`` so the list and the detail page can
    never disagree: the first ``is_current`` observation by position index. A
    capture with no current role returns None rather than guessing from the most
    recent dated entry — the dates are frequently unreliable.
    """

    current = [e for e in snapshot.experiences if e.is_current]
    if not current:
        return None
    return sorted(current, key=lambda e: e.position_index)[0]


def _has_capture(session: Session, contact_id: uuid.UUID) -> bool:
    return bool(
        session.scalar(
            select(literal(1))
            .select_from(LinkedInProfileSnapshot)
            .where(LinkedInProfileSnapshot.matched_contact_id == contact_id)
            .limit(1)
        )
    )
