"""Making a database URL safe to hand to Alembic's ini-backed config.

This lives in ``app.db`` rather than in ``migrations/env.py`` because
``env.py`` can only be imported inside an Alembic run — it touches
``alembic.context``, which does not exist otherwise. Keeping the rule here means
it can be unit tested directly, which matters: it is one line of string
manipulation guarding a failure mode that only shows up with a particular shape
of password.
"""

from __future__ import annotations


def escape_for_alembic_config(url: str) -> str:
    """Double every ``%`` so ConfigParser hands the URL back unchanged.

    Alembic stores ``sqlalchemy.url`` in a :class:`configparser.ConfigParser`,
    which performs ``%``-interpolation on the values it reads back. A
    percent-encoded credential — ``dbPost%232026`` for a password containing
    ``#`` — makes ConfigParser try to interpolate ``%23`` and raise::

        ValueError: invalid interpolation syntax

    Doubling on the way in means interpolation collapses it back to a single
    ``%`` on the way out, so the URL SQLAlchemy finally receives is
    byte-for-byte the one the application configured.

    This escaping exists *only* for that config round trip. It must never be
    applied to a URL passed straight to :func:`sqlalchemy.create_engine`, which
    would then try to connect using a literally doubled percent in the password.
    """

    return url.replace("%", "%%")
