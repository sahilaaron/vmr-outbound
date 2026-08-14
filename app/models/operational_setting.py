"""Durable, administrator-controlled operational settings.

One row per operator product control. The table is small and deliberately dull;
the interesting part is what is *not* here.

What belongs in this table
--------------------------
Ordinary product operation: whether Company Research runs, whether the
MillionVerifier provider may be called, whether Insights or Personalization or
Gmail Drafts are in use. These are decisions an operator makes while running the
product, and they change on a Tuesday afternoon for a reason that has nothing to
do with the deployment.

Before this table existed all of them were ``FEATURES__*`` environment variables,
which meant every one of those decisions required SSH access, an edit to
``/etc/vmr/vmr.env`` and a service restart. Hosted Beta UAT found the cost of
that directly: Agent controls were enabled, Research jobs were paused with
``feature_disabled``, and the only fix was a deploy. An administrator should not
need a shell to operate the product.

What does **not** belong here, and never will
---------------------------------------------
Anything that is a deployment or security boundary: ``DATABASE_URL``, OAuth
client secrets, provider API keys, encryption keys, session secrets, trusted
hosts and proxies, ``APP_ENV``, ``DRY_RUN``, and the local-only intake switches
whose entire safety argument is the startup validation in
``app/core/runtime.py``. Those stay in the environment, are shown read-only, and
have no write path from the Admin UI at all —
``app/services/operations/settings.py`` enumerates the two sets and refuses a
write to anything outside the first.

Why a mutable row rather than the immutable version + activation ledger
-----------------------------------------------------------------------
Three other config families in this codebase (personalization policy, the
verification waterfall, the email pattern policy) use an append-only version
table plus an activation table, because each version is a *document* an operator
composed and might want to roll back to as a whole.

A switch is not a document. It has two states, and "roll back to the previous
version of ON" is not a thing anybody needs. What is needed is who changed it,
when, and why — and that is the audit event this table's service writes on every
change, which is the same trail every other operator action in the application
leaves. ``version`` is here for optimistic concurrency, exactly as
``agent_controls.version`` is, so two administrators on the same screen cannot
silently overwrite each other.

A missing row is not "off". It means nobody has expressed an opinion yet, and
the resolver falls back to the environment default, so this table can be created
empty on an existing deployment without changing a single behaviour.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class OperationalSetting(Base):
    """One administrator-controlled product switch."""

    __tablename__ = "operational_settings"

    #: The feature-flag name this row overrides, e.g. ``company_research``. It is
    #: the primary key, so the table is a set of opinions rather than a log, and
    #: an unknown key cannot be inserted by the service that validates against
    #: the registry in ``app/services/operations/settings.py``.
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False)
    #: What the administrator said when they changed it. Shown back on the screen
    #: and recorded in the audit event, because "who turned Research off" is a
    #: question somebody always asks a week later.
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )
    #: Bumped on every write. The Admin form submits the value it rendered, so a
    #: second administrator's change is reported as a conflict rather than
    #: silently discarded — the same rule ``agent_controls`` already uses.
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"OperationalSetting(key={self.key!r}, enabled={self.enabled!r})"
