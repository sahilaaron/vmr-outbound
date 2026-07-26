"""APP-002 widen label and note anchors for the contact CRM

Revision ID: 4b7d1e92c530
Revises: 26f8ab7044f1
Create Date: 2026-07-26 12:40:00.000000

DAT-013 anchored every label assignment to a permanent contact and every note to
a capture. The contact CRM needs both anchors to work in either direction:

* a capture that is still awaiting company-domain resolution has no contact row,
  yet must remain actionable, so it must be able to carry a label;
* a contact created by a spreadsheet import has no capture, yet must be able to
  carry an operator note.

Both changes are widening. ``NOT NULL`` is dropped, never added; no column is
renamed or removed; no enum is rebuilt; no data is rewritten. Every existing row
stays valid exactly as it is.

The anchor check on ``contact_label_assignments`` is an inclusive OR rather than
an exclusive one. ``capture_id`` there already records *which capture produced
the label* — it is provenance, not an alternative anchor — so a row may
legitimately carry both columns. An exclusive check would reject rows the
previous migration deliberately created.

The unique guarantee is preserved by splitting one constraint into two partial
indexes, one per anchor space, so contact-anchored and capture-anchored
assignments cannot collide with each other.

Downgrade is a true inverse, but it refuses to run when rows exist that only the
widened schema permits. Silently deleting an operator's labels or notes to make
a downgrade succeed would be data loss disguised as a rollback.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "4b7d1e92c530"
down_revision: str | Sequence[str] | None = "26f8ab7044f1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # --- labels: allow a capture-anchored assignment -----------------------
    op.alter_column(
        "contact_label_assignments",
        "contact_id",
        existing_type=sa.UUID(),
        nullable=True,
    )
    op.create_check_constraint(
        "ck_contact_label_assignments_anchor",
        "contact_label_assignments",
        "contact_id IS NOT NULL OR capture_id IS NOT NULL",
    )
    # One constraint covering a single anchor space becomes two partial indexes
    # covering both. A contact-anchored row is unique on (contact_id, label_id);
    # a capture-anchored row is unique on (capture_id, label_id).
    op.drop_constraint(
        "uq_contact_label_assignments_contact_id",
        "contact_label_assignments",
        type_="unique",
    )
    op.create_index(
        "uq_contact_label_assignments_contact",
        "contact_label_assignments",
        ["contact_id", "label_id"],
        unique=True,
        postgresql_where=sa.text("contact_id IS NOT NULL"),
    )
    op.create_index(
        "uq_contact_label_assignments_capture",
        "contact_label_assignments",
        ["capture_id", "label_id"],
        unique=True,
        postgresql_where=sa.text("contact_id IS NULL"),
    )

    # --- notes: allow a contact-anchored note with no capture --------------
    op.alter_column(
        "contact_capture_notes",
        "capture_id",
        existing_type=sa.UUID(),
        nullable=True,
    )
    op.create_check_constraint(
        "ck_contact_capture_notes_anchor",
        "contact_capture_notes",
        "capture_id IS NOT NULL OR contact_id IS NOT NULL",
    )


def downgrade() -> None:
    bind = op.get_bind()

    # Refuse rather than destroy. These are the rows that only exist because of
    # the widening, so restoring NOT NULL would have to delete them.
    orphan_labels = bind.execute(
        sa.text("SELECT count(*) FROM contact_label_assignments WHERE contact_id IS NULL")
    ).scalar_one()
    if orphan_labels:
        raise RuntimeError(
            f"Cannot downgrade: {orphan_labels} capture-anchored label assignment(s) exist. "
            "Promote the captures to contacts or remove those assignments first — "
            "this migration will not delete them for you."
        )

    orphan_notes = bind.execute(
        sa.text("SELECT count(*) FROM contact_capture_notes WHERE capture_id IS NULL")
    ).scalar_one()
    if orphan_notes:
        raise RuntimeError(
            f"Cannot downgrade: {orphan_notes} contact-only note(s) exist. "
            "Notes are append-only; remove them deliberately before downgrading — "
            "this migration will not delete them for you."
        )

    op.drop_constraint("ck_contact_capture_notes_anchor", "contact_capture_notes", type_="check")
    op.alter_column(
        "contact_capture_notes",
        "capture_id",
        existing_type=sa.UUID(),
        nullable=False,
    )

    op.drop_index("uq_contact_label_assignments_capture", table_name="contact_label_assignments")
    op.drop_index("uq_contact_label_assignments_contact", table_name="contact_label_assignments")
    op.create_unique_constraint(
        "uq_contact_label_assignments_contact_id",
        "contact_label_assignments",
        ["contact_id", "label_id"],
    )
    op.drop_constraint(
        "ck_contact_label_assignments_anchor",
        "contact_label_assignments",
        type_="check",
    )
    op.alter_column(
        "contact_label_assignments",
        "contact_id",
        existing_type=sa.UUID(),
        nullable=False,
    )
