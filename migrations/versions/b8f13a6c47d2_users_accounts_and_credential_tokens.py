"""Hosted Beta user accounts and one-time credential links

Revision ID: b8f13a6c47d2
Revises: 0926b59b7912
Create Date: 2026-08-12 10:40:00.000000

Two new tables and three new enum types. Nothing existing is altered: no column
is added to an existing table, no constraint is relaxed, no row is read, and no
historical activity is attributed to anybody.

## Why nothing is back-filled

The application has been running with operator activity recorded as free-text
actors — ``"system:import"``, an operator email, a service name — and none of
those can be resolved to an account row with any confidence. Guessing would
produce a database that *asserts* Priya imported a batch when all that is known
is that somebody using an address that looks like hers did.

So this revision attributes nothing. Existing ``created_by`` and ``actor``
columns keep their text values and keep meaning exactly what they meant. Issue
#269's per-user attribution starts from the accounts created *after* this
revision, and the historical rows stay honestly unattributed.

## The two unique indexes are the design, not an optimisation

``ix_users_email_normalized`` is what makes "one person is one account" a
database fact. Both sign-in paths resolve against the normalised address, so
without the constraint a race between two administrators creating the same
person, or a Google callback and a password login arriving together, could
produce two rows and two identities for one human being.

``ix_users_google_subject`` does the same job for the provider identity. Google's
``sub`` is stable across a Workspace address rename; the address is not. The
partial-uniqueness behaviour comes free — Postgres treats NULLs as distinct in a
unique index — so any number of accounts may be unlinked while no two may claim
the same Google account.

## Downgrade

Drops both tables and the three enum types. Safe and complete: nothing outside
these tables references them, because no foreign key was added to an existing
table. It does destroy the account directory, which on a hosted deployment means
everybody is signed out and the administrator is recreated at the next start from
``AUTH__BOOTSTRAP_ADMIN_EMAIL``. That is the honest consequence of removing the
authority, and it is why the downgrade is written rather than left as a
``pass``: an operator rolling back needs it to actually undo, not to leave a
half-present schema.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "b8f13a6c47d2"
down_revision = "0926b59b7912"
branch_labels = None
depends_on = None

_USER_ROLE = "user_role"
_USER_STATE = "user_state"
_TOKEN_PURPOSE = "user_credential_token_purpose"


def upgrade() -> None:
    # Labels are the enum *member names*, not their values. That is what
    # SQLAlchemy's `Enum(PythonEnum)` sends by default, and it is what every
    # other enum type in this schema uses (`campaign_status` is `DRAFT`,
    # `ACTIVE`, `ARCHIVED`). Writing the lowercase values here instead produces a
    # schema that migrates cleanly and then rejects every insert.
    user_role = postgresql.ENUM("ADMIN", "USER", name=_USER_ROLE, create_type=False)
    user_state = postgresql.ENUM("ACTIVE", "DISABLED", name=_USER_STATE, create_type=False)
    token_purpose = postgresql.ENUM(
        "INITIAL_SETUP", "RESET", name=_TOKEN_PURPOSE, create_type=False
    )

    bind = op.get_bind()
    user_role.create(bind, checkfirst=True)
    user_state.create(bind, checkfirst=True)
    token_purpose.create(bind, checkfirst=True)

    op.create_table(
        "users",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            nullable=False,
        ),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("email_normalized", sa.String(length=320), nullable=False),
        sa.Column("display_name", sa.String(length=200), nullable=True),
        sa.Column("role", user_role, nullable=False, server_default="USER"),
        sa.Column("state", user_state, nullable=False, server_default="ACTIVE"),
        # Nullable on purpose: an admin-created account has no usable password
        # until its holder sets one. NULL is "cannot authenticate with a
        # password", never "empty password".
        sa.Column("password_hash", sa.Text(), nullable=True),
        sa.Column("password_set_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("google_subject", sa.String(length=255), nullable=True),
        sa.Column("google_linked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("auth_version", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by", sa.String(length=320), nullable=True),
    )
    op.create_index("ix_users_email_normalized", "users", ["email_normalized"], unique=True)
    op.create_index("ix_users_google_subject", "users", ["google_subject"], unique=True)

    op.create_table(
        "user_credential_tokens",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            nullable=False,
        ),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(
                "users.id",
                ondelete="CASCADE",
                name="fk_user_credential_tokens_user_id_users",
            ),
            nullable=False,
        ),
        sa.Column("purpose", token_purpose, nullable=False),
        # A SHA-256 digest of the raw link, hex-encoded. The raw value is never
        # stored, so a database dump contains no usable password link.
        sa.Column("token_digest", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("superseded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("issued_by", sa.String(length=320), nullable=True),
    )
    op.create_index(
        "ix_user_credential_tokens_digest",
        "user_credential_tokens",
        ["token_digest"],
        unique=True,
    )
    op.create_index("ix_user_credential_tokens_user_id", "user_credential_tokens", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_user_credential_tokens_user_id", table_name="user_credential_tokens")
    op.drop_index("ix_user_credential_tokens_digest", table_name="user_credential_tokens")
    op.drop_table("user_credential_tokens")

    op.drop_index("ix_users_google_subject", table_name="users")
    op.drop_index("ix_users_email_normalized", table_name="users")
    op.drop_table("users")

    for type_name in (_TOKEN_PURPOSE, _USER_STATE, _USER_ROLE):
        op.execute(sa.text(f"DROP TYPE IF EXISTS {type_name}"))
