"""SQLAlchemy declarative base and shared metadata conventions.

A consistent constraint naming convention keeps Alembic autogenerate stable and
migrations reversible (AGENTS.md: "Use database migrations").
"""

from __future__ import annotations

from sqlalchemy import MetaData
from sqlalchemy.orm import DeclarativeBase

NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    """Declarative base for all ORM models."""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)


# Import models here so that ``Base.metadata`` is fully populated for Alembic
# autogenerate and ``create_all`` in tests. Keep this list current as models
# are added in later phases.
from app.models import audit_event as _audit_event  # noqa: E402,F401
from app.models import campaign as _campaign  # noqa: E402,F401
from app.models import company as _company  # noqa: E402,F401
from app.models import contact as _contact  # noqa: E402,F401
from app.models import draft as _draft  # noqa: E402,F401
from app.models import email_evidence as _email_evidence  # noqa: E402,F401
from app.models import external_event as _external_event  # noqa: E402,F401
from app.models import import_batch as _import_batch  # noqa: E402,F401
from app.models import insight as _insight  # noqa: E402,F401
from app.models import provenance as _provenance  # noqa: E402,F401
from app.models import score as _score  # noqa: E402,F401
from app.models import suppression as _suppression  # noqa: E402,F401
