"""Gmail mailbox ownership moves to the durable user

Revision ID: b45732880eff
Revises: 40bb1177a2fa
Create Date: 2026-08-12 19:54:11.882430

The Gmail slice (``a7d3e1c85f42``) keyed a mailbox grant on ``operator_subject``
-- Google's ``sub`` for the signed-in operator. That was the only durable
identity a session had when it was written, because every session was then a
Google session.

``b8f13a6c47d2`` changed that. Accounts became rows in ``users``, and an account
can now sign in with a password and never touch Google at all. Such a session
carries ``user_id`` and an **empty** subject -- ``OperatorSession`` says so, and
says the subject is "retained for the audit trail rather than for any access
decision". Left alone, the two slices compose into a Gmail feature that no
password operator can use: the lookups short-circuit on the empty subject, and
connecting would try to insert ``''`` and hit ``operator_subject_not_blank``.

So ownership moves to ``users.id``:

* ``user_id`` becomes the ownership key, and the partial unique index that makes
  "one live mailbox per owner" a database fact moves onto it.
* ``operator_subject`` stays, nullable, as provenance -- which Google sign-in
  authorized this mailbox, when one did. It is no longer read by any
  authorization path.

Existing rows are backfilled through ``users.google_subject``, which is exactly
the same identifier the column already held. A row that cannot be matched to an
account is not guessed at: the migration refuses, because inventing an owner for
a mailbox grant is the one outcome worse than stopping. In practice there are no
such rows anywhere -- the Gmail feature has never been deployed or switched on --
and the backfill exists so that the statement above is enforced rather than
assumed.

``ondelete="RESTRICT"`` matches ``gmail_draft_records.grant_id``: a mailbox grant
is the record of what was authorized, and deleting an account must not silently
erase it.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b45732880eff"
down_revision: str | Sequence[str] | None = "40bb1177a2fa"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "gmail_mailbox_grants",
        sa.Column("user_id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=True),
    )

    # The column already held Google's `sub`; `users.google_subject` holds the
    # same value for the same person. This is a lookup, not a guess.
    op.execute(
        """
        UPDATE gmail_mailbox_grants AS g
           SET user_id = u.id
          FROM users AS u
         WHERE u.google_subject IS NOT NULL
           AND u.google_subject = g.operator_subject
        """
    )

    connection = op.get_bind()
    orphaned = connection.execute(
        sa.text("SELECT count(*) FROM gmail_mailbox_grants WHERE user_id IS NULL")
    ).scalar_one()
    if orphaned:
        raise RuntimeError(
            f"{orphaned} Gmail mailbox grant(s) have no matching account and this migration "
            "will not invent one. Link each operator's Google identity to a user "
            "(users.google_subject) and run this again."
        )

    op.alter_column("gmail_mailbox_grants", "user_id", nullable=False)
    op.create_foreign_key(
        "fk_gmail_mailbox_grants_user_id_users",
        "gmail_mailbox_grants",
        "users",
        ["user_id"],
        ["id"],
        ondelete="RESTRICT",
    )

    # Ownership indexes move to the new key.
    op.drop_index("uq_gmail_mailbox_grants_connected", table_name="gmail_mailbox_grants")
    op.drop_index("ix_gmail_mailbox_grants_operator", table_name="gmail_mailbox_grants")
    op.create_index("ix_gmail_mailbox_grants_operator", "gmail_mailbox_grants", ["user_id"])
    op.create_index(
        "uq_gmail_mailbox_grants_connected",
        "gmail_mailbox_grants",
        ["user_id"],
        unique=True,
        postgresql_where=sa.text("status = 'CONNECTED'"),
    )

    # A password-authorized grant has no Google subject at all.
    op.alter_column("gmail_mailbox_grants", "operator_subject", nullable=True)
    op.drop_constraint("operator_subject_not_blank", "gmail_mailbox_grants", type_="check")
    op.create_check_constraint(
        "operator_subject_not_blank",
        "gmail_mailbox_grants",
        "operator_subject IS NULL OR btrim(operator_subject) <> ''",
    )


def downgrade() -> None:
    # Reversing this re-imposes "a mailbox belongs to a Google subject", which a
    # password-authorized grant cannot satisfy. Such a row is deleted rather
    # than given a fabricated subject: the grant is dead either way once the
    # column it is keyed on cannot hold its owner, and a fabricated subject
    # would be indistinguishable from a real one.
    op.execute("DELETE FROM gmail_mailbox_grants WHERE operator_subject IS NULL")

    op.drop_constraint("operator_subject_not_blank", "gmail_mailbox_grants", type_="check")
    op.create_check_constraint(
        "operator_subject_not_blank",
        "gmail_mailbox_grants",
        "btrim(operator_subject) <> ''",
    )
    op.alter_column("gmail_mailbox_grants", "operator_subject", nullable=False)

    op.drop_index("uq_gmail_mailbox_grants_connected", table_name="gmail_mailbox_grants")
    op.drop_index("ix_gmail_mailbox_grants_operator", table_name="gmail_mailbox_grants")
    op.create_index(
        "ix_gmail_mailbox_grants_operator", "gmail_mailbox_grants", ["operator_subject"]
    )
    op.create_index(
        "uq_gmail_mailbox_grants_connected",
        "gmail_mailbox_grants",
        ["operator_subject"],
        unique=True,
        postgresql_where=sa.text("status = 'CONNECTED'"),
    )

    op.drop_constraint(
        "fk_gmail_mailbox_grants_user_id_users", "gmail_mailbox_grants", type_="foreignkey"
    )
    op.drop_column("gmail_mailbox_grants", "user_id")
