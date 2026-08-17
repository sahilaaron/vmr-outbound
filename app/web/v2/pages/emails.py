"""Actions on a person's emails: edit a message, create Gmail drafts.

These are the write routes behind the seven-email view on a person's page. The
optional review decisions (approve / discard) are recorded against exact
message versions; nothing here sends anything.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from fastapi import Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.core.auth.accounts import session_account_id
from app.core.auth.context import current_operator
from app.core.config import Settings, get_settings
from app.models.campaign import Campaign
from app.models.email_sequence import SEQUENCE_LENGTH, EmailSequenceMessageVersion
from app.models.enums import SequenceGenerationStatus, SequenceValidationStatus
from app.services import campaign_access
from app.services.campaign_access import actor_from_request
from app.services.gmail import drafts as gmail_drafts
from app.services.gmail import mailbox as gmail_mailbox
from app.services.gmail import provider as gmail_provider
from app.services.gmail import read as gmail_read
from app.services.personalization.cadence import campaign_opted_in
from app.services.sequences import read as sequence_read
from app.services.sequences import review as sequence_review
from app.web.v2 import shell

router = shell.router


#: Every way the sequence section can be in a state other than "here it is".
#: Each one is rendered with its own wording; see ``_sequence.html::unavailable``.
SEQUENCE_STATE_FEATURE_OFF = "feature_off"
SEQUENCE_STATE_CAMPAIGN_OFF = "campaign_off"
SEQUENCE_STATE_PENDING = "pending"
SEQUENCE_STATE_FAILED = "failed"
SEQUENCE_STATE_AVAILABLE = "available"


@dataclass(frozen=True)
class SequenceAvailability:
    """Why the sequence section looks the way it does, for one membership.

    This type exists because a boolean could not tell the truth. "No sequence"
    covering feature-off, campaign-not-opted-in, nothing-generated-yet and
    generation-refused is how an operator ends up waiting for something that is
    switched off, and every one of those four needs different wording.

    ``read_only`` is separate from ``state`` on purpose. A sequence that already
    exists stays visible when the deployment switch is off or the Campaign has
    opted out -- the work happened and the decisions are real -- but no new
    decision may be recorded against it, because the operator has just been told
    this configuration no longer produces sequences and a review action would
    contradict that.
    """

    state: str
    #: True when an existing sequence is shown but cannot be acted on.
    read_only: bool = False
    #: Set when the sequence is shown despite the configuration being off, so
    #: the page can explain why it is still here.
    notice: str | None = None

    @property
    def available(self) -> bool:
        return self.state == SEQUENCE_STATE_AVAILABLE


def gmail_draft_rows(
    db: Session, settings: Settings, *, sequence: Any | None
) -> dict[uuid.UUID, gmail_read.DraftRow]:
    """Gmail draft state for one sequence, for the operator making the request."""

    if sequence is None or not shell.gmail_drafts_on(db, settings):
        return {}
    owner = session_account_id(current_operator())
    if owner is None:
        return {}
    grant = gmail_mailbox.connected_grant(db, user_id=owner)
    if grant is None:
        return {}
    return gmail_read.draft_rows(
        db, sequence=sequence, mailbox_account_subject=grant.mailbox_account_subject
    )


def sequence_availability(
    db: Session, settings: Settings, *, campaign: Campaign | None, sequence: Any | None
) -> SequenceAvailability:
    """Resolve the exact state, given the two switches and what exists.

    The order is deliberate. An existing sequence is disclosed first, because
    hiding recorded work is the worst of the available answers; only when there
    is nothing to show does the configuration decide the wording.

    ``db`` is a parameter because the deployment half of the gate is now an
    administrator's durable setting rather than an environment variable, so
    reading it is a query.
    """

    generation_on = shell.sequences_on(db, settings)
    # ``campaign is None`` means the caller is not looking at one campaign -- the
    # unfiltered Review queue, for instance. Opt-in is a per-campaign fact, so
    # with no campaign in hand the honest answer is silence about it rather than
    # a claim that some campaign has not opted in.
    campaign_known = campaign is not None
    opted_in = campaign is not None and campaign_opted_in(campaign)

    if sequence is not None:
        if not generation_on:
            return SequenceAvailability(
                state=SEQUENCE_STATE_AVAILABLE,
                read_only=True,
                notice=(
                    "Seven-message sequences are switched off in this environment. This "
                    "sequence and every decision recorded against it are kept and shown in "
                    "full, but no new sequence will be written and no review action can be "
                    "recorded while the switch is off."
                ),
            )
        if campaign_known and not opted_in:
            return SequenceAvailability(
                state=SEQUENCE_STATE_AVAILABLE,
                read_only=True,
                notice=(
                    "This campaign is no longer configured to generate sequences, so the "
                    "Personalization Agent has gone back to writing a single draft for it. "
                    "The sequence below was written while the campaign was opted in; it is "
                    "kept and readable, and no new review action can be recorded against it."
                ),
            )
        failed = (
            sequence.generation_status is not SequenceGenerationStatus.COMPLETE
            or sequence.validation_status is SequenceValidationStatus.FAILED
        )
        if failed:
            return SequenceAvailability(state=SEQUENCE_STATE_FAILED)
        return SequenceAvailability(state=SEQUENCE_STATE_AVAILABLE)

    if not generation_on:
        return SequenceAvailability(state=SEQUENCE_STATE_FEATURE_OFF)
    if campaign_known and not opted_in:
        return SequenceAvailability(state=SEQUENCE_STATE_CAMPAIGN_OFF)
    return SequenceAvailability(state=SEQUENCE_STATE_PENDING)


def step_position(step: str | None) -> int:
    """Turn a ``?step=`` parameter into a position, defaulting to the initial.

    Out-of-range and unparseable values fall back to position 1 rather than
    404ing. A mistyped step is a navigation slip, not a missing resource, and
    the initial message is always the right thing to show instead.
    """

    if not step:
        return 1
    try:
        value = int(step)
    except ValueError:
        return 1
    return value if 1 <= value <= SEQUENCE_LENGTH else 1


# ---------------------------------------------------------------------------
# Contacts
# ---------------------------------------------------------------------------


#: Where a sequence action returns when the submitted target is not usable.
SEQUENCE_FALLBACK = "/app/review"


def _sequence_back(target: str) -> str:
    """Constrain a submitted redirect target to this application.

    The ``back`` field is operator-supplied and is echoed into a ``Location``
    header, so it must not be able to point off-site. A value that is not a
    plain in-app path is replaced outright rather than repaired: a half-fixed
    redirect target is harder to reason about than a discarded one.

    ``//host`` is rejected explicitly. It starts with a slash and looks local,
    but a browser reads it as a protocol-relative absolute URL and leaves the
    site.
    """

    candidate = (target or "").strip()
    if not candidate.startswith("/app") or candidate.startswith("//"):
        return SEQUENCE_FALLBACK
    return candidate


#: The largest form body a sequence write route will accept, in bytes. A message
#: body is truncated to 20 000 characters and a subject to 300, so anything past
#: this is not a message -- and by the time truncation runs, the whole request
#: has already been buffered in memory.
MAX_SEQUENCE_FORM_BYTES = 256 * 1024

OVERSIZED_REFUSAL = (
    "That submission was too large to be a sequence message, so nothing was changed. "
    "A message body is limited to 20,000 characters."
)


def _oversized(request: Request) -> bool:
    """Whether the declared body is too large to be a sequence edit.

    Checked from ``Content-Length`` before the form is read, which is the only
    point where refusing costs nothing. This does not close the hole completely
    -- a chunked request declares no length, and Starlette buffers as it parses
    -- so it is a bound on the ordinary case rather than a guarantee. The
    complete fix is a body-size limit at the server or proxy layer. Production
    Hardening now supplies one -- ``MAX_REQUEST_BYTES``, enforced in
    ``app/core/http.py`` before a route is reached -- but at 25 MiB, which is a
    ceiling for uploads rather than for a review note. This route-level bound
    stays because the two answer different questions: PH stops a request that
    could exhaust the process, this stops a message body that is not a message.
    """

    declared = request.headers.get("content-length")
    if declared is None:
        return False
    try:
        return int(declared) > MAX_SEQUENCE_FORM_BYTES
    except ValueError:
        return False


def _same_origin(request: Request) -> bool:
    """Whether this write plausibly came from this application's own pages.

    The workbench has no sessions, no cookies and no sign-in, so a cross-site
    POST carries no ambient authority and cannot authenticate as anybody. What it
    *can* do, while an operator has the local server running, is drive the
    sequence write routes blind from a page in another tab. Approving or
    discarding was already reachable that way; the edit route widened it to
    "write arbitrary text into a message body", and that widening is worth
    closing on its own terms.

    Two headers, in order of reliability. ``Sec-Fetch-Site`` is set by the
    browser and cannot be forged by page script; ``same-origin`` and ``none``
    (a typed URL or a bookmark) are accepted, anything else is refused. Failing
    that, ``Origin`` is compared against the request's own host.

    A request carrying neither header is allowed. That is deliberate, not an
    oversight: ``curl``, the test client and any scripted local tool send
    neither, and this check is a cross-site guard rather than an authentication
    mechanism. Treating "no headers" as hostile would break every non-browser
    caller while stopping no browser attack, because browsers always send them.

    ``Origin: null`` is allowed for the same reason, and this is not a gap that
    was tolerated -- it is one Production Hardening created. PH sets
    ``Referrer-Policy: no-referrer`` on every response, and the Fetch Standard
    says a document with that policy serialises its origin as ``null`` when it
    sends a form POST. So every write from these very pages carries
    ``Origin: null``. Modern browsers hide the consequence because
    ``Sec-Fetch-Site`` is checked first and short-circuits; a browser that
    implements ``Referrer-Policy`` but not ``Sec-Fetch-*``, or any proxy or
    extension that strips ``Sec-Fetch-*`` while leaving ``Origin``, would have
    had every approve, discard and edit silently refused with a message saying
    the request came from another site.

    Refusing ``null`` also buys nothing. A genuine cross-site POST from an
    attacker's page carries *that page's* origin, which still fails the host
    comparison below; the only requests ``null`` describes are ones whose origin
    the browser declined to disclose, and this application's own pages are now
    permanently among them.
    """

    site = request.headers.get("sec-fetch-site")
    if site is not None:
        return site in {"same-origin", "none"}
    origin = request.headers.get("origin")
    if origin is None or origin == "null":
        return True
    host = request.headers.get("host")
    if host is None:  # pragma: no cover - Host is mandatory in HTTP/1.1
        return False
    return origin.rstrip("/").endswith(f"//{host}")


CROSS_SITE_REFUSAL = (
    "That request did not come from this application, so nothing was changed. "
    "Sequence review actions can only be taken from the review pages themselves."
)


def _sequence_write_refusal(
    db: Session, settings: Settings, *, sequence_id: uuid.UUID | None
) -> str | None:
    """Why this sequence cannot be acted on right now, or ``None``.

    Read-only is enforced here rather than only in the template. A page can be
    left open across a configuration change, and a form that has already been
    rendered will happily post; the refusal has to live where the write happens.

    The rule matches what the page says: a sequence stays fully readable when
    the deployment switch is off or its Campaign has opted out, but no new
    decision may be recorded against it. Recording one would contradict the
    notice the operator was just shown, and would put a fresh human decision on
    a configuration that no longer produces sequences.
    """

    if sequence_id is None:
        return None
    sequence = sequence_read.get_sequence(db, sequence_id)
    if sequence is None:
        return None
    availability = sequence_availability(
        db,
        settings,
        campaign=db.get(Campaign, sequence.campaign_id),
        sequence=sequence,
    )
    if not availability.read_only:
        return None
    if not shell.sequences_on(db, settings):
        return (
            "Seven-message sequences are switched off in this environment, so no review "
            "decision can be recorded. The sequence and its existing decisions are unchanged."
        )
    return (
        "This campaign is no longer configured to generate sequences, so no new review "
        "decision can be recorded against this one. Its existing decisions are unchanged."
    )


def _sequence_id_for_version(db: Session, version_id: uuid.UUID) -> uuid.UUID | None:
    version = db.get(EmailSequenceMessageVersion, version_id)
    return version.sequence_id if version is not None else None


def _require_review_access(request: Request, db: Session, campaign_id: uuid.UUID | None) -> None:
    """Refuse a review write against a campaign this account may not use.

    The review routes are keyed by a *draft* or *sequence* id, not by a campaign
    id, so the router-level path guard never sees them — it only fires on a
    ``{campaign_id}`` path parameter. The review page itself is scoped, including
    its ``?draft=`` and ``?sequence=`` deep links, so the ids are not on offer;
    but hiding an id is a courtesy and this is the control.

    It matters more here than almost anywhere else on the surface. Approval is
    the human authorisation the whole pipeline waits for: an approved draft is a
    statement that a named person read this exact version and is willing for it
    to go out. Letting somebody outside the campaign record that statement would
    put a signature on work they were never shown.

    ``campaign_id`` of ``None`` means the target could not be resolved at all —
    a deleted or bogus id — and is left to the handler, which already answers it
    with a specific message.
    """

    if campaign_id is None:
        return
    campaign_access.require_campaign_access(db, campaign_id, actor_from_request(request))


def _sequence_campaign_id(db: Session, sequence_id: uuid.UUID | None) -> uuid.UUID | None:
    if sequence_id is None:
        return None
    record = sequence_read.get_sequence(db, sequence_id)
    return record.campaign_id if record is not None else None


@router.post("/review/sequence/messages/{version_id}/approve")
def sequence_message_approve(
    version_id: str,
    request: Request,
    db: Session = Depends(get_db),
    reason: str = Form(""),
    back: str = Form("/app/review"),
) -> RedirectResponse:
    """Approve one message. The other six are untouched."""

    target = _sequence_back(back)
    identifier = shell.uuid_or_none(version_id)
    if identifier is None:
        return shell.redirect(target, err="That is not a sequence message version id.")
    if not _same_origin(request):
        return shell.redirect(target, err=CROSS_SITE_REFUSAL)
    if _oversized(request):
        return shell.redirect(target, err=OVERSIZED_REFUSAL)
    sequence_id_for_message = _sequence_id_for_version(db, identifier)
    _require_review_access(request, db, _sequence_campaign_id(db, sequence_id_for_message))
    refusal = _sequence_write_refusal(db, get_settings(), sequence_id=sequence_id_for_message)
    if refusal is not None:
        return shell.redirect(target, err=refusal)
    try:
        sequence_review.approve_message(
            db,
            message_version_id=identifier,
            actor=sequence_review.OPERATOR_ACTOR,
            reason=reason or None,
        )
    except sequence_review.SequenceReviewError as exc:
        return shell.redirect(target, err=str(exc))
    db.commit()
    return shell.redirect(
        target,
        ok=(
            "Approved, and recorded against this exact message version. Nothing was sent "
            "and no Gmail draft was created: there is no sending path in this build."
        ),
    )


@router.post("/review/sequence/messages/{version_id}/discard")
def sequence_message_discard(
    version_id: str,
    request: Request,
    db: Session = Depends(get_db),
    reason: str = Form(""),
    back: str = Form("/app/review"),
) -> RedirectResponse:
    """Discard one message without pretending the sequence is ready."""

    target = _sequence_back(back)
    identifier = shell.uuid_or_none(version_id)
    if identifier is None:
        return shell.redirect(target, err="That is not a sequence message version id.")
    if not _same_origin(request):
        return shell.redirect(target, err=CROSS_SITE_REFUSAL)
    if _oversized(request):
        return shell.redirect(target, err=OVERSIZED_REFUSAL)
    sequence_id_for_message = _sequence_id_for_version(db, identifier)
    _require_review_access(request, db, _sequence_campaign_id(db, sequence_id_for_message))
    refusal = _sequence_write_refusal(db, get_settings(), sequence_id=sequence_id_for_message)
    if refusal is not None:
        return shell.redirect(target, err=refusal)
    try:
        sequence_review.discard_message(
            db,
            message_version_id=identifier,
            actor=sequence_review.OPERATOR_ACTOR,
            reason=reason or None,
        )
    except sequence_review.SequenceReviewError as exc:
        return shell.redirect(target, err=str(exc))
    db.commit()
    return shell.redirect(
        target,
        ok="Discarded. The sequence is not ready while one of its messages is discarded.",
    )


@router.post("/review/sequence/messages/{version_id}/edit")
def sequence_message_edit(
    version_id: str,
    request: Request,
    db: Session = Depends(get_db),
    subject: str = Form(""),
    body: str = Form(""),
    reason: str = Form(""),
    back: str = Form("/app/review"),
) -> RedirectResponse:
    """Write a new version of one message, keeping the text it replaced."""

    target = _sequence_back(back)
    identifier = shell.uuid_or_none(version_id)
    if identifier is None:
        return shell.redirect(target, err="That is not a sequence message version id.")
    if not _same_origin(request):
        return shell.redirect(target, err=CROSS_SITE_REFUSAL)
    if _oversized(request):
        return shell.redirect(target, err=OVERSIZED_REFUSAL)
    sequence_id_for_message = _sequence_id_for_version(db, identifier)
    _require_review_access(request, db, _sequence_campaign_id(db, sequence_id_for_message))
    refusal = _sequence_write_refusal(db, get_settings(), sequence_id=sequence_id_for_message)
    if refusal is not None:
        return shell.redirect(target, err=refusal)
    try:
        sequence_review.edit_message(
            db,
            message_version_id=identifier,
            subject=subject,
            body=body,
            actor=sequence_review.OPERATOR_ACTOR,
            reason=reason or None,
        )
    except sequence_review.SequenceReviewError as exc:
        return shell.redirect(target, err=str(exc))
    db.commit()
    return shell.redirect(
        target,
        ok=(
            "Saved as a new version. The previous version is kept, any approval against it "
            "is marked invalidated, and the other six messages are unchanged."
        ),
    )


@router.post("/review/sequence/{sequence_id}/approve")
def sequence_approve(
    sequence_id: str,
    request: Request,
    db: Session = Depends(get_db),
    version_ids: str = Form(""),
    reason: str = Form(""),
    back: str = Form("/app/review"),
) -> RedirectResponse:
    """Approve every message in one operation, naming every exact version.

    ``version_ids`` is what the page was showing. If it no longer matches what
    is stored, nothing is approved -- a bulk approval that quietly covered a
    version the operator never read would be exactly the ambiguity the sequence
    review model exists to prevent.
    """

    target = _sequence_back(back)
    identifier = shell.uuid_or_none(sequence_id)
    if identifier is None:
        return shell.redirect(target, err="That is not a sequence id.")
    if not _same_origin(request):
        return shell.redirect(target, err=CROSS_SITE_REFUSAL)
    if _oversized(request):
        return shell.redirect(target, err=OVERSIZED_REFUSAL)
    _require_review_access(request, db, _sequence_campaign_id(db, identifier))
    refusal = _sequence_write_refusal(db, get_settings(), sequence_id=identifier)
    if refusal is not None:
        return shell.redirect(target, err=refusal)
    parsed = tuple(
        value
        for value in (shell.uuid_or_none(item) for item in version_ids.split(",") if item.strip())
        if value is not None
    )
    if not parsed:
        return shell.redirect(
            target, err="No message versions were named, so nothing was approved."
        )
    try:
        sequence_review.approve_sequence(
            db,
            sequence_id=identifier,
            expected_version_ids=parsed,
            actor=sequence_review.OPERATOR_ACTOR,
            reason=reason or None,
        )
    except sequence_review.SequenceReviewError as exc:
        return shell.redirect(target, err=str(exc))
    db.commit()
    return shell.redirect(
        target,
        ok=(
            "Approved all seven messages, each recorded against its exact version. Nothing "
            "was sent and no Gmail draft was created."
        ),
    )


@router.post("/review/sequence/{sequence_id}/gmail-drafts")
def sequence_create_gmail_drafts(
    sequence_id: str,
    request: Request,
    db: Session = Depends(get_db),
    version_ids: str = Form(""),
    back: str = Form("/app/review"),
) -> RedirectResponse:
    """Create one Gmail draft per current, non-discarded message version (#267).

    ``version_ids`` is what the page was showing, submitted for the same reason
    the bulk approval submits it: if the stored versions no longer match, the
    operator is looking at text that has been replaced, and drafting the newer
    text would put a message they never read into somebody's mailbox. The check
    lives in ``app/services/gmail/drafts.py`` so it cannot be skipped by a
    second caller.

    This route creates drafts. It cannot send: the Gmail adapter implements no
    send call, and the only Gmail endpoints reachable from here are
    ``users.drafts.create`` and a bounded ``users.drafts.list`` lookup.
    """

    target = _sequence_back(back)
    settings = get_settings()
    if not shell.gmail_drafts_on(db, settings):
        return shell.redirect(
            target,
            err="Gmail draft creation is switched off in this environment, so nothing happened.",
        )
    identifier = shell.uuid_or_none(sequence_id)
    if identifier is None:
        return shell.redirect(target, err="That is not a sequence id.")
    if not _same_origin(request):
        return shell.redirect(target, err=CROSS_SITE_REFUSAL)
    if _oversized(request):
        return shell.redirect(target, err=OVERSIZED_REFUSAL)
    _require_review_access(request, db, _sequence_campaign_id(db, identifier))
    # A sequence that is read-only for review is read-only for drafting too.
    # Creating a draft from a sequence whose feature switch or campaign opt-in
    # has since been turned off would take a *more* consequential action than
    # the review decision the same page refuses to record.
    refusal = _sequence_write_refusal(db, settings, sequence_id=identifier)
    if refusal is not None:
        return shell.redirect(target, err=refusal)

    operator = current_operator()
    owner = session_account_id(operator)
    if operator is None or owner is None:
        return shell.redirect(
            target,
            err=(
                "Creating Gmail drafts requires a signed-in operator, because a mailbox is "
                "connected to one. This environment has no operator sign-in."
            ),
        )
    grant = gmail_mailbox.connected_grant(db, user_id=owner)
    if grant is None:
        return shell.redirect(
            target, err="No Gmail mailbox is connected, so no draft was created. Connect Gmail."
        )

    parsed = tuple(
        value
        for value in (shell.uuid_or_none(item) for item in version_ids.split(",") if item.strip())
        if value is not None
    )
    if not parsed:
        return shell.redirect(target, err="No message versions were named, so nothing was drafted.")

    from app.web.gmail_routes import oauth_client

    try:
        oauth = oauth_client(request, settings)
    except ValueError:
        return shell.redirect(
            target,
            err="Gmail is not configured in this environment, so no draft could be created.",
        )
    provider = getattr(request.app.state, GMAIL_PROVIDER_STATE_KEY, None) or (
        gmail_provider.HttpGmailProvider(settings.gmail)
    )

    try:
        run = gmail_drafts.create_drafts(
            db,
            sequence_id=identifier,
            expected_version_ids=parsed,
            grant=grant,
            settings=settings.gmail,
            oauth_client=oauth,
            provider=provider,
            actor=operator.email,
        )
    except gmail_mailbox.GmailMailboxError as exc:
        # Deliberately no rollback: the service has already committed the
        # reconnect-required transition, and undoing it would hide why this
        # failed from the page the operator is about to land on.
        return shell.redirect(target, err=str(exc))
    except gmail_drafts.GmailDraftError as exc:
        # Raised only before any Gmail call, so nothing external happened and
        # there is nothing committed to preserve.
        db.rollback()
        return shell.redirect(target, err=str(exc))

    # `create_drafts` commits as it goes, deliberately: each Gmail call is
    # preceded by a committed reservation. There is nothing left to commit here,
    # and the summary reports only what was actually observed.
    if run.fully_successful:
        return shell.redirect(target, ok=run.summary())
    return shell.redirect(target, err=run.summary())


#: Lets a test inject a deterministic Gmail transport without a network. The
#: production path builds an ``HttpGmailProvider`` per request, which is cheap:
#: it holds no connection pool of its own.
GMAIL_PROVIDER_STATE_KEY = "vmr_gmail_provider"


__all__ = ["router"]
