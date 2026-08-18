"""Record the evidence behind a model-asserted company domain

Revision ID: c1f7a9e34b06
Revises: a7d3e5f19c22
Create Date: 2026-08-18 09:15:00.000000

The model fallback behind the logo.dev lookup stored what it answered —
``model_domain``, ``model_source_url``, ``model_note`` — and nothing about why
that answer should be believed. The resolution policy therefore accepted a model
domain on two checks: that it parsed as a hostname, and that it was not on the
unsuitable-domain blocklist. For the population this fallback exists to serve —
companies whose name a brand matcher could not match — those two checks are
close to no check at all, and the failure they admit is the specific one that
matters: a confident, well-formed domain belonging to a different company of the
same name in a different country.

This revision adds the one column that lets the policy ask a better question:

``salesnav_company_enrichments.model_claim`` (JSONB, nullable) holds the model's
structured claim — its stated confidence, the pages it says it opened with each
page's parsed host, what it said about competing same-named companies, and a
short auditable reasoning summary. It never holds a prompt, a raw response, or
the model's working.

Additive and nullable, so every existing row is valid unchanged and no data is
migrated. Rows answered before this contract have NULL, which the policy reads
as "unevidenced" and refuses — deliberately, and not a data loss: the domain
those rows assert is still recorded on ``model_domain`` exactly as before, and a
forced re-lookup produces an answer under the new contract. The behaviour change
lives in the policy, not here; this revision only makes the evidence storable.

Downgrade drops the column. That discards evidence for decisions already made,
so it is a real loss of provenance rather than a no-op — but it is a loss of
*newly added* provenance only, and the decisions themselves live in
``company_domain_resolutions`` and are untouched.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "c1f7a9e34b06"
down_revision: str | Sequence[str] | None = "a7d3e5f19c22"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "salesnav_company_enrichments"
_COLUMN = "model_claim"


def upgrade() -> None:
    op.add_column(
        _TABLE,
        sa.Column(_COLUMN, postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )


def downgrade() -> None:
    op.drop_column(_TABLE, _COLUMN)
