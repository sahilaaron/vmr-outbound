"""The RFC 5322 message VMR hands to Gmail, and the fingerprint over it.

Two rules shape this module, and both are about *not* changing the text a human
approved:

1. **Nothing is added to the message.** No tracking pixel, no unsubscribe
   footer, no signature, no template expansion, no HTML wrapper. The subject and
   body are the exact strings on the message version the operator was looking
   at, and what appears in Gmail is what appears in VMR.
2. **Nothing is inferred about threading.** The message carries its own
   ``Message-ID`` and no ``In-Reply-To`` and no ``References``. VMR knows which
   sequence message precedes which -- that lineage is in
   ``email_sequence_messages.predecessor_message_id`` and is untouched -- but
   before the first message has actually been sent there is no RFC predecessor
   to reference. Minting one would make seven unrelated drafts *look* like a
   conversation that never happened. Gmail reply threading belongs to the
   delivery adapter, after a real send produces a real ``Message-ID``.

The ``Message-ID`` this module mints is this message's own identity, which every
RFC 5322 message needs and which Gmail would otherwise assign. It is derived
deterministically from the exact message version so that the bounded
reconciliation in ``drafts.py`` can ask Gmail one question -- "is there already
a draft with this id?" -- instead of scanning a mailbox.

Plain text only. The sequence generator's validation refuses HTML in a message
body (``no HTML where plain text is expected``), the review surface renders the
body in a ``<pre>``, and there is no canonical HTML representation anywhere in
the application to promote. A ``text/html`` alternative would therefore have to
be *invented* from the plain text, which is a content transformation this slice
is explicitly not permitted to make.
"""

from __future__ import annotations

import base64
import hashlib
import uuid
from email.headerregistry import Address
from email.message import EmailMessage

#: The longest subject and body this module will encode. Both are far above what
#: the sequence validator permits (300 and 20 000 characters), so hitting one
#: means something upstream is wrong rather than a message being long.
MAX_SUBJECT_CHARS = 2_000
MAX_BODY_CHARS = 200_000


class GmailMessageError(ValueError):
    """A sequence message cannot be turned into a valid RFC 5322 message."""


def rfc_message_id(*, message_version_id: uuid.UUID, domain: str) -> str:
    """The deterministic ``Message-ID`` for one exact message version.

    Deterministic, so that a retry after an ambiguous Gmail response can ask
    whether *this* draft already exists rather than guessing. Derived from the
    version id alone, so an edit -- which creates a new version -- gets a
    genuinely new message identity rather than reusing the one already sitting
    in the mailbox.
    """

    return f"<vmr-seq-{message_version_id.hex}@{domain}>"


def content_fingerprint(*, recipient: str, subject: str, body: str) -> str:
    """SHA-256 over the canonical rendering of what will be drafted.

    Stored on the lineage row so a reader can prove *what* was drafted without
    the record keeping a second copy of the message text -- which would drift
    from the immutable version that is already the authority for it.

    The separator is a NUL byte because it cannot occur in any of the three
    values, so no combination of recipient, subject and body can be rearranged
    into another combination with the same digest.
    """

    canonical = "\x00".join((recipient.strip().lower(), subject, body))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _validated_address(value: str) -> str:
    """One deliverable-looking address, or a refusal.

    Header injection is the risk being closed here: a newline in an address
    would end the ``To:`` header and let everything after it become headers of
    its own. ``EmailMessage`` refuses embedded newlines when it serialises, but
    refusing here means the failure names the address rather than surfacing as a
    serialisation error three layers away.
    """

    candidate = (value or "").strip()
    if not candidate:
        raise GmailMessageError("This contact has no email address, so nothing can be drafted.")
    # ASCII-only, and refused rather than encoded. A non-ASCII local part is a
    # `SMTPUTF8` address, which ``EmailMessage`` refuses to serialise under the
    # default policy -- and it raises ``email.errors.MessageDefect``, which is
    # *not* a subclass of this module's error, so it would escape every caller
    # and surface as a 500 with a half-written lineage row behind it. Refusing
    # here turns a crash into a message the operator can act on. VMR's own
    # address normalisation is ASCII-only for the same reason
    # (``app/core/auth/config.normalize_operator_email``), so nothing that can
    # reach this function legitimately is being excluded.
    if not candidate.isascii():
        raise GmailMessageError("This contact's email address is not usable as a recipient.")
    if any(character.isspace() for character in candidate):
        raise GmailMessageError("This contact's email address is not usable as a recipient.")
    if any(
        character in candidate for character in (",", ";", "<", ">", '"', "\\", "(", ")", "[", "]")
    ):
        raise GmailMessageError("This contact's email address is not usable as a recipient.")
    local, separator, domain = candidate.partition("@")
    if not separator or not local or not domain or "@" in domain or "." not in domain:
        raise GmailMessageError("This contact's email address is not usable as a recipient.")
    return candidate


def _validated_header_text(value: str, *, label: str, limit: int) -> str:
    candidate = (value or "").strip()
    if not candidate:
        raise GmailMessageError(f"This message has no {label}, so nothing can be drafted.")
    if len(candidate) > limit:
        raise GmailMessageError(f"This message's {label} is too long to draft.")
    if any(character in candidate for character in ("\r", "\n")):
        raise GmailMessageError(f"This message's {label} contains a line break and cannot be sent.")
    return candidate


def build_raw_message(
    *,
    sender: str,
    recipient: str,
    subject: str,
    body: str,
    rfc_message_id_value: str,
) -> str:
    """Return the base64url-encoded RFC 5322 message Gmail's ``raw`` field takes.

    ``raw`` rather than the structured ``message`` field: the structured form
    makes Gmail assemble the MIME, which means Gmail decides the encoding and
    the header set. Handing over the exact bytes keeps the message this
    application built the message the operator reads.
    """

    from_address = _validated_address(sender)
    to_address = _validated_address(recipient)
    clean_subject = _validated_header_text(subject, label="subject", limit=MAX_SUBJECT_CHARS)
    clean_body = (body or "").strip()
    if not clean_body:
        raise GmailMessageError("This message has no body, so nothing can be drafted.")
    if len(clean_body) > MAX_BODY_CHARS:
        raise GmailMessageError("This message's body is too long to draft.")

    message = EmailMessage()
    message["From"] = Address(addr_spec=from_address)
    message["To"] = Address(addr_spec=to_address)
    message["Subject"] = clean_subject
    message["Message-ID"] = rfc_message_id_value
    # Deliberately absent: In-Reply-To, References, and any Gmail threadId. See
    # the module docstring -- there is no sent predecessor for a follow-up draft
    # to reply to, and fabricating one would misrepresent a conversation.
    message.set_content(clean_body, subtype="plain", charset="utf-8")

    try:
        serialized = message.as_bytes()
    except Exception as exc:  # noqa: BLE001 - see below
        # Deliberately broad, and deliberately re-raised as this module's own
        # error. `email` reports a malformed message through several unrelated
        # exception types -- `MessageDefect`, `LookupError`, `UnicodeError` --
        # none of which shares a base class with `GmailMessageError`. The
        # validation above is written to make every one of them unreachable;
        # this converts the one that is not into a refusal the caller already
        # handles, instead of a 500 with a reserved lineage row behind it.
        raise GmailMessageError("This message could not be assembled as an email.") from exc

    return base64.urlsafe_b64encode(serialized).decode("ascii")
