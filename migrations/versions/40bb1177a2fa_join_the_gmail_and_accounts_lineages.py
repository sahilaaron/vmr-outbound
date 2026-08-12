"""join the gmail and accounts lineages

Revision ID: 40bb1177a2fa
Revises: a7d3e1c85f42, b8f13a6c47d2
Create Date: 2026-08-12 19:51:02.356512

Two slices branched from ``0926b59b7912`` at the same time and neither knew about
the other: ``a7d3e1c85f42`` added the Gmail draft tables, ``b8f13a6c47d2`` added
``users`` and its credential tokens. Merging their branches leaves two Alembic
heads, which is not a state ``alembic upgrade head`` can act on.

This revision joins them and changes no schema. It is deliberately empty: the
alternative -- re-pointing one migration's ``down_revision`` at the other -- would
rewrite the recorded ancestry of a revision that has already been reviewed, and
would claim an ordering between two slices that genuinely had none.

The schema consequence of the two meeting is real, but it belongs to its own
revision rather than to a graph join, and is handled in ``b45732880eff``.
"""

from collections.abc import Sequence

# revision identifiers, used by Alembic.
revision: str = "40bb1177a2fa"
down_revision: str | Sequence[str] | None = ("a7d3e1c85f42", "b8f13a6c47d2")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """No schema change: this revision exists to join two lineages."""


def downgrade() -> None:
    """No schema change: this revision exists to join two lineages."""
