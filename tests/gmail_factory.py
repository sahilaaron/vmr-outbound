"""Deterministic fixtures for the Gmail draft integration tests (#267).

Three things live here, and none of them touches a network:

``FakeGmailTransport``   a Gmail provider seam that records every call, can be
                         told to fail definitely or ambiguously, and physically
                         cannot send -- it implements the two methods on the
                         provider protocol and no third one.
``FakeGmailOAuthClient`` an authorization-code client that mints deterministic
                         tokens and records revocations.
``build_sequence``       one Campaign Contact with a complete seven-message
                         sequence, built straight through the ORM so a test can
                         set up an edit, a discard or a supersede exactly.

No test in this suite contacts Google, and none creates a draft in a real
mailbox. That is asserted rather than assumed: ``FakeGmailTransport`` is the
only provider any test installs, and
``test_gmail_draft_integration.py::test_no_real_gmail_endpoint_is_contacted_by_the_suite``
fails if a live adapter ever reaches a socket.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

from app.core.auth.identity import IdentityClaims
from app.models.campaign import Campaign, CampaignContact
from app.models.company import Company
from app.models.contact import Contact
from app.models.email_sequence import (
    EmailSequence,
    EmailSequenceMessage,
    EmailSequenceMessageVersion,
)
from app.models.enums import (
    CampaignStatus,
    SequenceMessageOrigin,
    SequenceMessagePurpose,
    SequenceMessageType,
)
from app.services.gmail.oauth import GmailTokenGrant
from app.services.gmail.provider import GmailDraftHandle, GmailProviderError
from sqlalchemy.orm import Session

PURPOSES = (
    SequenceMessagePurpose.INITIAL_OUTREACH,
    SequenceMessagePurpose.CONCISE_REMINDER,
    SequenceMessagePurpose.NEW_ANGLE,
    SequenceMessagePurpose.ROLE_RELEVANCE,
    SequenceMessagePurpose.PROOF_OR_OUTCOME,
    SequenceMessagePurpose.LOW_FRICTION_RESOURCE,
    SequenceMessagePurpose.CLOSE_THE_LOOP,
)
ELAPSED_DAYS = (0, 3, 7, 12, 18, 25, 35)
DELAY_DAYS = (0, 3, 4, 5, 6, 7, 10)


# ---------------------------------------------------------------------------
# Provider seams
# ---------------------------------------------------------------------------


@dataclass
class FakeGmailTransport:
    """A Gmail adapter that records calls and never leaves the process.

    ``create_draft`` and ``find_draft_by_rfc_message_id`` are the *whole*
    surface, exactly as on ``app.services.gmail.provider.GmailProvider``. There
    is deliberately no ``send``, no ``send_draft`` and no ``send_message``: a
    fake that offered one would let a test pass while the application called
    something no real deployment must be able to call.
    """

    #: Raised instead of answering, once per queued entry, then exhausted.
    failures: list[GmailProviderError] = field(default_factory=list)
    #: Every raw message handed to Gmail, in order.
    created: list[str] = field(default_factory=list)
    #: Drafts this fake believes exist, keyed by the RFC Message-ID.
    drafts_by_message_id: dict[str, GmailDraftHandle] = field(default_factory=dict)
    lookups: list[str] = field(default_factory=list)
    access_tokens_seen: list[str] = field(default_factory=list)
    #: When true, `create_draft` records the draft but then raises an ambiguous
    #: error -- Gmail acted and the answer was lost, the case idempotency has to
    #: survive.
    lose_response_after_creating: bool = False
    _counter: int = 0

    def create_draft(self, *, access_token: str, raw_message: str) -> GmailDraftHandle:
        self.access_tokens_seen.append(access_token)
        if self.failures and not self.lose_response_after_creating:
            raise self.failures.pop(0)
        self._counter += 1
        handle = GmailDraftHandle(
            draft_id=f"draft-{self._counter}",
            message_id=f"msg-{self._counter}",
            thread_id=f"thread-{self._counter}",
        )
        self.created.append(raw_message)
        message_id = _message_id_header(raw_message)
        if message_id:
            self.drafts_by_message_id[message_id] = handle
        if self.lose_response_after_creating:
            raise GmailProviderError("timeout", ambiguous=True)
        return handle

    def find_draft_by_rfc_message_id(
        self, *, access_token: str, rfc_message_id: str
    ) -> GmailDraftHandle | None:
        self.access_tokens_seen.append(access_token)
        self.lookups.append(rfc_message_id)
        return self.drafts_by_message_id.get(rfc_message_id)


def _message_id_header(raw_message: str) -> str | None:
    import base64

    decoded = base64.urlsafe_b64decode(raw_message.encode()).decode("utf-8", "replace")
    for line in decoded.splitlines():
        if line.lower().startswith("message-id:"):
            return line.split(":", 1)[1].strip()
        if not line.strip():
            break
    return None


def decoded_message(raw_message: str) -> str:
    import base64

    return base64.urlsafe_b64decode(raw_message.encode()).decode("utf-8", "replace")


@dataclass
class FakeGmailOAuthClient:
    """A Gmail authorization-code client with no network behind it."""

    mailbox_address: str = "operator@vmr.example"
    mailbox_subject: str = "gmail-account-subject-1"
    granted_scopes: tuple[str, ...] = (
        "openid",
        "email",
        "https://www.googleapis.com/auth/gmail.compose",
    )
    refresh_token: str | None = "refresh-token-1"
    expires_in: int = 3600
    #: Raised by `exchange_code`, so a test can drive a refused consent.
    exchange_error: Exception | None = None
    #: Raised by `refresh`, so a test can drive a revoked grant.
    refresh_error: Exception | None = None
    authorization_calls: list[dict[str, Any]] = field(default_factory=list)
    exchanges: list[dict[str, str]] = field(default_factory=list)
    refreshes: int = 0
    revoked: list[str] = field(default_factory=list)
    _issued: int = 0

    def authorization_url(
        self,
        *,
        redirect_uri: str,
        state: str,
        nonce: str,
        code_challenge: str,
        login_hint: str | None,
    ) -> str:
        self.authorization_calls.append(
            {
                "redirect_uri": redirect_uri,
                "state": state,
                "nonce": nonce,
                "code_challenge": code_challenge,
                "login_hint": login_hint,
                "scope": " ".join(self.granted_scopes),
            }
        )
        return f"https://accounts.google.test/gmail-consent?state={state}"

    def exchange_code(self, *, code: str, redirect_uri: str, code_verifier: str) -> GmailTokenGrant:
        self.exchanges.append(
            {"code": code, "redirect_uri": redirect_uri, "code_verifier": code_verifier}
        )
        if self.exchange_error is not None:
            raise self.exchange_error
        self._issued += 1
        nonce = self.authorization_calls[-1]["nonce"] if self.authorization_calls else ""
        return GmailTokenGrant(
            access_token=f"access-token-{self._issued}",
            refresh_token=self.refresh_token,
            expires_in=self.expires_in,
            granted_scopes=self.granted_scopes,
            claims=mailbox_claims(
                email=self.mailbox_address, subject=self.mailbox_subject, nonce=nonce
            ),
        )

    def refresh(self, *, refresh_token: str) -> GmailTokenGrant:
        self.refreshes += 1
        if self.refresh_error is not None:
            raise self.refresh_error
        self._issued += 1
        return GmailTokenGrant(
            access_token=f"access-token-{self._issued}",
            refresh_token=None,
            expires_in=self.expires_in,
            granted_scopes=self.granted_scopes,
            claims=None,
        )

    def revoke(self, *, token: str) -> None:
        self.revoked.append(token)


def mailbox_claims(
    *,
    email: str,
    subject: str,
    nonce: str,
    audience: str = "vmr-gmail-test-client.apps.googleusercontent.com",
    issuer: str = "https://accounts.google.com",
    email_verified: bool = True,
    now: int | None = None,
) -> IdentityClaims:
    import time

    moment = int(time.time()) if now is None else now
    return IdentityClaims(
        subject=subject,
        email=email,
        email_verified=email_verified,
        display_name="Mailbox Owner",
        issuer=issuer,
        audience=audience,
        expires_at=moment + 3600,
        issued_at=moment,
        nonce=nonce,
        hosted_domain=None,
    )


# ---------------------------------------------------------------------------
# Domain fixtures
# ---------------------------------------------------------------------------


@dataclass
class SequenceFixture:
    campaign: Campaign
    company: Company
    contact: Contact
    membership: CampaignContact
    sequence: EmailSequence
    messages: list[EmailSequenceMessage]
    versions: list[EmailSequenceMessageVersion]

    @property
    def version_ids(self) -> tuple[uuid.UUID, ...]:
        return tuple(version.id for version in self.versions)

    @property
    def version_ids_csv(self) -> str:
        return ",".join(str(value) for value in self.version_ids)


def build_sequence(
    db: Session,
    *,
    email: str | None = None,
    without_email: bool = False,
    sequence_enabled: bool = True,
    owner_user_id: uuid.UUID | str | None = None,
) -> SequenceFixture:
    """One Campaign Contact with a complete, live, seven-message sequence.

    Built through the ORM rather than through the generation pipeline on
    purpose: these tests are about what happens *after* a sequence exists, and
    driving a model call to obtain one would couple every Gmail assertion to the
    Personalization prompt.

    ``owner_user_id`` sets ``Campaign.created_by_user_id``. It exists because
    campaigns now have owners: a signed-in USER reaches only the campaigns they
    created or were assigned, so a fixture campaign with no owner is one the test
    operator cannot open, and the whole Gmail flow would be asserted against a
    403 rather than against Gmail. Passing the operator's account id makes the
    fixture describe the situation these tests are actually about — an operator
    working on their own campaign. Left ``None``, the campaign is ownerless,
    which is what a pre-migration campaign looks like.
    """

    company = Company(name="Kiln Systems", domain="kiln.example", industry="Industrial technology")
    campaign = Campaign(
        name=f"Gmail {uuid.uuid4()}",
        description="Sourced market intelligence reports",
        status=CampaignStatus.ACTIVE,
        execution_enabled=True,
        cadence_config={"sequence": {"enabled": True}} if sequence_enabled else {},
        created_by_user_id=(uuid.UUID(str(owner_user_id)) if owner_user_id is not None else None),
    )
    db.add_all([company, campaign])
    db.flush()

    contact = Contact(
        first_name="Ada",
        last_name="Lovelace",
        title="Head of Research",
        company_name=company.name,
        company_domain=company.domain,
        company_id=company.id,
        # `without_email` rather than `email=None`, so "give this contact no
        # address" cannot be confused with "pick one for me".
        email=None if without_email else (email or f"ada-{uuid.uuid4().hex[:8]}@kiln.example"),
        natural_key=f"ada|lovelace|{uuid.uuid4()}",
    )
    db.add(contact)
    db.flush()

    membership = CampaignContact(campaign_id=campaign.id, contact_id=contact.id)
    db.add(membership)
    db.flush()

    sequence_key = uuid.uuid4()
    sequence = EmailSequence(
        sequence_key=sequence_key,
        sequence_version=1,
        campaign_contact_id=membership.id,
        campaign_id=campaign.id,
        contact_id=contact.id,
        company_id=company.id,
        input_digest="0" * 64,
        sequence_producer_version="test-builder/v1",
        validation_policy_version="test-validation/v1",
        cadence_source="default",
        planned_span_days=35,
        message_count=7,
    )
    db.add(sequence)
    db.flush()

    messages: list[EmailSequenceMessage] = []
    predecessor: uuid.UUID | None = None
    for index in range(7):
        position = index + 1
        message = EmailSequenceMessage(
            sequence_key=sequence_key,
            campaign_contact_id=membership.id,
            position=position,
            message_type=(
                SequenceMessageType.INITIAL if position == 1 else SequenceMessageType.FOLLOW_UP
            ),
            purpose=PURPOSES[index],
            predecessor_message_id=predecessor,
        )
        db.add(message)
        # Flushed one at a time: each row's id is the next row's predecessor and
        # the chain check constraint runs on insert.
        db.flush()
        predecessor = message.id
        messages.append(message)

    versions: list[EmailSequenceMessageVersion] = []
    for index, message in enumerate(messages):
        version = EmailSequenceMessageVersion(
            message_id=message.id,
            sequence_id=sequence.id,
            message_version=1,
            position=message.position,
            subject=f"Kiln process control, note {message.position}",
            body=(
                f"Hello Ada,\n\nThis is sequence message {message.position} about sourced "
                "market coverage of process-control sectors.\n\nBest,\nVMR"
            ),
            recommended_delay_days=DELAY_DAYS[index],
            recommended_elapsed_day=ELAPSED_DAYS[index],
            origin=SequenceMessageOrigin.GENERATED,
        )
        db.add(version)
        versions.append(version)
    db.flush()

    return SequenceFixture(
        campaign=campaign,
        company=company,
        contact=contact,
        membership=membership,
        sequence=sequence,
        messages=messages,
        versions=versions,
    )
