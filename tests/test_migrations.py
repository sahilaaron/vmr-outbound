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

#: The revision immediately below SEQ-001, for the same reason: a relative step
#: would silently start testing whichever migration happens to be newest.
_SEQ_001_PARENT = _load_migration(
    "0926b59b7912_seq_001_seven_message_outreach_sequence.py"
).down_revision

#: The revision immediately below DAT-017A, for the same reason.
_DAT_017A_PARENT = _load_migration(
    "d7a3f18c62b4_dat_017a_company_domain_resolution.py"
).down_revision

#: The revision immediately below KB-001, for the same reason.
_KB_001_PARENT = _load_migration("b8e5d34a91c7_kb_001_seller_knowledge_base.py").down_revision

#: The revision immediately below the source-agnostic resolution subject, for the
#: same reason.
_SUBJECT_PARENT = _load_migration(
    "f4c9a2e70b18_source_agnostic_company_domain_resolution.py"
).down_revision

_INS_002_PARENT = "f2a91d7c4e60"


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


#: PostgreSQL truncates any identifier past this, so a longer name is not a
#: style problem — it is a name the server will never store as written.
_PG_IDENTIFIER_LIMIT = 63


def _metadata_check_constraints() -> dict[str, set[str]]:
    """Every named check constraint the models resolve to, keyed by table."""

    from app.db.base import Base
    from sqlalchemy import CheckConstraint

    found: dict[str, set[str]] = {}
    for table in Base.metadata.tables.values():
        for constraint in table.constraints:
            if isinstance(constraint, CheckConstraint) and constraint.name is not None:
                found.setdefault(table.name, set()).add(str(constraint.name))
    return found


def test_every_check_constraint_name_fits_a_postgres_identifier() -> None:
    """A name over 63 bytes is stored truncated, and then never matches again.

    The failure this prevents is quiet in both directions. SQLAlchemy emits the
    long name, PostgreSQL truncates it and appends a hash of the original, and
    nothing complains — the constraint is created and enforces exactly what was
    asked. What breaks is the *comparison*: the metadata keeps saying
    ``ck_contact_label_assignments_ck_contact_label_assignments_anchor`` while
    the catalog says ``..._ck_contact_label_assignmen_8b26``, and every
    autogenerate run from then on proposes dropping and recreating a constraint
    that is already correct.

    Checked here rather than left to ``alembic check`` because it is a property
    of the models alone: it needs no database, no migration and no particular
    Alembic version, and Alembic did not compare check constraints by name at
    all before 1.19.0. This drift outlived four releases of the tool that was
    supposed to find it.

    The convention prepends ``ck_<table>_``, so the budget for the name given in
    the model is 63 minus that prefix. On a wide table that is genuinely tight —
    ``company_intelligence_classifications`` leaves 23 characters — and the
    answer is a shorter constraint name, not a longer identifier.
    """

    too_long = {
        f"{table}.{name}": len(name)
        for table, names in _metadata_check_constraints().items()
        for name in names
        if len(name) > _PG_IDENTIFIER_LIMIT
    }
    assert too_long == {}, (
        "check constraint names PostgreSQL cannot store as written "
        f"(limit {_PG_IDENTIFIER_LIMIT}): {too_long}. Shorten the `name=` given "
        "in the model; do not spell the `ck_<table>_` prefix, the metadata "
        "naming convention already adds it."
    )


def test_migrated_check_constraint_names_match_the_model_metadata(
    temp_database_url: str,
) -> None:
    """What the migrations build and what the models describe must be one schema.

    ``alembic check`` asserts this too, but only on a version that compares
    check constraints by name — and only for as long as that stays true. This
    asserts the invariant itself against a freshly migrated database, so the
    guarantee does not depend on which Alembic the resolver picked today.
    """

    result = _alembic(["upgrade", "head"], temp_database_url)
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"

    expected = _metadata_check_constraints()
    engine = create_engine(temp_database_url)
    try:
        with engine.connect() as connection:
            rows = connection.execute(
                text(
                    "SELECT rel.relname, con.conname FROM pg_constraint con "
                    "JOIN pg_class rel ON rel.oid = con.conrelid "
                    "JOIN pg_namespace ns ON ns.oid = rel.relnamespace "
                    "WHERE con.contype = 'c' AND ns.nspname = current_schema()"
                )
            ).all()
    finally:
        engine.dispose()

    actual: dict[str, set[str]] = {}
    for table, name in rows:
        actual.setdefault(table, set()).add(name)

    drift = {
        table: {
            "in the models only": sorted(names - actual.get(table, set())),
            "in the database only": sorted(actual.get(table, set()) - names),
        }
        for table, names in expected.items()
        if names != actual.get(table, set())
    }
    assert drift == {}, f"check constraint names differ between models and migrations: {drift}"


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


def test_agent_studio_migration_seeds_valid_immutable_history(
    temp_database_url: str,
) -> None:
    result = _alembic(["upgrade", "head"], temp_database_url)
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    engine = create_engine(temp_database_url)
    try:
        with engine.connect() as conn:
            row = conn.execute(
                text(
                    "SELECT p.version_number, p.schema_version, p.configuration, a.activated_by "
                    "FROM personalization_policy_versions p "
                    "JOIN personalization_policy_activations a ON a.policy_version_id = p.id"
                )
            ).one()
            assert row.version_number == 1
            assert row.schema_version == "personalization-policy/v1"
            assert len(row.configuration["standards"]) == 8
            assert len(row.configuration["strategies"]) == 5
            assert row.activated_by == "system:migration"

        with pytest.raises(Exception, match="append-only"):
            with engine.begin() as conn:
                conn.execute(text("UPDATE personalization_policy_versions SET name = 'mutated'"))

        result = _alembic(["downgrade", "d3b7e2f19c45"], temp_database_url)
        assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
        result = _alembic(["upgrade", "head"], temp_database_url)
        assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    finally:
        engine.dispose()


def test_ins_002_round_trip_preserves_historical_insights_without_fabricating_types(
    temp_database_url: str,
) -> None:
    assert _alembic(["upgrade", _INS_002_PARENT], temp_database_url).returncode == 0
    engine = create_engine(temp_database_url)
    company_id = uuid.uuid4()
    insight_id = uuid.uuid4()
    try:
        with engine.begin() as conn:
            conn.execute(
                text("INSERT INTO companies (id, name) VALUES (:id, 'Historical Co')"),
                {"id": company_id},
            )
            conn.execute(
                text(
                    "INSERT INTO insights "
                    "(id, subject, company_id, claim, kind, state, version) "
                    "VALUES (:id, 'COMPANY', :company, 'Historical claim', "
                    "'FACT', 'SUPPORTED', 1)"
                ),
                {"id": insight_id, "company": company_id},
            )

        assert _alembic(["upgrade", "head"], temp_database_url).returncode == 0
        with engine.connect() as conn:
            row = conn.execute(
                text(
                    "SELECT claim, insight_type, structured_payload, producer_job_id, "
                    "dossier_version_id, derivation_version FROM insights WHERE id = :id"
                ),
                {"id": insight_id},
            ).one()
            assert row.claim == "Historical claim"
            assert tuple(row[1:]) == (None, None, None, None, None)

        assert _alembic(["downgrade", _INS_002_PARENT], temp_database_url).returncode == 0
        with engine.connect() as conn:
            assert (
                conn.execute(
                    text("SELECT claim FROM insights WHERE id = :id"), {"id": insight_id}
                ).scalar_one()
                == "Historical claim"
            )
        assert _alembic(["upgrade", "head"], temp_database_url).returncode == 0
    finally:
        engine.dispose()


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


def test_a_decision_must_name_exactly_one_acquisition_subject(
    temp_database_url: str,
) -> None:
    """The subject rule is enforced by the schema, not only by the service.

    A decision with neither subject is unattributable evidence; one with both
    would let two acquisition surfaces claim the same decision row. Both are
    unrepresentable rather than merely undocumented, so a script, a repair or a
    later feature that writes the table directly cannot create one.
    """

    assert _alembic(["upgrade", "head"], temp_database_url).returncode == 0

    engine = create_engine(temp_database_url)
    insert = text(
        "INSERT INTO company_domain_resolutions "
        " (id, capture_id, contact_id, decision_number, is_current, state, decision_kind, "
        "  policy_version, reasons) "
        "VALUES (:id, :cap, :con, 1, true, 'UNRESOLVED', 'AUTOMATIC', 'test', '[]'::jsonb)"
    )
    try:
        with engine.begin() as conn:
            capture_id = _seed_capture(conn)
            contact_id = _seed_contact(conn, first="Ada", domain="subject.example")

        for capture_value, contact_value, why in (
            (capture_id, contact_id, "two subjects"),
            (None, None, "no subject"),
        ):
            with engine.begin() as conn, pytest.raises(Exception) as caught:
                conn.execute(
                    insert, {"id": uuid.uuid4(), "cap": capture_value, "con": contact_value}
                )
            assert "single_subject" in str(caught.value), why

        # And exactly one of each is accepted, so the constraint refuses only
        # what it is meant to.
        with engine.begin() as conn:
            conn.execute(insert, {"id": uuid.uuid4(), "cap": capture_id, "con": None})
            conn.execute(insert, {"id": uuid.uuid4(), "cap": None, "con": contact_id})
    finally:
        engine.dispose()


def test_the_subject_downgrade_refuses_while_a_contact_decision_exists(
    temp_database_url: str,
) -> None:
    """Narrowing the ledger back to captures would have to delete evidence.

    A contact-subject decision is the only record of why a Contact acquired
    without a capture carries the company it carries and how certain that was.
    The reversal refuses rather than dropping it — and reverses cleanly once
    there is nothing to lose, which is what keeps the round trip meaningful.
    """

    assert _alembic(["upgrade", "head"], temp_database_url).returncode == 0

    engine = create_engine(temp_database_url)
    try:
        with engine.begin() as conn:
            contact_id = _seed_contact(conn, first="Ada", domain="seed.example")
            conn.execute(
                text(
                    "INSERT INTO company_domain_resolutions "
                    " (id, contact_id, decision_number, is_current, state, decision_kind, "
                    "  policy_version, selected_domain, reasons) "
                    "VALUES (:id, :con, 1, true, 'PROVISIONAL', 'AUTOMATIC', "
                    " 'company-domain-resolution/practical-v1', 'seed.example', "
                    " '[\"single_aligned_provider_candidate\"]'::jsonb)"
                ),
                {"id": uuid.uuid4(), "con": contact_id},
            )

        blocked = _alembic(["downgrade", _SUBJECT_PARENT], temp_database_url)
        assert blocked.returncode != 0, "the downgrade must refuse while a decision exists"
        assert "contact-subject" in (blocked.stdout + blocked.stderr)

        with engine.begin() as conn:
            conn.execute(text("DELETE FROM company_domain_resolutions"))

        cleared = _alembic(["downgrade", _SUBJECT_PARENT], temp_database_url)
        assert cleared.returncode == 0, f"{cleared.stdout}\n{cleared.stderr}"
    finally:
        engine.dispose()


def test_the_subject_downgrade_refuses_while_a_contact_owns_candidates(
    temp_database_url: str,
) -> None:
    """A contact-owned candidate record holds a confirmation other surfaces read back."""

    assert _alembic(["upgrade", "head"], temp_database_url).returncode == 0

    engine = create_engine(temp_database_url)
    try:
        with engine.begin() as conn:
            contact_id = _seed_contact(conn, first="Ada", domain="subject.example")
            conn.execute(
                text(
                    "INSERT INTO salesnav_company_enrichments "
                    " (id, contact_id, company_key, company_name, row_count, lookup_status, "
                    "  confirmation_status, lookup_attempts, model_lookup_status, "
                    "  model_lookup_attempts) "
                    "VALUES (:id, :con, 'meridian works', 'Meridian Works', 1, 'NOT_STARTED', "
                    " 'UNCONFIRMED', 0, 'NOT_STARTED', 0)"
                ),
                {"id": uuid.uuid4(), "con": contact_id},
            )

        blocked = _alembic(["downgrade", _SUBJECT_PARENT], temp_database_url)
        assert blocked.returncode != 0, "the downgrade must refuse while a record exists"
        assert "contact-owned candidate record" in (blocked.stdout + blocked.stderr)
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


# ---------------------------------------------------------------------------
# SEQ-001 — the seven-message sequence tables
# ---------------------------------------------------------------------------


def _seed_sequence(conn: Connection, *, with_review: bool) -> None:
    """The smallest sequence a downgrade could destroy.

    Built through raw SQL rather than the ORM because this runs against a
    database at one exact migration revision, where the ORM's idea of the schema
    may be ahead of what exists.
    """

    company_id = _seed_company(conn, name="Sequence Co", domain="seq.example")
    contact_id = _seed_contact(conn, first="Seq", domain="seq.example")
    campaign_id = uuid.uuid4()
    conn.execute(
        text("INSERT INTO campaigns (id, name, status) VALUES (:id, :n, 'DRAFT')"),
        {"id": campaign_id, "n": f"Sequence campaign {campaign_id}"},
    )
    membership_id = uuid.uuid4()
    conn.execute(
        text(
            "INSERT INTO campaign_contacts (id, campaign_id, contact_id, state) "
            "VALUES (:id, :c, :ct, 'IMPORTED')"
        ),
        {"id": membership_id, "c": campaign_id, "ct": contact_id},
    )
    sequence_key = uuid.uuid4()
    sequence_id = uuid.uuid4()
    conn.execute(
        text(
            "INSERT INTO email_sequences (id, sequence_key, sequence_version, "
            "campaign_contact_id, campaign_id, contact_id, company_id, input_digest, "
            "sequence_producer_version, validation_policy_version, cadence_source, "
            "message_count, generation_status, validation_status, review_state, stop_state) "
            "VALUES (:id, :k, 1, :m, :c, :ct, :co, 'digest', 'builder/v1', 'validation/v1', "
            "'default', 7, 'COMPLETE', 'PASSED', 'APPROVED', 'RUNNING')"
        ),
        {
            "id": sequence_id,
            "k": sequence_key,
            "m": membership_id,
            "c": campaign_id,
            "ct": contact_id,
            "co": company_id,
        },
    )
    message_id = uuid.uuid4()
    conn.execute(
        text(
            "INSERT INTO email_sequence_messages (id, sequence_key, campaign_contact_id, "
            "position, message_type, purpose, delivery_state) "
            "VALUES (:id, :k, :m, 1, 'INITIAL', 'INITIAL_OUTREACH', 'NOT_READY')"
        ),
        {"id": message_id, "k": sequence_key, "m": membership_id},
    )
    version_id = uuid.uuid4()
    conn.execute(
        text(
            "INSERT INTO email_sequence_message_versions (id, message_id, sequence_id, "
            "message_version, position, subject, body, recommended_delay_days, "
            "recommended_elapsed_day, origin, generation_status, validation_status, "
            "intelligence_accepted_count, intelligence_excluded_count) "
            "VALUES (:id, :mid, :sid, 1, 1, 'A subject', 'A body worth keeping.', 0, 0, "
            "'GENERATED', 'COMPLETE', 'PASSED', 0, 0)"
        ),
        {"id": version_id, "mid": message_id, "sid": sequence_id},
    )
    if with_review:
        conn.execute(
            text(
                "INSERT INTO email_sequence_message_reviews (id, message_version_id, "
                "message_id, decision, decided_by) "
                "VALUES (:id, :v, :m, 'APPROVED', 'operator')"
            ),
            {"id": uuid.uuid4(), "v": version_id, "m": message_id},
        )


def test_seq_001_downgrade_succeeds_on_an_empty_schema(temp_database_url: str) -> None:
    """No sequence has ever been generated: reverse without ceremony.

    This is what keeps the full round-trip test meaningful — a guard that
    refused unconditionally would make the migration untestable.
    """

    assert _alembic(["upgrade", "head"], temp_database_url).returncode == 0
    reversed_cleanly = _alembic(["downgrade", _SEQ_001_PARENT], temp_database_url)
    assert reversed_cleanly.returncode == 0, f"{reversed_cleanly.stdout}\n{reversed_cleanly.stderr}"
    assert _alembic(["upgrade", "head"], temp_database_url).returncode == 0


@pytest.mark.parametrize("with_review", [False, True])
def test_seq_001_downgrade_refuses_while_sequence_data_exists(
    temp_database_url: str, with_review: bool
) -> None:
    """Generated copy and human decisions are not re-derivable, so refuse.

    Parametrised over the two cases that matter separately, and under default
    approval they are no longer "before review" and "after review". A generated
    sequence is approved and carries **no** review row at all, so
    ``with_review=False`` is the ordinary case rather than a transient one, and
    ``with_review=True`` is the sequence somebody actually ruled on. Both must
    block; the second is the one that would destroy a record of a human
    judgement, and the first still holds copy nothing can re-derive.

    The seeded ``review_state`` is ``APPROVED`` for the same reason: seeding
    ``NEEDS_REVIEW`` would be seeding a state generation no longer produces, and
    a guard proven only against unreachable data proves less than it appears to.
    """

    assert _alembic(["upgrade", "head"], temp_database_url).returncode == 0

    engine = create_engine(temp_database_url)
    try:
        with engine.begin() as conn:
            _seed_sequence(conn, with_review=with_review)

        blocked = _alembic(["downgrade", _SEQ_001_PARENT], temp_database_url)
        output = blocked.stdout + blocked.stderr
        assert blocked.returncode != 0, "the downgrade must refuse while sequence data exists"
        assert "SEQ-001" in output
        assert "generated sequence(s)" in output
        assert "generated or edited message version(s)" in output
        if with_review:
            assert "recorded human review decision(s)" in output

        # Bounded and non-leaking: the operator learns the scale of what would be
        # lost, never a sample of the content or a schema internal.
        assert "A body worth keeping." not in output
        assert "A subject" not in output
        assert "Traceback" not in output or "RuntimeError" in output
        assert "psycopg" not in output
        assert len(output) < 8_000

        # Clearing the data deliberately releases the guard.
        with engine.begin() as conn:
            for table in (
                "email_sequence_message_reviews",
                "email_sequence_message_versions",
                "email_sequences",
                "email_sequence_messages",
            ):
                conn.execute(text(f"DELETE FROM {table}"))

        cleared = _alembic(["downgrade", _SEQ_001_PARENT], temp_database_url)
        assert cleared.returncode == 0, f"{cleared.stdout}\n{cleared.stderr}"
        assert _alembic(["upgrade", "head"], temp_database_url).returncode == 0
    finally:
        engine.dispose()
