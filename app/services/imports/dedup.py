"""Conservative, deterministic contact deduplication (DAT-004).

Matching rules — in priority order — are intentionally strict so two different
people are never combined merely because their names or companies look similar:

1. **Exact normalized email.** A shared normalized email address is a strong
   identity signal: same email == same person.
2. **Exact natural key**, used only when the incoming row has *no* email. The
   natural key is ``casefold(first)|casefold(last)|domain`` — an exact match, not
   a similarity score. If more than one existing contact shares that key (because
   they were distinguished by different emails), the match is treated as
   ambiguous and the row is kept separate rather than merged.

When an incoming row *has* an email but it matches no existing contact, a new
contact is created even if the name+domain coincides with an email-less contact:
preferring a possible false duplicate over an incorrect merge is the required
bias.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.contact import Contact
from app.models.enums import DedupMatchType


@dataclass(frozen=True)
class DedupResult:
    """Outcome of looking for an existing contact that matches a row."""

    contact: Contact | None
    match_type: DedupMatchType | None
    ambiguous: bool
    note: str | None

    @property
    def is_match(self) -> bool:
        return self.contact is not None


def find_existing_contact(
    session: Session,
    *,
    email: str | None,
    natural_key: str | None,
) -> DedupResult:
    """Find the single existing contact that this row is a duplicate of, if any.

    Contacts created earlier in the same import are visible here because the
    importer flushes them to the session before processing later rows, so
    intra-batch duplicates are caught the same way as cross-batch ones.
    """

    if email:
        existing = session.scalars(select(Contact).where(Contact.email == email)).first()
        if existing is not None:
            return DedupResult(
                contact=existing,
                match_type=DedupMatchType.EMAIL,
                ambiguous=False,
                note=f"matched existing contact {existing.id} by exact email",
            )
        # Email present but unmatched: do not fall through to natural-key
        # matching — a distinct email means we treat this as a distinct person.
        return DedupResult(contact=None, match_type=None, ambiguous=False, note=None)

    if natural_key:
        matches = session.scalars(select(Contact).where(Contact.natural_key == natural_key)).all()
        if len(matches) == 1:
            existing = matches[0]
            return DedupResult(
                contact=existing,
                match_type=DedupMatchType.NATURAL_KEY,
                ambiguous=False,
                note=f"matched existing contact {existing.id} by exact natural key",
            )
        if len(matches) > 1:
            return DedupResult(
                contact=None,
                match_type=None,
                ambiguous=True,
                note=(
                    f"{len(matches)} existing contacts share this natural key; "
                    "kept separate to avoid an incorrect merge"
                ),
            )

    return DedupResult(contact=None, match_type=None, ambiguous=False, note=None)
