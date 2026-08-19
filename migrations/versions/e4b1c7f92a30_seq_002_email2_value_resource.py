"""SEQ-002: the Campaign Report URL and the ``VALUE_RESOURCE`` message purpose.

Revision ID: e4b1c7f92a30
Revises: d6a7c4b9e201
Create Date: 2026-08-19 09:40:00.000000

Two additive changes, and one of them is only additive because of how it is
written down.

**``campaigns.campaign_resource_url``** is a nullable address column. Every
existing Campaign takes ``NULL`` and stays valid: a Campaign that writes one
email per person has no report to offer, and a Campaign created before Email 2
owned a resource never had one. The requirement that a seven-message Campaign
must carry one lives in
``app/services/personalization/sequence.py``, where it is true, rather than in a
NOT NULL constraint, where it would be false for most of the table.

**``sequence_message_purpose`` gains ``VALUE_RESOURCE``.** It does not lose
``CONCISE_REMINDER``, and no row is rewritten. Position 2 of every sequence
generated before ``sequence-builder/v2`` was a concise reminder and carried no
resource; relabelling those rows would make the record claim seven emails
contained a link that six of them never had. The new label is what position 2
means from this revision onward, the old one is what it meant before, and both
statements are true at the same time because both labels exist.

``ALTER TYPE ... ADD VALUE`` runs inside the migration transaction, which
PostgreSQL 12+ permits so long as the new label is not *used* in the same
transaction. It is not: nothing here writes a row.

Downgrade REFUSES once either addition holds data. See :func:`downgrade`.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e4b1c7f92a30"
down_revision: str | Sequence[str] | None = "d6a7c4b9e201"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: The labels ``sequence_message_purpose`` carried before this revision, in the
#: order SEQ-001 created them. Named here so the downgrade rebuilds exactly the
#: type it found rather than whatever the model happens to say later.
_OLD_PURPOSES = (
    "INITIAL_OUTREACH",
    "CONCISE_REMINDER",
    "NEW_ANGLE",
    "ROLE_RELEVANCE",
    "PROOF_OR_OUTCOME",
    "LOW_FRICTION_RESOURCE",
    "CLOSE_THE_LOOP",
)


def upgrade() -> None:
    op.add_column(
        "campaigns",
        sa.Column("campaign_resource_url", sa.String(length=2048), nullable=True),
    )
    # IF NOT EXISTS keeps a partially-applied environment recoverable rather
    # than failing on a label that is already there.
    op.execute("ALTER TYPE sequence_message_purpose ADD VALUE IF NOT EXISTS 'VALUE_RESOURCE'")


def downgrade() -> None:
    """Reverse cleanly on an empty schema; refuse once there is something to lose.

    Two separate refusals, for two separate reasons.

    A message already labelled ``VALUE_RESOURCE`` cannot survive the type
    rebuild below. It could be cast to ``CONCISE_REMINDER`` — the position is
    the same and the unique constraint would not collide, because no sequence
    holds both — and that is precisely what this migration must not do. Email 2
    of those sequences contains a report link and was written to introduce it;
    calling it a concise reminder afterwards would leave the record describing a
    message that was never generated.

    A Report URL is likewise the only copy of itself. It is short and an
    operator could retype it, but nothing in the audit trail stores the value —
    ``campaign.updated`` records which fields changed, not what they became — so
    dropping the column silently loses every Campaign's report address with no
    way to recover it from the database.

    Both refusals are conditional on there being something to protect, so a
    database that never used either addition reverses without ceremony. That is
    what keeps the migration round-trip check meaningful.
    """

    bind = op.get_bind()

    resourced = bind.execute(
        sa.text("SELECT count(*) FROM email_sequence_messages WHERE purpose = 'VALUE_RESOURCE'")
    ).scalar_one()
    if resourced:
        raise RuntimeError(
            f"SEQ-002 (e4b1c7f92a30) will not downgrade while {resourced} sequence "
            "message(s) still use the VALUE_RESOURCE purpose it added. Reversing would "
            "have to relabel them as concise reminders, which would misreport what "
            "those emails actually say. Restore from a backup taken before the upgrade "
            "instead."
        )

    reports = bind.execute(
        sa.text("SELECT count(*) FROM campaigns WHERE campaign_resource_url IS NOT NULL")
    ).scalar_one()
    if reports:
        raise RuntimeError(
            f"SEQ-002 (e4b1c7f92a30) will not downgrade while {reports} campaign(s) "
            "carry a Report URL. The column is the only record of that address — the "
            "audit trail names the field, not its value — so dropping it would lose "
            "them silently. Clear the Report URL on those Campaigns first, or restore "
            "from a backup."
        )

    op.drop_column("campaigns", "campaign_resource_url")

    # Rebuild the ENUM without VALUE_RESOURCE. Safe only because the refusal
    # above proved no row references it.
    op.execute("ALTER TYPE sequence_message_purpose RENAME TO sequence_message_purpose_old")
    sa.Enum(*_OLD_PURPOSES, name="sequence_message_purpose").create(bind)
    op.execute(
        "ALTER TABLE email_sequence_messages "
        "ALTER COLUMN purpose TYPE sequence_message_purpose "
        "USING purpose::text::sequence_message_purpose"
    )
    op.execute("DROP TYPE sequence_message_purpose_old")
