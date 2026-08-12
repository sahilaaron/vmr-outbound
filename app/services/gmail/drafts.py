"""Creating Gmail drafts from one reviewed sequence, exactly once.

This module owns the parts that must not live in a route handler or in the
provider: authorization, the stale-version check, lineage, idempotency and
transaction semantics. The provider owns HTTP and nothing else.

The three properties this file exists to guarantee
--------------------------------------------------

**1. Draft exactly what the operator was looking at.** The caller submits the
set of message-version ids the page rendered. If that set is not exactly the set
of current versions stored right now, nothing is drafted and the operator is
told to reload. Silently fetching newer text would put a message the operator
never read into a stranger's mailbox with their approval implied.

**2. Clicking twice does not create two drafts.** The uniqueness constraint is
``(mailbox_account_subject, message_version_id)`` and it is a database fact, not
a check-then-act. The row is committed *before* the Gmail call rather than after
it, which is what makes the third property possible.

**3. An unresolved attempt is never treated as "no draft".** A timeout or a 5xx
proves nothing about whether Gmail acted, and neither does a reservation whose
outcome was never recorded -- the process may have died on either side of the
call, or another request may be inside that window right now. Both states
(``UNCONFIRMED`` and ``RESERVED``) are reconciled with one exact
``rfc822msgid:`` lookup before anything is decided, and if that lookup finds
nothing it still waits out ``RECONCILIATION_MIN_AGE_SECONDS`` first, because
Gmail's search index is not instantaneous and "not found" a second after the
write is not evidence.

What is deliberately *not* built here: no scheduler, no cadence execution, no
mailbox polling, no reply detection, no send, no generic synchronization
service. The reconciliation is one query about one draft VMR itself minted an
id for.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.gmail_config import GmailSettings
from app.models.campaign import CampaignContact
from app.models.contact import Contact
from app.models.email_sequence import EmailSequence, EmailSequenceMessageVersion
from app.models.enums import (
    GmailDraftStatus,
    SequenceGenerationStatus,
    SequenceStopState,
    SequenceValidationStatus,
)
from app.models.gmail import GmailDraftRecord, GmailMailboxGrant
from app.services.gmail import mailbox as gmail_mailbox
from app.services.gmail import mime
from app.services.gmail.oauth import GmailOAuthClient
from app.services.gmail.provider import GmailProvider, GmailProviderError
from app.services.sequences import review as sequence_review
from app.services.suppressions import evaluate_suppression

#: How old an unconfirmed attempt must be before a "no such draft" answer from
#: Gmail's search is trusted enough to re-create it. Gmail indexes a newly
#: created draft asynchronously, so an immediate second click can legitimately
#: find nothing that does in fact exist. Waiting is the difference between an
#: idempotency guarantee and a race.
RECONCILIATION_MIN_AGE_SECONDS = 60

#: The two statuses that mean "Gmail may or may not hold a draft for this exact
#: message version". Neither may be re-attempted without asking Gmail first.
_UNRESOLVED_STATUSES = frozenset({GmailDraftStatus.RESERVED, GmailDraftStatus.UNCONFIRMED})


class GmailDraftError(RuntimeError):
    """Drafting cannot proceed, and nothing was written to Gmail."""


@dataclass(frozen=True)
class DraftOutcome:
    """What happened to one message."""

    position: int
    message_version_id: uuid.UUID
    #: ``created``, ``reused``, ``recovered``, ``failed`` or ``unconfirmed``.
    outcome: str
    detail: str = ""


@dataclass
class DraftRun:
    """The result of one Create Gmail drafts click."""

    mailbox_address: str
    outcomes: list[DraftOutcome] = field(default_factory=list)
    skipped_discarded: int = 0

    @property
    def created(self) -> int:
        return sum(1 for item in self.outcomes if item.outcome == "created")

    @property
    def reused(self) -> int:
        return sum(1 for item in self.outcomes if item.outcome in {"reused", "recovered"})

    @property
    def failed(self) -> int:
        return sum(1 for item in self.outcomes if item.outcome == "failed")

    @property
    def unconfirmed(self) -> int:
        return sum(1 for item in self.outcomes if item.outcome == "unconfirmed")

    def summary(self) -> str:
        """One honest operator-readable sentence.

        Never claims a number it did not observe: "created" counts drafts Gmail
        returned an id for in *this* run, "already existed" counts lineage that
        was already there, and a partial or unconfirmed result says so first
        rather than being averaged into a success.
        """

        parts: list[str] = []
        if self.created:
            parts.append(f"{self.created} Gmail draft{_s(self.created)} created")
        if self.reused:
            parts.append(f"{self.reused} already existed and {_was(self.reused)} reused")
        if self.failed:
            parts.append(f"{self.failed} could not be created")
        if self.unconfirmed:
            parts.append(
                f"{self.unconfirmed} could not be confirmed — check your Gmail Drafts folder "
                "before clicking again"
            )
        if self.skipped_discarded:
            parts.append(
                f"{self.skipped_discarded} discarded message{_s(self.skipped_discarded)} skipped"
            )
        if not parts:
            return f"Nothing to draft into {self.mailbox_address}."
        return f"{'; '.join(parts)} in {self.mailbox_address}."

    @property
    def fully_successful(self) -> bool:
        return not self.failed and not self.unconfirmed


def _s(count: int) -> str:
    return "" if count == 1 else "s"


def _was(count: int) -> str:
    return "was" if count == 1 else "were"


@dataclass(frozen=True)
class _Draftable:
    position: int
    message_id: uuid.UUID
    version_id: uuid.UUID
    subject: str
    body: str


def _draftable_messages(
    session: Session, *, sequence: EmailSequence, expected_version_ids: tuple[uuid.UUID, ...]
) -> tuple[tuple[_Draftable, ...], int]:
    """The messages to draft, after the stale check and the discard rule.

    Returns ``(draftable, skipped_discarded)``. A discarded message is skipped
    rather than refused: the sequence contract says a discard stops the chain at
    that message, and the operator has already been told the sequence is not
    ready. Drafting one anyway would put text a human rejected into a mailbox.
    """

    states = sequence_review.message_states(session, sequence=sequence)
    if not states:
        raise GmailDraftError("This sequence has no messages, so nothing can be drafted.")

    current = {state.version_id for state in states}
    submitted = set(expected_version_ids)
    if not submitted:
        raise GmailDraftError("No message versions were named, so nothing was drafted.")
    if submitted != current:
        raise GmailDraftError(
            "The sequence on your screen is no longer the current one — a message has been "
            "edited or regenerated since the page loaded. Nothing was drafted. Reload and "
            "check the text before creating drafts."
        )

    skipped = sum(1 for state in states if not state.approved)
    wanted = {state.version_id for state in states if state.approved}
    if not wanted:
        raise GmailDraftError(
            "Every message in this sequence is discarded or has had its approval withdrawn, "
            "so there is nothing to draft."
        )

    rows = session.scalars(
        select(EmailSequenceMessageVersion).where(EmailSequenceMessageVersion.id.in_(wanted))
    ).all()
    by_id = {row.id: row for row in rows}
    draftable = tuple(
        _Draftable(
            position=state.position,
            message_id=state.message_id,
            version_id=state.version_id,
            subject=by_id[state.version_id].subject,
            body=by_id[state.version_id].body,
        )
        for state in states
        if state.version_id in by_id
    )
    return draftable, skipped


def _recipient_for(session: Session, *, sequence: EmailSequence) -> str:
    """The one authoritative recipient address, or a refusal.

    ``Contact.email`` is the canonical normalized address the rest of the
    pipeline verifies and suppresses against; nothing here invents, reformats or
    guesses one. The suppression ledger is consulted before the address is
    returned, because a draft addressed to a suppressed contact is one keystroke
    away from being sent to them.
    """

    contact = session.get(Contact, sequence.contact_id)
    if contact is None:
        raise GmailDraftError("This sequence has no contact on file, so nothing can be drafted.")
    address = (contact.email or "").strip()
    if not address:
        raise GmailDraftError(
            "This contact has no confirmed email address, so no Gmail draft can be addressed."
        )
    decision = evaluate_suppression(session, email=address, domain=contact.company_domain)
    if decision.blocked:
        raise GmailDraftError(
            f"This contact is suppressed ({decision.reason}), so no Gmail draft was created."
        )
    return address


def _guard_sequence(session: Session, *, sequence: EmailSequence) -> None:
    if sequence.superseded_at is not None:
        raise GmailDraftError(
            "This sequence has been superseded by a newer generation. Nothing was drafted; "
            "open the current sequence and try again."
        )
    if sequence.stop_state is SequenceStopState.STOPPED:
        raise GmailDraftError("This sequence is stopped, so no Gmail draft was created.")
    if sequence.generation_status is not SequenceGenerationStatus.COMPLETE:
        raise GmailDraftError("This sequence was never completed, so nothing can be drafted.")
    if sequence.validation_status is SequenceValidationStatus.FAILED:
        raise GmailDraftError("This sequence failed validation, so nothing can be drafted.")
    membership = session.get(CampaignContact, sequence.campaign_contact_id)
    if membership is None:
        raise GmailDraftError(
            "This sequence's campaign membership no longer exists, so nothing was drafted."
        )


def create_drafts(
    session: Session,
    *,
    sequence_id: uuid.UUID,
    expected_version_ids: tuple[uuid.UUID, ...],
    grant: GmailMailboxGrant,
    settings: GmailSettings,
    oauth_client: GmailOAuthClient,
    provider: GmailProvider,
    actor: str,
    now: datetime | None = None,
) -> DraftRun:
    """Create one Gmail draft per current, non-discarded message version.

    The caller commits. Every Gmail call is preceded by a *committed*
    reservation row, so this function deliberately commits several times: the
    alternative is one transaction wrapped around several external writes, which
    is the arrangement where a rollback erases the only local record that an
    external write happened.
    """

    moment = now or datetime.now(UTC)
    sequence = session.get(EmailSequence, sequence_id)
    if sequence is None:
        raise GmailDraftError("That sequence does not exist.")
    _guard_sequence(session, sequence=sequence)

    if not gmail_mailbox.scopes_are_sufficient(grant):
        raise GmailDraftError(
            "The connected Google account did not grant permission to create drafts. "
            "Disconnect and connect Gmail again."
        )

    recipient = _recipient_for(session, sequence=sequence)
    draftable, skipped = _draftable_messages(
        session, sequence=sequence, expected_version_ids=expected_version_ids
    )

    try:
        access_token = gmail_mailbox.access_token_for(
            session, grant=grant, settings=settings, client=oauth_client, now=moment
        )
    except gmail_mailbox.GmailMailboxError:
        # `access_token_for` moves the grant to RECONNECT_REQUIRED on the way
        # out, and that transition has to survive. Committing it here rather
        # than leaving it to the caller is deliberate: a caller that rolled the
        # failed action back -- the obvious thing to do -- would also roll back
        # the record of *why* it failed, and the operator would keep seeing a
        # connected mailbox that fails on every click with no explanation.
        session.commit()
        raise
    session.commit()

    run = DraftRun(mailbox_address=grant.mailbox_address, skipped_discarded=skipped)
    for item in draftable:
        run.outcomes.append(
            _draft_one(
                session,
                sequence=sequence,
                item=item,
                recipient=recipient,
                grant=grant,
                settings=settings,
                provider=provider,
                access_token=access_token,
                actor=actor,
                moment=moment,
            )
        )
    return run


def _existing_record(
    session: Session, *, grant: GmailMailboxGrant, version_id: uuid.UUID
) -> GmailDraftRecord | None:
    return session.scalars(
        select(GmailDraftRecord).where(
            GmailDraftRecord.mailbox_account_subject == grant.mailbox_account_subject,
            GmailDraftRecord.message_version_id == version_id,
        )
    ).first()


def _draft_one(
    session: Session,
    *,
    sequence: EmailSequence,
    item: _Draftable,
    recipient: str,
    grant: GmailMailboxGrant,
    settings: GmailSettings,
    provider: GmailProvider,
    access_token: str,
    actor: str,
    moment: datetime,
) -> DraftOutcome:
    message_id_value = mime.rfc_message_id(
        message_version_id=item.version_id, domain=settings.message_id_domain
    )
    fingerprint = mime.content_fingerprint(
        recipient=recipient, subject=item.subject, body=item.body
    )

    record = _existing_record(session, grant=grant, version_id=item.version_id)

    if record is not None and record.status is GmailDraftStatus.CREATED:
        return DraftOutcome(
            position=item.position,
            message_version_id=item.version_id,
            outcome="reused",
            detail="a Gmail draft for this exact message version already exists",
        )

    if record is not None and record.status in _UNRESOLVED_STATUSES:
        # ``RESERVED`` is here, and not only ``UNCONFIRMED``, because the two are
        # the same question wearing different clothes. A reservation is committed
        # *before* the Gmail call and stays ``RESERVED`` across it, so finding one
        # on a later run means one of three things: the process died between the
        # reservation and the call, it died between the call and the outcome
        # commit, or another request is inside that window right now. Only the
        # first is safe to re-attempt, and nothing in the row distinguishes it.
        #
        # Treating ``RESERVED`` as "nothing happened yet" was a real duplicate:
        # a worker killed after Gmail accepted the draft, or a second click a
        # moment behind the first, produced a second copy in a stranger-facing
        # mailbox. Reconciling first costs one lookup and answers the question
        # properly.
        resolved = _reconcile(
            session,
            record=record,
            provider=provider,
            access_token=access_token,
            moment=moment,
        )
        if resolved is not None:
            return resolved
        # Reconciliation cleared the row for another attempt.

    if record is None:
        record = GmailDraftRecord(
            mailbox_grant_id=grant.id,
            mailbox_account_subject=grant.mailbox_account_subject,
            mailbox_address=grant.mailbox_address,
            campaign_contact_id=sequence.campaign_contact_id,
            sequence_id=sequence.id,
            sequence_key=sequence.sequence_key,
            message_id=item.message_id,
            message_version_id=item.version_id,
            position=item.position,
            recipient_email=recipient,
            content_fingerprint=fingerprint,
            rfc_message_id=message_id_value,
            status=GmailDraftStatus.RESERVED,
            attempt_count=0,
            created_by=actor,
        )
        session.add(record)
        try:
            # Committed *before* the Gmail call. This is the reservation that
            # makes the idempotency key exist ahead of the external write, so a
            # crash in between leaves a known state rather than a silent gap.
            session.commit()
        except IntegrityError:
            # Two clicks raced. The constraint decided; this one reads the
            # winner rather than creating a second draft.
            session.rollback()
            existing = _existing_record(session, grant=grant, version_id=item.version_id)
            if existing is not None and existing.status is GmailDraftStatus.CREATED:
                return DraftOutcome(
                    position=item.position,
                    message_version_id=item.version_id,
                    outcome="reused",
                    detail="another request created this draft first",
                )
            return DraftOutcome(
                position=item.position,
                message_version_id=item.version_id,
                outcome="unconfirmed",
                detail="another request is creating this draft",
            )
    else:
        record.mailbox_grant_id = grant.id
        record.mailbox_address = grant.mailbox_address
        record.recipient_email = recipient
        record.content_fingerprint = fingerprint
        record.rfc_message_id = message_id_value
        record.status = GmailDraftStatus.RESERVED
        record.failure_category = None
        session.commit()

    try:
        raw = mime.build_raw_message(
            sender=grant.mailbox_address,
            recipient=recipient,
            subject=item.subject,
            body=item.body,
            rfc_message_id_value=message_id_value,
        )
    except mime.GmailMessageError as exc:
        record.status = GmailDraftStatus.FAILED
        record.failure_category = "unusable_message"
        session.commit()
        return DraftOutcome(
            position=item.position,
            message_version_id=item.version_id,
            outcome="failed",
            detail=str(exc),
        )

    record.attempt_count += 1
    session.commit()

    try:
        handle = provider.create_draft(access_token=access_token, raw_message=raw)
    except GmailProviderError as exc:
        if exc.ambiguous:
            record.status = GmailDraftStatus.UNCONFIRMED
            record.failure_category = exc.category
            session.commit()
            return DraftOutcome(
                position=item.position,
                message_version_id=item.version_id,
                outcome="unconfirmed",
                detail="Gmail did not answer, so it is not known whether the draft exists",
            )
        record.status = GmailDraftStatus.FAILED
        record.failure_category = exc.category
        session.commit()
        if exc.unauthorized:
            gmail_mailbox.mark_reconnect_required(session, grant=grant, category=exc.category)
            session.commit()
            return DraftOutcome(
                position=item.position,
                message_version_id=item.version_id,
                outcome="failed",
                detail="Gmail refused the connected mailbox; connect Gmail again",
            )
        return DraftOutcome(
            position=item.position,
            message_version_id=item.version_id,
            outcome="failed",
            detail="Gmail refused this draft",
        )

    record.status = GmailDraftStatus.CREATED
    record.gmail_draft_id = handle.draft_id
    record.gmail_message_id = handle.message_id
    record.gmail_thread_id = handle.thread_id
    record.failure_category = None
    session.commit()
    return DraftOutcome(
        position=item.position,
        message_version_id=item.version_id,
        outcome="created",
    )


def _reconcile(
    session: Session,
    *,
    record: GmailDraftRecord,
    provider: GmailProvider,
    access_token: str,
    moment: datetime,
) -> DraftOutcome | None:
    """Resolve one unconfirmed attempt, or say it is still unresolved.

    Returns an outcome when the question is settled and ``None`` when the caller
    may safely attempt the draft again. Bounded on purpose: one exact
    ``rfc822msgid:`` query against a ``Message-ID`` VMR itself minted, and no
    scan of anything else in the mailbox.
    """

    try:
        found = provider.find_draft_by_rfc_message_id(
            access_token=access_token, rfc_message_id=record.rfc_message_id
        )
    except GmailProviderError:
        return DraftOutcome(
            position=record.position,
            message_version_id=record.message_version_id,
            outcome="unconfirmed",
            detail="Gmail could not be asked whether this draft already exists",
        )

    if found is not None:
        record.status = GmailDraftStatus.CREATED
        record.gmail_draft_id = found.draft_id
        record.gmail_message_id = found.message_id
        record.gmail_thread_id = found.thread_id
        record.failure_category = None
        session.commit()
        return DraftOutcome(
            position=record.position,
            message_version_id=record.message_version_id,
            outcome="recovered",
            detail="an earlier attempt had in fact created this draft",
        )

    updated = record.updated_at
    if updated is not None and updated.tzinfo is None:
        updated = updated.replace(tzinfo=UTC)
    if updated is not None and moment - updated < timedelta(seconds=RECONCILIATION_MIN_AGE_SECONDS):
        # Gmail indexes a new draft asynchronously. "Not found" this soon after
        # the ambiguous attempt is not evidence that nothing was created, and
        # acting on it is how a retry produces the duplicate this whole design
        # exists to prevent.
        return DraftOutcome(
            position=record.position,
            message_version_id=record.message_version_id,
            outcome="unconfirmed",
            detail=("an attempt is in flight or too recent to rule out; try again shortly"),
        )
    return None
