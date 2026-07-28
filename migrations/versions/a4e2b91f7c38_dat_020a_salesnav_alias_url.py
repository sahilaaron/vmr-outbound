"""DAT-020A the derived Sales Navigator resolving alias, stored as evidence.

Revision ID: a4e2b91f7c38
Revises: c3f5a71d9b42
Create Date: 2026-07-29

Adds one nullable column, ``linkedin_profile_snapshots.salesnav_alias_url``.

DAT-019 stopped writing ``/in/<member-id>`` into the canonical profile URL,
which was right: an opaque Sales Navigator identifier is not the person's
published handle, and the normalizer that serves handles folds case, which
corrupts the identifier. What that correction removed was an operator's only way
to open a profile whose handle was not known yet. DAT-020 rebuilt the alias in
the extension; this column is where the backend keeps it.

It is a place to *record* a value, not a new identity. The column takes no part
in matching: no index, no uniqueness, no reader in any resolution path. Keeping
it beside ``normalized_profile_url`` and ``salesnav_member_id`` is the point —
three columns for the three distinct things a capture can know about a person's
LinkedIn identity, so that an export cannot present a derived alias as an
observed handle.

**No back-fill.** The alias is reconstructible for older rows from
``salesnav_member_id``, and deriving it here would be indistinguishable, after
the fact, from evidence that the extension actually observed and emitted it.
Existing rows keep NULL, which is the honest statement that this capture
predates the field.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a4e2b91f7c38"
down_revision: str | Sequence[str] | None = "c3f5a71d9b42"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "linkedin_profile_snapshots",
        sa.Column("salesnav_alias_url", sa.String(length=512), nullable=True),
    )


def downgrade() -> None:
    # Safe to drop unconditionally: the column is evidence that nothing reads for
    # matching, so removing it cannot orphan an identity or a promotion. The
    # recorded aliases themselves are lost, which is why the upgrade deliberately
    # never invented any.
    op.drop_column("linkedin_profile_snapshots", "salesnav_alias_url")
