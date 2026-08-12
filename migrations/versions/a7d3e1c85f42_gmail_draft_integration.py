"""Gmail mailbox grants and Gmail draft lineage (#267)

Revision ID: a7d3e1c85f42
Revises: 0926b59b7912
Create Date: 2026-08-12 09:40:00.000000

Two new tables and two new enum types. Nothing existing is altered: no column is
added to the sequence tables, no constraint on them is relaxed, no hosted-auth
or extension-auth table is touched, and no historical row is read or rewritten.
A database that never enables ``FEATURES__GMAIL_DRAFTS`` behaves exactly as it
did before this revision ran.

## The two constraints that carry the safety properties

``uq_gmail_mailbox_grants_connected`` is a *partial* unique index on
``operator_subject WHERE status = 'CONNECTED'``. It makes "one live mailbox per
operator" a database fact rather than an application convention, while leaving
every revoked and reconnect-required row in place for audit -- those rows are
the record of what was authorized and when, and the drafts created under them
still name them.

``uq_gmail_draft_records_account_version`` is unique on
``(mailbox_account_subject, message_version_id)``. This is the idempotency key,
and it is deliberately keyed on Google's stable *account* subject rather than on
the grant row: a disconnect-and-reconnect cycle writes a new grant for the same
mailbox, and keying on the grant would let that cycle produce a second identical
draft in the same human's Drafts folder. Keying on the exact message version --
never on ``(contact, position)`` -- is what makes an edit produce a new draft
while leaving the historical lineage row untouched.

``ck_gmail_draft_records_created_draft_names_its_gmail_id`` is the row-level
statement of the same honesty rule the status enum encodes: only a row that
claims ``CREATED`` may carry a Gmail draft id, and a row that claims it must.
Nothing can record a success it has no evidence for.

## Tokens

``encrypted_refresh_token`` and ``encrypted_access_token`` hold Fernet
ciphertext and nothing else. The key is supplied from the environment
(``GMAIL__TOKEN_ENCRYPTION_KEY``) and is deliberately not derivable from
anything in this schema, so a database copy on its own decrypts neither column.
No other column here holds, hashes or fingerprints a credential.

## Downgrade

Reverses cleanly on an empty schema and refuses once either table holds a row,
following the convention APP-003 (``c48b1f70a3d2``) established and KB-001,
CI-001, DAT-017A and SEQ-001 also follow.

The draft lineage is the only local record of what VMR put in a human's mailbox
and of which exact message version it came from. Dropping it does not remove one
Gmail draft; it removes the ability to answer "has this already been drafted?",
which is precisely what stops the next click creating duplicates. The grant rows
are named too, because a revoked grant is an audit record of a permission that
was once held.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "a7d3e1c85f42"
down_revision = "0926b59b7912"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "gmail_mailbox_grants",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("operator_subject", sa.String(length=255), nullable=False),
        sa.Column("operator_email", sa.String(length=320), nullable=False),
        sa.Column("mailbox_account_subject", sa.String(length=255), nullable=False),
        sa.Column("mailbox_address", sa.String(length=320), nullable=False),
        sa.Column("granted_scopes", sa.String(length=1024), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "CONNECTED",
                "RECONNECT_REQUIRED",
                "REVOKED",
                name="gmail_grant_status",
            ),
            nullable=False,
        ),
        sa.Column("last_error_category", sa.String(length=64), nullable=True),
        sa.Column("encrypted_refresh_token", sa.Text(), nullable=True),
        sa.Column("encrypted_access_token", sa.Text(), nullable=True),
        sa.Column("access_token_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "connected_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("last_refreshed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("disconnected_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "btrim(operator_subject) <> ''",
            name="operator_subject_not_blank",
        ),
        sa.CheckConstraint(
            "btrim(mailbox_address) <> ''",
            name="mailbox_address_not_blank",
        ),
        sa.CheckConstraint(
            "btrim(mailbox_account_subject) <> ''",
            name="mailbox_account_subject_not_blank",
        ),
        sa.CheckConstraint(
            "status <> 'CONNECTED' OR encrypted_refresh_token IS NOT NULL",
            name="connected_grant_has_refresh_token",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_gmail_mailbox_grants"),
    )
    op.create_index(
        "ix_gmail_mailbox_grants_operator", "gmail_mailbox_grants", ["operator_subject"]
    )
    op.create_index(
        "ix_gmail_mailbox_grants_account", "gmail_mailbox_grants", ["mailbox_account_subject"]
    )
    op.create_index(
        "uq_gmail_mailbox_grants_connected",
        "gmail_mailbox_grants",
        ["operator_subject"],
        unique=True,
        postgresql_where=sa.text("status = 'CONNECTED'"),
    )

    op.create_table(
        "gmail_draft_records",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("mailbox_grant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("mailbox_account_subject", sa.String(length=255), nullable=False),
        sa.Column("mailbox_address", sa.String(length=320), nullable=False),
        sa.Column("campaign_contact_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("sequence_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("sequence_key", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("message_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("message_version_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("recipient_email", sa.String(length=320), nullable=False),
        sa.Column("content_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("rfc_message_id", sa.String(length=255), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "RESERVED",
                "CREATED",
                "UNCONFIRMED",
                "FAILED",
                name="gmail_draft_status",
            ),
            nullable=False,
        ),
        sa.Column("failure_category", sa.String(length=64), nullable=True),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("gmail_draft_id", sa.String(length=255), nullable=True),
        sa.Column("gmail_message_id", sa.String(length=255), nullable=True),
        sa.Column("gmail_thread_id", sa.String(length=255), nullable=True),
        sa.Column("created_by", sa.String(length=320), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "position >= 1 AND position <= 7",
            name="position_within_sequence",
        ),
        sa.CheckConstraint(
            "btrim(content_fingerprint) <> ''",
            name="content_fingerprint_not_blank",
        ),
        sa.CheckConstraint(
            "btrim(recipient_email) <> ''",
            name="recipient_email_not_blank",
        ),
        sa.CheckConstraint("attempt_count >= 0", name="attempt_count_not_negative"),
        sa.CheckConstraint(
            "(status = 'CREATED' AND gmail_draft_id IS NOT NULL) "
            "OR (status <> 'CREATED' AND gmail_draft_id IS NULL)",
            name="created_draft_names_its_gmail_id",
        ),
        sa.ForeignKeyConstraint(
            ["mailbox_grant_id"],
            ["gmail_mailbox_grants.id"],
            name="fk_gmail_draft_records_mailbox_grant_id_gmail_mailbox_grants",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["campaign_contact_id"],
            ["campaign_contacts.id"],
            name="fk_gmail_draft_records_campaign_contact_id_campaign_contacts",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["sequence_id"],
            ["email_sequences.id"],
            name="fk_gmail_draft_records_sequence_id_email_sequences",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["message_id"],
            ["email_sequence_messages.id"],
            name="fk_gmail_draft_records_message_id_email_sequence_messages",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["message_version_id"],
            ["email_sequence_message_versions.id"],
            name="fk_gmail_draft_records_message_version_id",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_gmail_draft_records"),
        sa.UniqueConstraint(
            "mailbox_account_subject",
            "message_version_id",
            name="uq_gmail_draft_records_account_version",
        ),
    )
    op.create_index("ix_gmail_draft_records_sequence", "gmail_draft_records", ["sequence_id"])
    op.create_index(
        "ix_gmail_draft_records_campaign_contact", "gmail_draft_records", ["campaign_contact_id"]
    )
    op.create_index("ix_gmail_draft_records_message", "gmail_draft_records", ["message_id"])
    op.create_index("ix_gmail_draft_records_grant", "gmail_draft_records", ["mailbox_grant_id"])


def downgrade() -> None:
    """Reverse cleanly on an empty schema; refuse once there is data to lose."""

    bind = op.get_bind()
    populated: list[str] = []
    for table, label in (
        ("gmail_draft_records", "Gmail draft lineage record(s)"),
        ("gmail_mailbox_grants", "Gmail mailbox authorization record(s)"),
    ):
        # `to_regclass` returns NULL rather than raising when the table is
        # absent, so a partially-applied schema reports "nothing to lose"
        # instead of failing with a confusing undefined-table error.
        exists = bind.execute(
            sa.text("SELECT to_regclass(:name)"), {"name": f"public.{table}"}
        ).scalar()
        if exists is None:
            continue
        count = bind.execute(sa.text(f"SELECT count(*) FROM {table}")).scalar_one()
        if count:
            populated.append(f"{count} {label}")

    if populated:
        raise RuntimeError(
            "#267 (a7d3e1c85f42) will not downgrade while the Gmail tables hold data that "
            "exists nowhere else: "
            + ", ".join(populated)
            + ". The draft lineage is the only local record of which exact message version "
            "produced which Gmail draft, and it is what stops a later click creating a "
            "duplicate in a real mailbox; dropping it removes that answer without removing "
            "one draft from Gmail. Restore from a backup taken before the upgrade instead, "
            "or delete the Gmail data deliberately first if it is genuinely disposable."
        )

    op.drop_index("ix_gmail_draft_records_grant", table_name="gmail_draft_records")
    op.drop_index("ix_gmail_draft_records_message", table_name="gmail_draft_records")
    op.drop_index("ix_gmail_draft_records_campaign_contact", table_name="gmail_draft_records")
    op.drop_index("ix_gmail_draft_records_sequence", table_name="gmail_draft_records")
    op.drop_table("gmail_draft_records")
    op.drop_index(
        "uq_gmail_mailbox_grants_connected",
        table_name="gmail_mailbox_grants",
        postgresql_where=sa.text("status = 'CONNECTED'"),
    )
    op.drop_index("ix_gmail_mailbox_grants_account", table_name="gmail_mailbox_grants")
    op.drop_index("ix_gmail_mailbox_grants_operator", table_name="gmail_mailbox_grants")
    op.drop_table("gmail_mailbox_grants")
    # Dropping a table leaves the enum types it used behind, and a later
    # re-upgrade would then fail on "type already exists".
    for _type_name in ("gmail_draft_status", "gmail_grant_status"):
        op.execute(sa.text(f"DROP TYPE IF EXISTS {_type_name}"))
