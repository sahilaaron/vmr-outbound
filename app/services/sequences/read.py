"""Bounded read models for sequences.

The performance rule here is one sentence long: **no list page loads a message
body**. A Review queue showing forty contacts must not fetch two hundred and
eighty email bodies to render forty cards, and it must not issue forty queries
to count approvals.

So the queue read model does three statements regardless of page size -- the
sequences, the position-1 subjects, and the per-sequence decision tallies --
and joins them in Python by id. That is the convention the rest of this
codebase already uses (see ``app.services.drafts._current_version_numbers``),
and it is used here for the same reason: "is this approved" is a question about
the set, and asking it row by row would issue a query per row.

Bodies are fetched exactly when one message is expanded, by
:func:`message_detail`, which loads one message.

None of these functions writes, audits, commits or spends. A read model that
had a side effect would make a page load an action.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import Select, func, select
from sqlalchemy.orm import Session

from app.models.campaign import Campaign, CampaignContact
from app.models.company import Company
from app.models.contact import Contact
from app.models.email_sequence import (
    SEQUENCE_LENGTH,
    EmailSequence,
    EmailSequenceMessage,
    EmailSequenceMessageReview,
    EmailSequenceMessageVersion,
)
from app.models.enums import (
    SequenceGenerationStatus,
    SequenceMessageOrigin,
    SequenceMessagePurpose,
    SequenceMessageType,
    SequenceReviewDecision,
    SequenceReviewState,
    SequenceStopState,
    SequenceValidationStatus,
)

# `review` imports nothing from this module, so this direction is safe. The
# dependency is deliberate: the card chip and the card counts must come from one
# derivation, and that derivation lives with the review rules.
from app.services.sequences import review as sequence_review

#: How much of the initial message a collapsed card shows.
EXCERPT_CHARS = 180

VIEW_ALL = "all"
VIEW_EDITED = "edited"
VIEW_REVIEWED = "reviewed"
VIEW_DISCARDED = "discarded"

#: The Review filters, in the order they render. ``all`` leads because it is the
#: only one that is never empty for a working campaign.
#:
#: There used to be a "Waiting for you" filter here, and it was the default. It
#: was the visible half of a mandatory approval queue: every generated sequence
#: landed in it and stayed there until an operator cleared it. Under default
#: approval nothing waits, so that filter would be permanently empty and the
#: default landing page would show nothing at all. Every filter below names
#: something a sequence can actually be.
VIEWS: tuple[tuple[str, str], ...] = (
    (VIEW_ALL, "All sequences"),
    (VIEW_EDITED, "You changed these"),
    (VIEW_REVIEWED, "You reviewed these"),
    (VIEW_DISCARDED, "Contains a discard"),
)

#: Every state a live, non-failed sequence can hold. Used as the SQL pre-filter
#: for the narrowing views so that a drifted cache can only cost a row's worth
#: of work, never hide it from the right view.
_ACTIVE_STATES = (
    SequenceReviewState.APPROVED,
    SequenceReviewState.CONTAINS_DISCARDED,
    SequenceReviewState.PARTIALLY_APPROVED,
    # Kept in the superset so a row written before default approval, whose
    # cached column still reads one of these, cannot be filtered out of a view
    # its derived state belongs in.
    SequenceReviewState.NEEDS_REVIEW,
    SequenceReviewState.GENERATED,
    SequenceReviewState.PARTIALLY_REVIEWED,
    SequenceReviewState.CONTAINS_EDITS,
)

#: Customer-facing wording for each purpose. The enum value is a stable
#: identifier; this is what a person reads.
PURPOSE_LABELS: dict[SequenceMessagePurpose, str] = {
    SequenceMessagePurpose.INITIAL_OUTREACH: "Initial outreach",
    SequenceMessagePurpose.CONCISE_REMINDER: "Concise reminder",
    SequenceMessagePurpose.NEW_ANGLE: "A different angle",
    SequenceMessagePurpose.ROLE_RELEVANCE: "Relevance to their role",
    SequenceMessagePurpose.PROOF_OR_OUTCOME: "Proof or outcome",
    SequenceMessagePurpose.LOW_FRICTION_RESOURCE: "A low-friction offer",
    SequenceMessagePurpose.CLOSE_THE_LOOP: "Closing the loop",
}

#: Short labels for the message selector: Initial | F1 | F2 ...
STEP_LABELS: dict[int, str] = {1: "Initial", 2: "F1", 3: "F2", 4: "F3", 5: "F4", 6: "F5", 7: "F6"}


@dataclass(frozen=True)
class SequenceCardRow:
    """One collapsed sequence card. Carries no message body but the excerpt."""

    sequence_id: uuid.UUID
    sequence_key: uuid.UUID
    sequence_version: int
    campaign_contact_id: uuid.UUID
    campaign_id: uuid.UUID
    campaign_name: str
    contact_id: uuid.UUID
    contact_name: str
    contact_title: str | None
    company_name: str | None
    email: str | None
    initial_subject: str
    initial_excerpt: str
    #: Derived from the same tally the counts below come from. This is what the
    #: card renders. It is never read from the cached column.
    review_state: SequenceReviewState
    #: What the cached ``email_sequences.review_state`` column says. Kept only so
    #: drift is observable; never rendered as the card's state.
    cached_review_state: SequenceReviewState
    validation_status: SequenceValidationStatus
    message_count: int
    #: Approved by default plus approved by a person. What "ready" means.
    approved: int
    #: The subset of ``approved`` a person actually decided. Rendered
    #: separately, always: a card that showed only ``approved`` would let a
    #: generated default read as somebody's judgement.
    human_approved: int
    discarded: int
    edited: int
    #: Current versions no human has ruled on. Not a backlog -- these are
    #: approved. The number answers "has anyone looked at this", nothing else.
    unreviewed: int
    warning_count: int
    strategy_id: str | None
    policy_version_number: int | None
    fallback_identifier: str | None
    intelligence_status: str | None
    planned_span_days: int | None
    created_at: datetime

    @property
    def step_total(self) -> int:
        return SEQUENCE_LENGTH

    @property
    def complete(self) -> bool:
        return self.message_count == SEQUENCE_LENGTH

    @property
    def reviewed_by_human(self) -> bool:
        """Whether a person has ruled on any message in this sequence."""

        return (self.human_approved + self.discarded) > 0

    @property
    def review_summary(self) -> str:
        """One phrase that never lets a default read as a decision."""

        if not self.reviewed_by_human:
            return "approved by default — nobody has reviewed it"
        parts = []
        if self.human_approved:
            parts.append(f"{self.human_approved} approved by you")
        if self.discarded:
            parts.append(f"{self.discarded} discarded")
        if self.unreviewed:
            parts.append(f"{self.unreviewed} approved by default")
        return ", ".join(parts)

    @property
    def cache_is_stale(self) -> bool:
        """Whether the stored filter column disagrees with the derived truth.

        Surfaced rather than silently corrected: a read model that quietly
        rewrote the cache would hide the fact that something wrote a decision
        without refreshing it.
        """

        return self.cached_review_state is not self.review_state

    @property
    def evidence_label(self) -> str:
        """Plain language for what the sequence was built from."""

        if self.fallback_identifier == "offering_led":
            return "Offering-led fallback — no prospect context cleared policy"
        if self.fallback_identifier:
            return self.fallback_identifier.replace("_", " ").capitalize()
        return "Context basis not recorded"


@dataclass(frozen=True)
class SequenceQueue:
    rows: tuple[SequenceCardRow, ...]
    total: int
    view: str
    campaign_id: uuid.UUID | None


@dataclass(frozen=True)
class MessageRow:
    """One row in the Contact-page sequence table. Still no body."""

    message_id: uuid.UUID
    version_id: uuid.UUID
    position: int
    message_type: SequenceMessageType
    purpose: SequenceMessagePurpose
    subject: str
    message_version: int
    origin: SequenceMessageOrigin
    validation_status: SequenceValidationStatus
    decision: SequenceReviewDecision | None
    decided_at: datetime | None
    decided_by: str | None
    recommended_delay_days: int
    recommended_elapsed_day: int
    predecessor_message_id: uuid.UUID | None
    #: The exact version this one was edited from, when it was. Lets a caller
    #: name the text that was replaced, not merely show it.
    source_version_id: uuid.UUID | None
    warning_count: int

    @property
    def step_label(self) -> str:
        return STEP_LABELS.get(self.position, f"Step {self.position}")

    @property
    def purpose_label(self) -> str:
        return PURPOSE_LABELS.get(self.purpose, self.purpose.value.replace("_", " "))

    @property
    def human_reviewed(self) -> bool:
        """Whether a person ruled on this exact version."""

        return self.decision is not None

    @property
    def approved(self) -> bool:
        """Approved by default or by a person. False for a standing discard."""

        return self.decision is None or self.decision is SequenceReviewDecision.APPROVED

    @property
    def decision_origin(self) -> str:
        """Who the standing state came from: ``"default"`` or ``"human"``.

        The field the UI needs in order to never print the bare word
        "approved". A generated default and an operator's approval are the same
        readiness and different facts, and only this distinguishes them.
        """

        return "human" if self.human_reviewed else "default"

    @property
    def review_label(self) -> str:
        if self.decision is SequenceReviewDecision.APPROVED:
            return "approved by you"
        if self.decision is SequenceReviewDecision.DISCARDED:
            return "discarded"
        if self.decision is SequenceReviewDecision.INVALIDATED:
            return "approval invalidated by an edit"
        return "approved by default"

    @property
    def edit_label(self) -> str:
        return {
            SequenceMessageOrigin.GENERATED: "generated",
            SequenceMessageOrigin.HUMAN_EDITED: "human-edited",
            SequenceMessageOrigin.REGENERATED: "regenerated",
        }[self.origin]

    @property
    def timing_label(self) -> str:
        if self.position == 1:
            return "Day 0 — first message"
        return f"Day {self.recommended_elapsed_day} — {self.recommended_delay_days} days later"


@dataclass(frozen=True)
class MessageDetail:
    """One expanded message. This is the only read model that carries a body."""

    row: MessageRow
    body: str
    original_subject: str | None
    original_body: str | None
    warnings: tuple[str, ...]
    context_used: dict[str, Any]
    evidence_insight_ids: tuple[str, ...]
    context_decision: dict[str, Any]
    intelligence_accepted_count: int
    intelligence_excluded_count: int
    policy_version_id: uuid.UUID | None
    strategy_id: str | None
    created_by: str | None
    created_at: datetime

    @property
    def research_basis(self) -> str:
        decision = self.context_decision or {}
        used = decision.get("context_used") or []
        return ", ".join(str(item) for item in used) if used else "No prospect context was used."

    @property
    def insights_basis(self) -> str:
        if self.evidence_insight_ids:
            return f"{len(self.evidence_insight_ids)} supplied insight(s) cited."
        return "No insight was cited."

    @property
    def intelligence_basis(self) -> str:
        decision = self.context_decision or {}
        block = decision.get("company_intelligence")
        if not isinstance(block, dict):
            return "Company Intelligence lineage was not recorded."
        status = block.get("status") or "unknown"
        version = block.get("version_number")
        used = "used as context" if block.get("used") else "not used"
        version_text = f" version v{version}," if version else ""
        return (
            f"Company Intelligence{version_text} {used} ({status}); "
            f"{self.intelligence_accepted_count} accepted, "
            f"{self.intelligence_excluded_count} excluded. "
            "Classifications orient tone; they are never cited as proof."
        )


@dataclass(frozen=True)
class SequenceSummary:
    """The block shown above the Contact-page table."""

    sequence_id: uuid.UUID
    sequence_key: uuid.UUID
    sequence_version: int
    campaign_id: uuid.UUID
    #: Carried so a caller can navigate from a sequence back to the person and
    #: the membership it belongs to without a second query.
    campaign_contact_id: uuid.UUID
    contact_id: uuid.UUID
    review_state: SequenceReviewState
    validation_status: SequenceValidationStatus
    generation_status: str
    message_count: int
    approved: int
    human_approved: int
    discarded: int
    edited: int
    unreviewed: int
    planned_span_days: int | None
    cadence_source: str
    created_at: datetime
    superseded_at: datetime | None
    strategy_id: str | None
    policy_version_number: int | None
    warning_count: int
    stop_state: str
    stop_reason: str | None
    current_actionable_position: int | None
    #: When a person last ruled on any message here, if one ever has.
    last_human_decision_at: datetime | None

    @property
    def complete(self) -> bool:
        return self.message_count == SEQUENCE_LENGTH

    @property
    def reviewed_by_human(self) -> bool:
        return (self.human_approved + self.discarded) > 0


# ---------------------------------------------------------------------------
# Bounded aggregate helpers
# ---------------------------------------------------------------------------


def _initial_subjects(
    session: Session, *, sequence_keys: list[uuid.UUID]
) -> dict[uuid.UUID, tuple[str, str]]:
    """Position-1 subject and excerpt for every sequence on the page, in one query.

    The excerpt is cut in SQL rather than by loading the body and slicing it in
    Python, so a page of forty cards transfers a few kilobytes instead of a few
    hundred.
    """

    if not sequence_keys:
        return {}
    rows = session.execute(
        select(
            EmailSequenceMessage.sequence_key,
            EmailSequenceMessageVersion.subject,
            func.left(EmailSequenceMessageVersion.body, EXCERPT_CHARS),
        )
        .join(
            EmailSequenceMessageVersion,
            EmailSequenceMessageVersion.message_id == EmailSequenceMessage.id,
        )
        .where(
            EmailSequenceMessage.sequence_key.in_(sequence_keys),
            EmailSequenceMessage.position == 1,
            EmailSequenceMessageVersion.superseded_at.is_(None),
        )
    ).all()
    return {key: (subject, excerpt) for key, subject, excerpt in rows}


def _tallies(
    session: Session, *, sequence_keys: list[uuid.UUID]
) -> dict[uuid.UUID, dict[str, int]]:
    """Every per-sequence count the queue needs, in one query.

    Under default approval ``approved`` is no longer "rows saying APPROVED": a
    current version with no review row is approved too. Both are counted here,
    in the same statement, so a page of forty cards still costs one query and
    the card can show readiness and human involvement as two separate numbers
    rather than one ambiguous one.
    """

    if not sequence_keys:
        return {}
    decision = EmailSequenceMessageReview.decision
    version_id = EmailSequenceMessageVersion.id
    rows = session.execute(
        select(
            EmailSequenceMessage.sequence_key,
            func.count(version_id),
            # Approved: no decision at all, or an explicit human approval.
            # `INVALIDATED` is deliberately excluded -- a withdrawn approval is
            # not an approval.
            func.count(version_id).filter(
                (decision.is_(None)) | (decision == SequenceReviewDecision.APPROVED)
            ),
            func.count(decision).filter(decision == SequenceReviewDecision.APPROVED),
            func.count(decision).filter(decision == SequenceReviewDecision.DISCARDED),
            func.count(version_id).filter(decision.is_(None)),
            func.count(version_id).filter(
                EmailSequenceMessageVersion.origin == SequenceMessageOrigin.HUMAN_EDITED
            ),
            func.coalesce(
                func.sum(func.jsonb_array_length(EmailSequenceMessageVersion.warnings)), 0
            ),
            func.max(EmailSequenceMessageReview.decided_at),
        )
        .join(
            EmailSequenceMessageVersion,
            EmailSequenceMessageVersion.message_id == EmailSequenceMessage.id,
        )
        .outerjoin(
            EmailSequenceMessageReview,
            EmailSequenceMessageReview.message_version_id == EmailSequenceMessageVersion.id,
        )
        .where(
            EmailSequenceMessage.sequence_key.in_(sequence_keys),
            EmailSequenceMessageVersion.superseded_at.is_(None),
        )
        .group_by(EmailSequenceMessage.sequence_key)
    ).all()
    return {
        key: {
            "total": int(total),
            "approved": int(approved),
            "human_approved": int(human_approved),
            "discarded": int(discarded),
            "unreviewed": int(unreviewed),
            "edited": int(edited),
            "warnings": int(warnings or 0),
            "last_decision_at": last_decision_at,
        }
        for (
            key,
            total,
            approved,
            human_approved,
            discarded,
            unreviewed,
            edited,
            warnings,
            last_decision_at,
        ) in rows
    }


#: The shape ``_tallies`` returns for a sequence that has no message rows at
#: all. Named so the two call sites cannot drift apart.
EMPTY_TALLY: dict[str, Any] = {
    "total": 0,
    "approved": 0,
    "human_approved": 0,
    "discarded": 0,
    "unreviewed": 0,
    "edited": 0,
    "warnings": 0,
    "last_decision_at": None,
}


def _decision_block(sequence: EmailSequence) -> dict[str, Any]:
    raw = sequence.personalization_decision
    return raw if isinstance(raw, dict) else {}


def _intelligence_status(sequence: EmailSequence) -> str | None:
    block = _decision_block(sequence).get("company_intelligence")
    if isinstance(block, dict):
        status = block.get("status")
        return str(status) if status else None
    return None


def _derived_state(sequence: EmailSequence, tally: dict[str, Any]) -> SequenceReviewState:
    """The card's state, derived from the tally rather than read from the cache."""

    return sequence_review.derive_state(
        superseded=sequence.superseded_at is not None,
        generation_complete=(sequence.generation_status is SequenceGenerationStatus.COMPLETE),
        validation_failed=sequence.validation_status is SequenceValidationStatus.FAILED,
        stopped=sequence.stop_state is SequenceStopState.STOPPED,
        total=tally["total"],
        approved=tally["approved"],
        discarded=tally["discarded"],
    )


def _card_row(
    sequence: EmailSequence,
    *,
    contact: Contact,
    campaign: Campaign,
    company: Company | None,
    subject: str,
    excerpt: str,
    tally: dict[str, Any],
    derived: SequenceReviewState,
) -> SequenceCardRow:
    """Build one card. Shared by the queue and by the single-sequence lookup.

    Extracted so a card fetched by id is byte-for-byte the same shape as one
    that arrived through a filter -- see :func:`card_for_sequence` for why a
    sequence has to be reachable by id at all.
    """

    decision = _decision_block(sequence)
    return SequenceCardRow(
        sequence_id=sequence.id,
        sequence_key=sequence.sequence_key,
        sequence_version=sequence.sequence_version,
        campaign_contact_id=sequence.campaign_contact_id,
        campaign_id=sequence.campaign_id,
        campaign_name=campaign.name,
        contact_id=sequence.contact_id,
        contact_name=_contact_name(contact),
        contact_title=contact.title,
        company_name=company.name if company is not None else None,
        email=contact.email,
        initial_subject=subject or "(no subject recorded)",
        initial_excerpt=excerpt or "",
        review_state=derived,
        cached_review_state=sequence.review_state,
        validation_status=sequence.validation_status,
        message_count=tally["total"],
        approved=tally["approved"],
        human_approved=tally["human_approved"],
        discarded=tally["discarded"],
        edited=tally["edited"],
        unreviewed=tally["unreviewed"],
        warning_count=tally["warnings"],
        strategy_id=sequence.personalization_strategy_id,
        policy_version_number=sequence.personalization_policy_version_number,
        fallback_identifier=(
            str(decision.get("fallback_identifier"))
            if decision.get("fallback_identifier")
            else None
        ),
        intelligence_status=_intelligence_status(sequence),
        planned_span_days=sequence.planned_span_days,
        created_at=sequence.created_at,
    )


def card_for_sequence(session: Session, sequence_id: uuid.UUID) -> SequenceCardRow | None:
    """One card, fetched by id, ignoring every filter.

    The Review page used to resolve an expanded sequence by scanning the rows
    the current filter had returned. That coupled *reading* a sequence to
    *matching* a filter: pick a filter the sequence is not in, and following a
    link to it rendered a page with no messages on it and no explanation. The
    filter narrows the list; it must not decide whether a named sequence can be
    opened.
    """

    record = session.execute(
        select(EmailSequence, Contact, Campaign, Company)
        .join(Contact, Contact.id == EmailSequence.contact_id)
        .join(Campaign, Campaign.id == EmailSequence.campaign_id)
        .outerjoin(Company, Company.id == EmailSequence.company_id)
        .where(EmailSequence.id == sequence_id)
        .limit(1)
    ).first()
    if record is None:
        return None
    sequence, contact, campaign, company = record
    subject, excerpt = _initial_subjects(session, sequence_keys=[sequence.sequence_key]).get(
        sequence.sequence_key, ("", "")
    )
    tally = _tallies(session, sequence_keys=[sequence.sequence_key]).get(
        sequence.sequence_key, dict(EMPTY_TALLY)
    )
    return _card_row(
        sequence,
        contact=contact,
        campaign=campaign,
        company=company,
        subject=subject,
        excerpt=excerpt,
        tally=tally,
        derived=_derived_state(sequence, tally),
    )


def _queue_statement(*, campaign_id: uuid.UUID | None, view: str) -> Select[Any]:
    statement = (
        select(EmailSequence, Contact, Campaign, Company)
        .join(Contact, Contact.id == EmailSequence.contact_id)
        .join(Campaign, Campaign.id == EmailSequence.campaign_id)
        .outerjoin(Company, Company.id == EmailSequence.company_id)
        .where(EmailSequence.superseded_at.is_(None))
    )
    if campaign_id is not None:
        statement = statement.where(EmailSequence.campaign_id == campaign_id)
    if view != VIEW_ALL:
        # A *superset*, deliberately. The stored column is a cache, and a cache
        # that has drifted must not be able to hide a sequence from the view it
        # actually belongs in -- filtering narrowly here would let a stale value
        # keep a sequence out of a view entirely, where no Python reconciliation
        # could put it back. The narrowing views therefore select every
        # non-terminal state and the caller narrows on the derived truth.
        #
        # ``all`` applies no state filter at all, so a failed or blocked
        # sequence is still visible somewhere rather than vanishing.
        statement = statement.where(EmailSequence.review_state.in_(_ACTIVE_STATES))
    return statement


def list_queue(
    session: Session,
    *,
    campaign_id: uuid.UUID | None = None,
    view: str = VIEW_ALL,
    limit: int = 50,
    offset: int = 0,
) -> SequenceQueue:
    """One compact card per Campaign Contact with a live sequence.

    The default view is ``all``. It used to be an approval backlog, which under
    default approval would be permanently empty -- so the page an operator lands
    on would show nothing while seven perfectly readable messages sat behind it.
    """

    if view not in {key for key, _label in VIEWS}:
        view = VIEW_ALL
    statement = _queue_statement(campaign_id=campaign_id, view=view)
    records = session.execute(
        statement.order_by(EmailSequence.created_at.desc()).limit(limit).offset(offset)
    ).all()

    keys = [sequence.sequence_key for sequence, _c, _cam, _co in records]
    subjects = _initial_subjects(session, sequence_keys=keys)
    tallies = _tallies(session, sequence_keys=keys)

    rows: list[SequenceCardRow] = []
    for sequence, contact, campaign, company in records:
        subject, excerpt = subjects.get(sequence.sequence_key, ("", ""))
        tally = tallies.get(sequence.sequence_key, dict(EMPTY_TALLY))
        derived = _derived_state(sequence, tally)
        # The SQL filter runs against the cached column because that is what an
        # index can serve. If the cache has drifted, the derived state is
        # authoritative and the row is dropped from a view it does not belong
        # in -- so a stale cache can slow a query down but can never put a
        # sequence in a view it does not belong to.
        #
        # Every narrowing filter below is a fact about the *messages*, not about
        # a workflow stage, so none of them can become permanently empty the way
        # an approval backlog did.
        if view == VIEW_DISCARDED and derived is not SequenceReviewState.CONTAINS_DISCARDED:
            continue
        if view == VIEW_EDITED and not tally["edited"]:
            continue
        if view == VIEW_REVIEWED and not (tally["human_approved"] + tally["discarded"]):
            continue
        rows.append(
            _card_row(
                sequence,
                contact=contact,
                campaign=campaign,
                company=company,
                subject=subject,
                excerpt=excerpt,
                tally=tally,
                derived=derived,
            )
        )
    # ``total`` counts what this window actually holds after narrowing, not a
    # global total. The distinction matters because the SQL pre-filter is a
    # superset: a global count taken from it would over-report. Nothing renders
    # this figure today, and the honest window count is the one a caller can act
    # on without being misled.
    return SequenceQueue(rows=tuple(rows), total=len(rows), view=view, campaign_id=campaign_id)


def _contact_name(contact: Contact) -> str:
    parts = [contact.first_name or "", contact.last_name or ""]
    name = " ".join(part for part in parts if part).strip()
    return name or contact.email or "(unnamed contact)"


def reviewed_count(session: Session, *, campaign_id: uuid.UUID | None = None) -> int:
    """How many live sequences a person has ruled on, by derived truth.

    This replaces ``awaiting_count``, which counted a backlog. Under default
    approval that number is structurally zero, and a badge showing a permanent
    zero is worse than no badge: it invites the reading that there is nothing
    to look at, when in fact everything is there and readable.

    Goes through ``list_queue`` rather than counting the cached column, for the
    same reason the queue narrows in Python: a count taken from a cache that has
    drifted is a number nobody can act on.
    """

    return len(list_queue(session, campaign_id=campaign_id, view=VIEW_REVIEWED, limit=500).rows)


def any_sequence_exists(session: Session, *, campaign_id: uuid.UUID | None = None) -> bool:
    """Whether any live sequence exists at all, for section visibility.

    One bounded existence query. It is what lets the Review page keep showing
    the sequence section -- and its filters -- after the deployment switch is
    turned off, instead of hiding recorded work behind a view that happens to be
    empty.
    """

    statement = select(EmailSequence.id).where(EmailSequence.superseded_at.is_(None)).limit(1)
    if campaign_id is not None:
        statement = statement.where(EmailSequence.campaign_id == campaign_id)
    return session.scalar(statement) is not None


def get_sequence(session: Session, sequence_id: uuid.UUID) -> EmailSequence | None:
    return session.get(EmailSequence, sequence_id)


def sequence_for_membership(
    session: Session, *, campaign_contact_id: uuid.UUID
) -> EmailSequence | None:
    return session.scalars(
        select(EmailSequence)
        .where(
            EmailSequence.campaign_contact_id == campaign_contact_id,
            EmailSequence.superseded_at.is_(None),
        )
        .limit(1)
    ).first()


def _current_versions_statement(sequence: EmailSequence) -> Select[Any]:
    """Every live message/version/decision triple for one sequence.

    Shared by the three readers below so "current" means exactly one thing --
    ``superseded_at IS NULL`` -- in every one of them. When that predicate was
    written out per function, a reader that forgot it would silently show an
    edited-away version as current.
    """

    return (
        select(EmailSequenceMessage, EmailSequenceMessageVersion, EmailSequenceMessageReview)
        .join(
            EmailSequenceMessageVersion,
            EmailSequenceMessageVersion.message_id == EmailSequenceMessage.id,
        )
        .outerjoin(
            EmailSequenceMessageReview,
            EmailSequenceMessageReview.message_version_id == EmailSequenceMessageVersion.id,
        )
        .where(
            EmailSequenceMessage.sequence_key == sequence.sequence_key,
            EmailSequenceMessageVersion.superseded_at.is_(None),
        )
    )


def _build_row(
    message: EmailSequenceMessage,
    version: EmailSequenceMessageVersion,
    review: EmailSequenceMessageReview | None,
) -> MessageRow:
    return MessageRow(
        message_id=message.id,
        version_id=version.id,
        position=message.position,
        message_type=message.message_type,
        purpose=message.purpose,
        subject=version.subject,
        message_version=version.message_version,
        origin=version.origin,
        validation_status=version.validation_status,
        decision=review.decision if review is not None else None,
        decided_at=review.decided_at if review is not None else None,
        decided_by=review.decided_by if review is not None else None,
        recommended_delay_days=version.recommended_delay_days,
        recommended_elapsed_day=version.recommended_elapsed_day,
        predecessor_message_id=message.predecessor_message_id,
        source_version_id=version.source_version_id,
        warning_count=len(version.warnings or []),
    )


def _build_detail(
    message: EmailSequenceMessage,
    version: EmailSequenceMessageVersion,
    review: EmailSequenceMessageReview | None,
) -> MessageDetail:
    return MessageDetail(
        row=_build_row(message, version, review),
        body=version.body,
        original_subject=version.original_subject,
        original_body=version.original_body,
        warnings=tuple(version.warnings or []),
        context_used=version.context_used if isinstance(version.context_used, dict) else {},
        evidence_insight_ids=tuple(version.evidence_insight_ids or []),
        context_decision=(
            version.context_decision if isinstance(version.context_decision, dict) else {}
        ),
        intelligence_accepted_count=version.intelligence_accepted_count,
        intelligence_excluded_count=version.intelligence_excluded_count,
        policy_version_id=version.personalization_policy_version_id,
        strategy_id=version.personalization_strategy_id,
        created_by=version.created_by,
        created_at=version.created_at,
    )


@dataclass(frozen=True)
class SequenceRosterState:
    """What the Campaign roster says about one membership's sequence.

    Deliberately a statement of fact rather than a workflow state. "Partial" is
    not a queue an operator has to clear; it is what the row currently holds.
    """

    campaign_contact_id: uuid.UUID
    sequence_id: uuid.UUID
    message_count: int
    edited: int
    discarded: int

    @property
    def step_total(self) -> int:
        return SEQUENCE_LENGTH

    @property
    def complete(self) -> bool:
        return self.message_count == SEQUENCE_LENGTH

    @property
    def label(self) -> str:
        """The roster cell, in the vocabulary the brief fixed."""

        if self.complete:
            return f"{SEQUENCE_LENGTH} of {SEQUENCE_LENGTH}"
        return f"Partial — {self.message_count} of {SEQUENCE_LENGTH}"


#: What a roster row shows when no live sequence exists for the membership.
ROSTER_NO_SEQUENCE = "No sequence yet"


def roster_states(
    session: Session, *, campaign_id: uuid.UUID
) -> dict[uuid.UUID, SequenceRosterState]:
    """Sequence presence for every membership in one campaign, in one query.

    Keyed by ``campaign_contact_id`` because that is what the roster row already
    carries, so the page needs no extra lookup to join them.

    One statement for the whole roster, for the same reason the queue tallies
    are one statement: a page of a hundred memberships must not cost a hundred
    queries to say "7 of 7".
    """

    rows = session.execute(
        select(
            EmailSequence.campaign_contact_id,
            EmailSequence.id,
            func.count(EmailSequenceMessageVersion.id),
            func.count(EmailSequenceMessageVersion.id).filter(
                EmailSequenceMessageVersion.origin == SequenceMessageOrigin.HUMAN_EDITED
            ),
            func.count(EmailSequenceMessageReview.decision).filter(
                EmailSequenceMessageReview.decision == SequenceReviewDecision.DISCARDED
            ),
        )
        .join(
            EmailSequenceMessage,
            EmailSequenceMessage.sequence_key == EmailSequence.sequence_key,
        )
        .join(
            EmailSequenceMessageVersion,
            EmailSequenceMessageVersion.message_id == EmailSequenceMessage.id,
        )
        .outerjoin(
            EmailSequenceMessageReview,
            EmailSequenceMessageReview.message_version_id == EmailSequenceMessageVersion.id,
        )
        .where(
            EmailSequence.campaign_id == campaign_id,
            EmailSequence.superseded_at.is_(None),
            EmailSequenceMessageVersion.superseded_at.is_(None),
        )
        .group_by(EmailSequence.campaign_contact_id, EmailSequence.id)
    ).all()
    return {
        membership_id: SequenceRosterState(
            campaign_contact_id=membership_id,
            sequence_id=sequence_id,
            message_count=int(total),
            edited=int(edited),
            discarded=int(discarded),
        )
        for membership_id, sequence_id, total, edited, discarded in rows
    }


def message_rows(session: Session, *, sequence: EmailSequence) -> tuple[MessageRow, ...]:
    """All seven rows for the Contact-page table, in one query, without bodies."""

    records = session.execute(
        _current_versions_statement(sequence).order_by(EmailSequenceMessage.position)
    ).all()
    return tuple(_build_row(message, version, review) for message, version, review in records)


def message_detail(
    session: Session, *, sequence: EmailSequence, position: int
) -> MessageDetail | None:
    """One message, with its body. Used where a page expands exactly one."""

    record = session.execute(
        _current_versions_statement(sequence)
        .where(EmailSequenceMessage.position == position)
        .limit(1)
    ).first()
    if record is None:
        return None
    message, version, review = record
    return _build_detail(message, version, review)


def message_details(session: Session, *, sequence: EmailSequence) -> tuple[MessageDetail, ...]:
    """All seven messages *with* their bodies, in one query.

    The queue deliberately never does this -- forty cards would mean two hundred
    and eighty bodies. The Contact page is the opposite case: exactly one
    contact, whose seven messages an operator has come to read, copy and edit.
    Loading them one position at a time behind a ``?step=`` link made the
    operator page seven times to see one sequence, and made "copy the whole
    thing" impossible.

    One statement, ordered by position, so the cost is one query and seven
    bodies rather than seven queries.
    """

    records = session.execute(
        _current_versions_statement(sequence).order_by(EmailSequenceMessage.position)
    ).all()
    return tuple(_build_detail(message, version, review) for message, version, review in records)


def summary(session: Session, *, sequence: EmailSequence) -> SequenceSummary:
    tally = _tallies(session, sequence_keys=[sequence.sequence_key]).get(
        sequence.sequence_key, dict(EMPTY_TALLY)
    )
    derived = sequence_review.derive_state(
        superseded=sequence.superseded_at is not None,
        generation_complete=sequence.generation_status is SequenceGenerationStatus.COMPLETE,
        validation_failed=sequence.validation_status is SequenceValidationStatus.FAILED,
        stopped=sequence.stop_state is SequenceStopState.STOPPED,
        total=tally["total"],
        approved=tally["approved"],
        discarded=tally["discarded"],
    )
    return SequenceSummary(
        sequence_id=sequence.id,
        sequence_key=sequence.sequence_key,
        sequence_version=sequence.sequence_version,
        campaign_id=sequence.campaign_id,
        campaign_contact_id=sequence.campaign_contact_id,
        contact_id=sequence.contact_id,
        review_state=derived,
        validation_status=sequence.validation_status,
        generation_status=sequence.generation_status.value,
        message_count=tally["total"],
        approved=tally["approved"],
        human_approved=tally["human_approved"],
        discarded=tally["discarded"],
        edited=tally["edited"],
        unreviewed=tally["unreviewed"],
        planned_span_days=sequence.planned_span_days,
        cadence_source=sequence.cadence_source,
        created_at=sequence.created_at,
        superseded_at=sequence.superseded_at,
        strategy_id=sequence.personalization_strategy_id,
        policy_version_number=sequence.personalization_policy_version_number,
        warning_count=tally["warnings"],
        stop_state=sequence.stop_state.value,
        stop_reason=sequence.stop_reason.value if sequence.stop_reason else None,
        current_actionable_position=sequence.current_actionable_position,
        last_human_decision_at=tally["last_decision_at"],
    )


def history(
    session: Session, *, campaign_contact_id: uuid.UUID, limit: int = 10
) -> tuple[EmailSequence, ...]:
    """Every sequence version for one membership, newest first.

    Bounded: an Admin diagnosing a contact needs the recent versions, not every
    version ever generated.
    """

    return tuple(
        session.scalars(
            select(EmailSequence)
            .where(EmailSequence.campaign_contact_id == campaign_contact_id)
            .order_by(EmailSequence.sequence_version.desc())
            .limit(limit)
        ).all()
    )


def membership_for(session: Session, *, sequence: EmailSequence) -> CampaignContact | None:
    return session.get(CampaignContact, sequence.campaign_contact_id)
