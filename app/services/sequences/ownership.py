"""Proving a posted message version belongs to the person the caller selected.

Every side-effecting action on the sending desk is addressed by a URL —
``/app/campaigns/{campaign_id}/desk/{membership_id}/{position}/…`` — and carries
a ``version_id`` in the form body. The route validated the three path segments
and then trusted the body, which is the gap this module closes.

**Why a hidden field is not an authorization boundary.** ``version_id`` reaches
the server as a form value. A form value is chosen by whoever submits the form,
not by whoever rendered it: it can be edited in the browser, replayed from
another page, or posted directly with curl. Rendering it inside a page the
caller was allowed to see says nothing about the id that comes back. So the id
has to be *re-derived* against the selection in the path, on the server, every
time — which is what :func:`current_version_for` does.

What that gap allowed, concretely. A caller with access to Campaign A posts A's
path with a ``version_id`` belonging to a person in Campaign B:

* on Edit, ``sequence_review.edit_message`` would write a new version of B's
  message — supersede B's approved text, invalidate B's approval, and record
  the edit against a Campaign the caller never opened;
* on Create Gmail Draft, ``gmail_drafts.create_draft`` resolves the recipient
  from the *version's* own sequence, so the draft would be composed to B's
  contact and placed in the caller's mailbox. That is a cross-Campaign
  disclosure of another person's address and message text, not merely a
  misrouted write.

Neither service is at fault. Both correctly refuse a version that does not
exist, is superseded, or hangs off a superseded sequence — they simply have no
way to know which membership the *caller* selected, because it is not one of
their arguments. Binding the two together is the caller's job, and doing it in
one place is what keeps the two routes from drifting apart.

**One helper, both routes, before any durable effect.** The function returns the
version or raises; it writes nothing, flushes nothing, and reaches no external
service, so a refusal cannot leave anything half-applied.
"""

from __future__ import annotations

import uuid

from sqlalchemy.orm import Session

from app.models.campaign import CampaignContact
from app.models.email_sequence import (
    EmailSequence,
    EmailSequenceMessage,
    EmailSequenceMessageVersion,
)


class SequenceOwnershipError(Exception):
    """The posted version is not the current message at that position for that person."""


#: Every refusal says the same thing, deliberately.
#:
#: The checks below distinguish "no such version", "superseded version", "wrong
#: position" and "another Campaign's version", and the caller must not be able
#: to tell those apart. A message that named the real cause would answer, for
#: any id at all, whether it exists and whether the caller may reach it —
#: turning a refusal into a lookup service for ids the caller is not entitled
#: to. The desk always re-renders the current email underneath this message, so
#: a legitimate user whose page went stale is told what to do without being told
#: anything about somebody else's data.
REFUSAL = "That email could not be found. Reload the page and try again."


def current_version_for(
    session: Session,
    *,
    campaign_id: uuid.UUID,
    membership: CampaignContact,
    position: int,
    version_id: uuid.UUID,
) -> EmailSequenceMessageVersion:
    """Resolve ``version_id`` only if it is *this* person's current email at ``position``.

    Raises :class:`SequenceOwnershipError` otherwise. The order of the checks
    carries no security meaning — every failure raises the same
    :data:`REFUSAL` — but the set does:

    * the version exists;
    * it is current, not superseded by a later edit;
    * it sits at the position named in the path;
    * its logical message and its generated sequence both belong to the
      selected ``CampaignContact``;
    * that sequence is itself current, not a superseded generation;
    * and the membership belongs to the Campaign named in the path.

    The last check is re-asserted here rather than assumed. The routes resolve
    the membership through a helper that already enforces it, and that is
    exactly why it is cheap to state again: this function is the boundary, and a
    boundary that depends on its callers having been careful is not one.
    """

    if membership.campaign_id != campaign_id:
        raise SequenceOwnershipError(REFUSAL)

    version = session.get(EmailSequenceMessageVersion, version_id)
    if version is None or version.superseded_at is not None:
        raise SequenceOwnershipError(REFUSAL)
    if version.position != position:
        raise SequenceOwnershipError(REFUSAL)

    # The logical message and the generation are checked independently rather
    # than one via the other. They are separate foreign keys on the version row,
    # so agreeing that *both* point back at this membership is a stronger
    # statement than following either one alone.
    message = session.get(EmailSequenceMessage, version.message_id)
    if message is None or message.campaign_contact_id != membership.id:
        raise SequenceOwnershipError(REFUSAL)
    if message.position != position:
        raise SequenceOwnershipError(REFUSAL)

    sequence = session.get(EmailSequence, version.sequence_id)
    if sequence is None or sequence.campaign_contact_id != membership.id:
        raise SequenceOwnershipError(REFUSAL)
    if sequence.superseded_at is not None:
        raise SequenceOwnershipError(REFUSAL)
    if sequence.campaign_id != campaign_id:
        raise SequenceOwnershipError(REFUSAL)

    return version


__all__ = ["REFUSAL", "SequenceOwnershipError", "current_version_for"]
