"""Merge the model-domain and campaign-offering migration heads.

Revision ID: d6a7c4b9e201
Revises: c1f7a9e34b06, c3e8b1d47a95
Create Date: 2026-08-18 12:20:00.000000

Both feature migrations were developed from the same prior head and landed
independently. This no-op merge revision restores one linear Alembic head
without rewriting either feature migration or changing application data.
"""

from __future__ import annotations

from collections.abc import Sequence

revision: str = "d6a7c4b9e201"
down_revision: str | Sequence[str] | None = ("c1f7a9e34b06", "c3e8b1d47a95")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
