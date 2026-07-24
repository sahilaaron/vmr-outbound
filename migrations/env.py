"""Alembic environment.

The database URL and target metadata come from the application, so migrations
always match the app's configuration (no duplicated connection strings, no
credentials in source).
"""

from logging.config import fileConfig

# Importing the declarative base registers every model on Base.metadata (base.py
# imports all model modules), so autogenerate sees the full schema.
import app.db.base  # noqa: E402, F401
from alembic import context
from app.core.config import get_settings
from app.db.base import Base
from app.db.safety import ensure_migration_allowed
from sqlalchemy import engine_from_config, pool

# Alembic Config object, providing access to the .ini file values.
config = context.config

# Interpret the config file for Python logging.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Inject the application's database URL at runtime (never hard-coded in the ini).
config.set_main_option("sqlalchemy.url", get_settings().database_url)


def _alembic_command_name() -> str | None:
    """The alembic CLI subcommand being executed (None when run programmatically)."""

    cmd = getattr(config.cmd_opts, "cmd", None)
    if not cmd:
        return None
    fn = cmd[0]
    name = getattr(fn, "__name__", None)
    return name if isinstance(name, str) else None


# FND-009 guard: migrations run freely against a loopback database; against any
# non-loopback host they run only through the deliberate operator command
# (scripts/rds_migrate.py, which sets a one-shot token), and `downgrade` is
# refused against a non-loopback host unconditionally. Fails closed with a
# masked message before any connection is attempted.
ensure_migration_allowed(get_settings().database_url, command=_alembic_command_name())

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode (emit SQL, no DBAPI required)."""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode against a live connection."""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
