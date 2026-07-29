"""Include the committed LEASED state in the active exact-email job invariant.

Revision ID: 8f0a3d6c2b91
Revises: 4c8e1b2d9a70
Create Date: 2026-07-29

The preceding migration adds the enum label inside an explicit autocommit
block. This follow-up keeps the index replacement after that boundary and
independently reviewable.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "8f0a3d6c2b91"
down_revision: str | Sequence[str] | None = "4c8e1b2d9a70"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_WITH_LEASED = (
    "status IN ("
    "'PENDING'::verification_job_status,"
    "'LEASED'::verification_job_status,"
    "'IN_PROGRESS'::verification_job_status,"
    "'RETRY_SCHEDULED'::verification_job_status)"
)
_LEGACY = (
    "status IN ("
    "'PENDING'::verification_job_status,"
    "'IN_PROGRESS'::verification_job_status,"
    "'RETRY_SCHEDULED'::verification_job_status)"
)


def upgrade() -> None:
    op.drop_index("uq_verification_jobs_active_email", table_name="verification_jobs")
    op.create_index(
        "uq_verification_jobs_active_email",
        "verification_jobs",
        ["email"],
        unique=True,
        postgresql_where=_WITH_LEASED,
    )


def downgrade() -> None:
    op.drop_index("uq_verification_jobs_active_email", table_name="verification_jobs")
    op.create_index(
        "uq_verification_jobs_active_email",
        "verification_jobs",
        ["email"],
        unique=True,
        postgresql_where=_LEGACY,
    )
