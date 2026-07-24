"""CMP-003 outreach event attribution

Revision ID: 4b659a2054e4
Revises: f4c533f48a92
Create Date: 2026-07-24 12:42:28.334089

Adds three nullable attribution columns to the existing ``external_events``
table (DAT-001) so a historical outreach event is queryable by contact, by
campaign, and by the specific campaign-contact membership it happened under,
without inventing a parallel outreach-history table:

* ``contact_id`` -> ``contacts.id``, ``ON DELETE CASCADE``
* ``campaign_id`` -> ``campaigns.id``, ``ON DELETE CASCADE``
* ``campaign_contact_id`` -> ``campaign_contacts.id``, ``ON DELETE SET NULL``
  (deliberately not CASCADE: a membership row can be legitimately removed by
  the pre-existing duplicate-contact merge coalescing path in
  ``app/services/identity.py``, which now re-parents events onto the survivor
  before deleting the redundant row; ``SET NULL`` is the defense-in-depth
  fallback so a historical event is never silently deleted alongside it)

All three columns stay nullable: an external event can still be represented
before it is resolved to a contact, unchanged from the existing DAT-001
"representation only" behaviour. No historical row is altered or dropped by
this migration.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "4b659a2054e4"
down_revision: str | Sequence[str] | None = "f4c533f48a92"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column("external_events", sa.Column("contact_id", sa.UUID(), nullable=True))
    op.add_column("external_events", sa.Column("campaign_id", sa.UUID(), nullable=True))
    op.add_column("external_events", sa.Column("campaign_contact_id", sa.UUID(), nullable=True))
    op.create_index(
        "ix_external_events_campaign_contact_id",
        "external_events",
        ["campaign_contact_id"],
        unique=False,
    )
    op.create_index(
        "ix_external_events_campaign_id", "external_events", ["campaign_id"], unique=False
    )
    op.create_index(
        "ix_external_events_contact_id", "external_events", ["contact_id"], unique=False
    )
    op.create_foreign_key(
        op.f("fk_external_events_campaign_id_campaigns"),
        "external_events",
        "campaigns",
        ["campaign_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_foreign_key(
        op.f("fk_external_events_contact_id_contacts"),
        "external_events",
        "contacts",
        ["contact_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_foreign_key(
        op.f("fk_external_events_campaign_contact_id_campaign_contacts"),
        "external_events",
        "campaign_contacts",
        ["campaign_contact_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_constraint(
        op.f("fk_external_events_campaign_contact_id_campaign_contacts"),
        "external_events",
        type_="foreignkey",
    )
    op.drop_constraint(
        op.f("fk_external_events_contact_id_contacts"), "external_events", type_="foreignkey"
    )
    op.drop_constraint(
        op.f("fk_external_events_campaign_id_campaigns"), "external_events", type_="foreignkey"
    )
    op.drop_index("ix_external_events_contact_id", table_name="external_events")
    op.drop_index("ix_external_events_campaign_id", table_name="external_events")
    op.drop_index("ix_external_events_campaign_contact_id", table_name="external_events")
    op.drop_column("external_events", "campaign_contact_id")
    op.drop_column("external_events", "campaign_id")
    op.drop_column("external_events", "contact_id")
