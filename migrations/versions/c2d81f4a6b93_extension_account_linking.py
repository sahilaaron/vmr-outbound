"""Account-linked extension authorization

Revision ID: c2d81f4a6b93
Revises: b45732880eff
Create Date: 2026-08-13 09:20:00.000000

Two new tables and nothing else. No existing table gains a column, no constraint
is relaxed, no row is read and no historical activity is attributed to anybody.

## What this replaces, and what it does not

``EXTENSION_AUTH__CREDENTIALS`` — a list of ``<key_id>:<sha256>`` entries in the
environment file — could say "this install may capture". It could not say *whose*
capture it is, could not be revoked without a restart, and required a human to
paste a permanent shared secret into a browser. This revision makes each of those
a property of a row.

It does **not** widen what an extension may reach. The enumerated capture
contract in ``app/core/auth/extension.py`` is untouched: four routes, three of
them reads. This revision changes who a capture belongs to and how the
authorization is obtained and ended, not what it is worth.

The legacy configuration path stays for local development and is inert outside
``APP_ENV=local``, so nothing here has to be back-filled: there is no meaningful
mapping from a configured key id to a VMR account, and inventing one would make
the database assert an ownership nobody ever stated.

## Why the partial unique index is the design

``uq_extension_sessions_live_install`` is unique over
``(user_id, extension_id, installation_id)`` *where* ``revoked_at IS NULL``. That
is the "one live link per install per account" rule stated where it cannot be
forgotten by an application path. Revoked rows accumulate freely — they are the
audit trail of every disconnect, every rotation replacement and every detected
refresh-token reuse — while exactly one row per install may be live.

Written as a raw ``CREATE UNIQUE INDEX ... WHERE`` because a partial index is not
expressible as a table constraint in Postgres, which is also why the ORM declares
it with ``postgresql_where`` rather than as a ``UniqueConstraint``.

## Why both digest columns are ``varchar(64)``

They hold SHA-256 hex and nothing else. SHA-256 rather than a password KDF for
the reason stated on ``user_credential_tokens``: the input is 256 bits of
``secrets.token_urlsafe`` entropy, so there is no dictionary to slow down, and
paying a KDF's cost on every capture request would buy nothing.

## Downgrade

Drops both tables. Complete and safe — nothing outside them references either,
because no foreign key was added to an existing table. The honest consequence is
that every connected extension stops working and each operator has to reconnect
once; no capture evidence is touched, since captures live in their own tables and
carry no reference to the link that submitted them.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "c2d81f4a6b93"
down_revision = "b45732880eff"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "extension_sessions",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("extension_id", sa.String(length=32), nullable=False),
        sa.Column("installation_id", sa.String(length=64), nullable=False),
        sa.Column("scope", sa.String(length=32), server_default="capture", nullable=False),
        sa.Column("access_token_hash", sa.String(length=64), nullable=False),
        sa.Column("access_token_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("refresh_token_hash", sa.String(length=64), nullable=False),
        sa.Column("refresh_token_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("label", sa.String(length=120), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_reason", sa.String(length=160), nullable=True),
        # CASCADE, deliberately: deleting an account must not leave an
        # authorization behind that names a user id nothing resolves.
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="fk_extension_sessions_user_id_users",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_extension_sessions"),
    )
    op.create_index("ix_extension_sessions_user_id", "extension_sessions", ["user_id"])
    op.execute(
        "CREATE UNIQUE INDEX uq_extension_sessions_live_install "
        "ON extension_sessions (user_id, extension_id, installation_id) "
        "WHERE revoked_at IS NULL"
    )

    op.create_table(
        "extension_authorization_codes",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("code_hash", sa.String(length=64), nullable=False),
        sa.Column("extension_id", sa.String(length=32), nullable=False),
        sa.Column("installation_id", sa.String(length=64), nullable=False),
        sa.Column("code_challenge", sa.String(length=128), nullable=False),
        sa.Column("redirect_uri", sa.String(length=255), nullable=False),
        sa.Column("scope", sa.String(length=32), server_default="capture", nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="fk_extension_authorization_codes_user_id_users",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_extension_authorization_codes"),
    )
    # Presented codes are looked up by digest and never by the secret itself; the
    # index is unique so a digest collision cannot produce two candidate rows.
    op.create_index(
        "ix_extension_authorization_codes_hash",
        "extension_authorization_codes",
        ["code_hash"],
        unique=True,
    )
    op.create_index(
        "ix_extension_authorization_codes_user_id",
        "extension_authorization_codes",
        ["user_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_extension_authorization_codes_user_id", table_name="extension_authorization_codes"
    )
    op.drop_index(
        "ix_extension_authorization_codes_hash", table_name="extension_authorization_codes"
    )
    op.drop_table("extension_authorization_codes")
    op.execute("DROP INDEX IF EXISTS uq_extension_sessions_live_install")
    op.drop_index("ix_extension_sessions_user_id", table_name="extension_sessions")
    op.drop_table("extension_sessions")
