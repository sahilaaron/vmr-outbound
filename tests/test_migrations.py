"""Migration round-trip test (DAT-001).

Runs ``upgrade head`` -> ``check`` -> ``downgrade base`` -> ``upgrade head`` against
a throwaway database via the ``alembic`` CLI, mirroring the CI step. Using a
dedicated database keeps the destructive downgrade away from the shared test
schema.
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from app.core.config import get_settings
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Connection, make_url

REPO_ROOT = Path(__file__).resolve().parents[1]


def _load_migration(revision_filename: str) -> object:
    """Import a migration module by path.

    The backfill statement is asserted against the real one rather than a copy,
    so a change to the migration cannot leave the test quietly passing against
    SQL that is no longer shipped.
    """

    path = REPO_ROOT / "migrations" / "versions" / revision_filename
    spec = importlib.util.spec_from_file_location(path.stem, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_BACKFILL_SQL = str(_load_migration("c48b1f70a3d2_app_003_company_workspace.py")._BACKFILL)

#: The revision immediately BELOW APP-003. Downgrading *to* it is what runs
#: APP-003's own downgrade, named explicitly so the test keeps exercising that
#: migration as later ones stack on top of it.
_APP_003_PARENT = _load_migration("c48b1f70a3d2_app_003_company_workspace.py").down_revision

#: The revision immediately below DAT-017A, for the same reason.
_DAT_017A_PARENT = _load_migration(
    "d7a3f18c62b4_dat_017a_company_domain_resolution.py"
).down_revision

#: The revision immediately below KB-001, for the same reason.
_KB_001_PARENT = _load_migration("b8e5d34a91c7_kb_001_seller_knowledge_base.py").down_revision


@pytest.fixture()
def temp_database_url() -> Iterator[str]:
    """Create and drop an isolated database for a migration round trip."""

    base = make_url(get_settings().database_url)
    name = f"vmr_mig_{uuid.uuid4().hex[:12]}"
    admin_url = base.set(database="postgres")

    admin_engine = create_engine(admin_url, isolation_level="AUTOCOMMIT")
    with admin_engine.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{name}"'))
    admin_engine.dispose()

    try:
        yield base.set(database=name).render_as_string(hide_password=False)
    finally:
        admin_engine = create_engine(admin_url, isolation_level="AUTOCOMMIT")
        with admin_engine.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin_engine.dispose()


def _alembic(args: list[str], database_url: str) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "DATABASE_URL": database_url, "PYTHONPATH": str(REPO_ROOT)}
    return subprocess.run(
        ["alembic", *args],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
    )


def test_migration_upgrade_check_downgrade_reupgrade(temp_database_url: str) -> None:
    for args in (
        ["upgrade", "head"],
        ["check"],
        ["downgrade", "base"],
        ["upgrade", "head"],
    ):
        result = _alembic(args, temp_database_url)
        assert result.returncode == 0, (
            f"alembic {' '.join(args)} failed:\n{result.stdout}\n{result.stderr}"
        )


def _seed_company(conn: Connection, *, name: str, domain: str | None) -> uuid.UUID:
    company_id = uuid.uuid4()
    conn.execute(
        text("INSERT INTO companies (id, name, domain) VALUES (:id, :n, :d)"),
        {"id": company_id, "n": name, "d": domain},
    )
    return company_id


def _seed_contact(conn: Connection, *, first: str, domain: str) -> uuid.UUID:
    contact_id = uuid.uuid4()
    conn.execute(
        text(
            "INSERT INTO contacts (id, first_name, last_name, company_name, company_domain, "
            " natural_key) VALUES (:id, :f, 'Tester', 'Seed Co', :d, :k)"
        ),
        {"id": contact_id, "f": first, "d": domain, "k": f"{first.casefold()}|tester|{domain}"},
    )
    return contact_id


def _company_id_of(conn: Connection, contact_id: uuid.UUID) -> uuid.UUID | None:
    return conn.execute(
        text("SELECT company_id FROM contacts WHERE id = :id"), {"id": contact_id}
    ).scalar_one()


def test_app_003_backfill_links_only_unambiguous_contacts(temp_database_url: str) -> None:
    """APP-003 links a contact to a company only when the evidence settles it.

    Every case here was a decision rather than an oversight. A contact whose
    domain matches nothing, matches two companies, or is blank is left NULL,
    because an unlinked contact stays visible and reviewable afterwards while a
    wrongly linked one does not. The rerun case matters just as much: a
    migration that can be applied twice must not overwrite a link an operator
    or a later process already made.
    """

    # Stop one migration short of APP-003, seed the pre-migration world, then
    # apply APP-003 so the backfill runs over real rows.
    assert _alembic(["upgrade", "a5feeb1bb50a"], temp_database_url).returncode == 0

    engine = create_engine(temp_database_url)
    try:
        with engine.begin() as conn:
            unique_company = _seed_company(conn, name="Unique Co", domain="unique.example")
            unique_contact = _seed_contact(conn, first="Unique", domain="unique.example")
            no_match_contact = _seed_contact(conn, first="Nomatch", domain="absent.example")
            blank_contact = _seed_contact(conn, first="Blank", domain="   ")

            # Two companies on one domain. Possible because the unique index is
            # partial and historical rows predate it; inserted directly so the
            # ambiguous case is real rather than hypothetical.
            conn.execute(text("DROP INDEX IF EXISTS uq_companies_domain"))
            _seed_company(conn, name="Dup One", domain="dup.example")
            _seed_company(conn, name="Dup Two", domain="dup.example")
            ambiguous_contact = _seed_contact(conn, first="Ambiguous", domain="dup.example")

        result = _alembic(["upgrade", "head"], temp_database_url)
        assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"

        with engine.begin() as conn:
            assert _company_id_of(conn, unique_contact) == unique_company, (
                "exactly one company carries the domain, so the link is unambiguous"
            )
            assert _company_id_of(conn, no_match_contact) is None, (
                "no company carries that domain; a link would be invented"
            )
            assert _company_id_of(conn, ambiguous_contact) is None, (
                "two companies carry that domain; picking one would be a guess"
            )
            assert _company_id_of(conn, blank_contact) is None, (
                "a blank domain cannot identify a company"
            )

        # Rerunning must preserve an existing link rather than recompute it.
        with engine.begin() as conn:
            other_company = _seed_company(conn, name="Chosen By Hand", domain="chosen.example")
            conn.execute(
                text("UPDATE contacts SET company_id = :c WHERE id = :id"),
                {"c": other_company, "id": unique_contact},
            )
            conn.execute(text(_BACKFILL_SQL))
            assert _company_id_of(conn, unique_contact) == other_company, (
                "a rerun must not overwrite a link that already exists"
            )

            # And a rerun still links anything that has since become unambiguous.
            _seed_company(conn, name="Late Co", domain="absent.example")
            conn.execute(text(_BACKFILL_SQL))
            assert _company_id_of(conn, no_match_contact) is not None
    finally:
        engine.dispose()


def test_app_003_downgrade_refuses_while_the_workspace_holds_data(
    temp_database_url: str,
) -> None:
    """The reversal is conditional on there being something to lose.

    An empty schema reverses without ceremony — that is what keeps the round
    trip above meaningful. A database holding contact links, field provenance or
    dossiers stops, because none of it can be re-derived and a rebuilt link is a
    guess rather than the decision somebody made.
    """

    assert _alembic(["upgrade", "head"], temp_database_url).returncode == 0

    engine = create_engine(temp_database_url)
    try:
        with engine.begin() as conn:
            company_id = _seed_company(conn, name="Held", domain="held.example")
            contact_id = _seed_contact(conn, first="Held", domain="held.example")
            conn.execute(
                text("UPDATE contacts SET company_id = :c WHERE id = :id"),
                {"c": company_id, "id": contact_id},
            )

        # Target APP-003 by revision rather than by "one step below head". Later
        # migrations stack on top of it, so a relative step would silently start
        # testing whichever migration happens to be newest instead of this one.
        blocked = _alembic(["downgrade", _APP_003_PARENT], temp_database_url)
        assert blocked.returncode != 0, "the downgrade must refuse while a link exists"
        assert "contact-to-company link" in (blocked.stdout + blocked.stderr)

        with engine.begin() as conn:
            conn.execute(text("UPDATE contacts SET company_id = NULL"))

        cleared = _alembic(["downgrade", _APP_003_PARENT], temp_database_url)
        assert cleared.returncode == 0, f"{cleared.stdout}\n{cleared.stderr}"
    finally:
        engine.dispose()


def _seed_capture(conn: Connection) -> uuid.UUID:
    """A minimal capture row — the anchor a resolution decision hangs from."""

    capture_id = uuid.uuid4()
    conn.execute(
        text(
            "INSERT INTO linkedin_profile_snapshots "
            " (id, client_capture_id, content_hash, schema_version, source, "
            "  extraction_status, payload, profile_fields, outcome) "
            "VALUES (:id, :c, :h, '1.0.0', 'test', 'ok', '{}'::jsonb, '{}'::jsonb, "
            # PostgreSQL stores the enum LABEL, which is the member NAME.
            " 'UNMATCHED_STAGED')"
        ),
        {"id": capture_id, "c": str(capture_id), "h": uuid.uuid4().hex},
    )
    return capture_id


def test_dat_017a_downgrade_refuses_while_resolution_decisions_exist(
    temp_database_url: str,
) -> None:
    """A decision cannot be re-derived, so the reversal refuses rather than drops it.

    A resolution decision is the only record of which evidence produced a company
    link, how certain it was, what the provider offered, and what an operator
    changed. Today's evidence is not the evidence the decision was made on, so
    rebuilding one is impossible and dropping one is silent data loss.

    As with APP-003, the refusal is conditional on there being something to
    protect — an empty schema reverses cleanly, which is what keeps the round
    trip above meaningful.
    """

    assert _alembic(["upgrade", "head"], temp_database_url).returncode == 0

    engine = create_engine(temp_database_url)
    try:
        with engine.begin() as conn:
            capture_id = _seed_capture(conn)
            conn.execute(
                text(
                    "INSERT INTO company_domain_resolutions "
                    " (id, capture_id, decision_number, is_current, state, decision_kind, "
                    "  policy_version, selected_domain, reasons) "
                    "VALUES (:id, :cap, 1, true, 'PROVISIONAL', 'AUTOMATIC', "
                    " 'company-domain-resolution/practical-v1', 'seed.example', "
                    " '[\"single_aligned_provider_candidate\"]'::jsonb)"
                ),
                {"id": uuid.uuid4(), "cap": capture_id},
            )

        blocked = _alembic(["downgrade", _DAT_017A_PARENT], temp_database_url)
        assert blocked.returncode != 0, "the downgrade must refuse while a decision exists"
        assert "resolution decision" in (blocked.stdout + blocked.stderr)

        with engine.begin() as conn:
            conn.execute(text("DELETE FROM company_domain_resolutions"))

        cleared = _alembic(["downgrade", _DAT_017A_PARENT], temp_database_url)
        assert cleared.returncode == 0, f"{cleared.stdout}\n{cleared.stderr}"
    finally:
        engine.dispose()


def test_dat_017a_downgrade_refuses_while_a_promotion_uses_the_new_enum_label(
    temp_database_url: str,
) -> None:
    """Rebuilding the enum without ``DOMAIN_PROVISIONAL`` must not silently recast a row.

    The downgrade recreates ``company_resolution_outcome`` without the label it
    added. A promotion still carrying that label could not survive the cast, so
    the migration checks for one first and stops with a readable message rather
    than failing halfway through on a type error.
    """

    assert _alembic(["upgrade", "head"], temp_database_url).returncode == 0

    engine = create_engine(temp_database_url)
    try:
        with engine.begin() as conn:
            capture_id = _seed_capture(conn)
            conn.execute(
                text(
                    "INSERT INTO contact_capture_promotions "
                    " (id, capture_id, company_outcome, contact_outcome, notes_linked) "
                    "VALUES (:id, :cap, 'DOMAIN_PROVISIONAL', 'PENDING', 0)"
                ),
                {"id": uuid.uuid4(), "cap": capture_id},
            )

        blocked = _alembic(["downgrade", _DAT_017A_PARENT], temp_database_url)
        assert blocked.returncode != 0, "the downgrade must refuse while the label is in use"
        assert "labels it added" in (blocked.stdout + blocked.stderr)
    finally:
        engine.dispose()


def test_kb_001_downgrade_refuses_while_the_knowledge_base_holds_typed_content(
    temp_database_url: str,
) -> None:
    """Operator-typed seller knowledge exists nowhere else, so the reversal refuses.

    A proof point or a positioning paragraph did not come from an import, a
    provider, or a captured page - a person wrote it. There is nothing to
    recompute it from, so dropping the tables would be silent, unrecoverable
    data loss rather than a reversible schema change.

    As with APP-003 and DAT-017A, the refusal is conditional on there being
    something to protect: an empty schema reverses cleanly, which is what keeps
    the round-trip test above meaningful.
    """

    assert _alembic(["upgrade", "head"], temp_database_url).returncode == 0

    engine = create_engine(temp_database_url)
    try:
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO seller_proof_points (id, statement, state) "
                    "VALUES (:id, :statement, 'ACTIVE')"
                ),
                {"id": uuid.uuid4(), "statement": "Covering this market since 2009."},
            )

        blocked = _alembic(["downgrade", _KB_001_PARENT], temp_database_url)
        assert blocked.returncode != 0, "the downgrade must refuse while typed content exists"
        assert "operator-entered content" in (blocked.stdout + blocked.stderr)

        with engine.begin() as conn:
            conn.execute(text("DELETE FROM seller_proof_points"))

        cleared = _alembic(["downgrade", _KB_001_PARENT], temp_database_url)
        assert cleared.returncode == 0, f"{cleared.stdout}\n{cleared.stderr}"
    finally:
        engine.dispose()


def test_kb_001_campaign_offerings_survive_an_archived_offering(
    temp_database_url: str,
) -> None:
    """Archiving is a state flip, so the association a campaign made still resolves.

    Asserted at the database level rather than through the service, because it
    is the schema - no delete path, and a foreign key to a row that is never
    removed - that keeps a historical campaign intact, not a convention the
    service layer happens to follow.
    """

    assert _alembic(["upgrade", "head"], temp_database_url).returncode == 0

    engine = create_engine(temp_database_url)
    try:
        campaign_id = uuid.uuid4()
        offering_id = uuid.uuid4()
        with engine.begin() as conn:
            conn.execute(
                text("INSERT INTO campaigns (id, name, status) VALUES (:id, :name, 'DRAFT')"),
                {"id": campaign_id, "name": "KB-001 archive check"},
            )
            conn.execute(
                text(
                    "INSERT INTO seller_offerings (id, name, offering_type, state) "
                    "VALUES (:id, :name, 'RESEARCH_REPORT', 'ACTIVE')"
                ),
                {"id": offering_id, "name": "Cement outlook"},
            )
            conn.execute(
                text(
                    "INSERT INTO campaign_offerings (id, campaign_id, offering_id) "
                    "VALUES (:id, :campaign, :offering)"
                ),
                {"id": uuid.uuid4(), "campaign": campaign_id, "offering": offering_id},
            )
            conn.execute(
                text("UPDATE seller_offerings SET state = 'ARCHIVED' WHERE id = :id"),
                {"id": offering_id},
            )

        with engine.connect() as conn:
            still_linked = conn.execute(
                text(
                    "SELECT o.name, o.state FROM campaign_offerings link "
                    "JOIN seller_offerings o ON o.id = link.offering_id "
                    "WHERE link.campaign_id = :campaign"
                ),
                {"campaign": campaign_id},
            ).all()
        assert still_linked == [("Cement outlook", "ARCHIVED")]
    finally:
        engine.dispose()
