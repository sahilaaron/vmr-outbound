"""Keep the displayed location on the permanent Contact.

Revision ID: b7e42d91c530
Revises: a3c91e5f7d02
Create Date: 2026-07-30

A capture records the location a LinkedIn page displayed for a person — "Greater
Chicago Area", "Pune, Maharashtra, India" — and the pending-capture row in the CRM
showed it. Promotion then dropped it: the value lived only inside the capture's
``profile_fields`` JSON, and the promoted Contact row read ``contacts.country``,
which nothing has ever written. So the location visibly disappeared at exactly the
moment a person became canonical, which reads as data loss because it is.

A separate column rather than reusing ``country``. A displayed location is free
text at whatever granularity the page happened to show, and writing a region or a
metro area into a field named ``country`` would make every consumer of that field
wrong — a normalized single-value country is a different claim from "this is what
the page said".

Nullable, because a page that showed no location must remain distinguishable from
one that showed an empty string. No backfill: the values for already-promoted
contacts are still in their captures' ``profile_fields``, and a migration that
guessed at them would invent provenance that never existed. They fill in naturally
on the next refresh.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b7e42d91c530"
down_revision: str | Sequence[str] | None = "a3c91e5f7d02"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("contacts", sa.Column("location", sa.String(length=255), nullable=True))


def downgrade() -> None:
    op.drop_column("contacts", "location")
