"""Company Intelligence inside Admin Agent Studio — the integration contract.

Assembling two independently-built areas is easy to get *nearly* right: the pages
load, the tests pass, and a company-scoped classification area has quietly become
a tenth pipeline Agent, or a customer-facing feature, or a second place that
writes the same rows. These tests exist to make each of those fail loudly.

What is being asserted, in one line each:

* Company Intelligence is **reachable** from Agent Studio, and reachable by
  linking to the pages it already owns rather than by a competing implementation.
* It is **not an Agent**: no ``AgentIdentifier``, not in ``PIPELINE_ORDER``, no
  Agent control, its own queue and its own worker.
* It is **not customer-facing**: nothing under ``/app``.
* Studio is **read-only** about it: rendering a Studio view mutates no Company
  Intelligence row.
* Both areas' own surfaces still work, the assembled migration chain has one
  head, Sending is still disabled, and no Identity Agent Studio appeared.
"""

from __future__ import annotations

import ast
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from app.core.config import get_settings
from app.main import create_app
from app.models.company_intelligence import (
    CompanyIntelligenceBackfillItem,
    CompanyIntelligenceBackfillRun,
    CompanyIntelligenceClassification,
    CompanyIntelligenceConflict,
    CompanyIntelligenceDecision,
    CompanyIntelligenceEvidenceLink,
    CompanyIntelligenceJob,
    CompanyIntelligenceVersion,
)
from app.models.enums import AgentControlStatus, AgentIdentifier
from app.models.intelligence_taxonomy import (
    IntelligenceTaxonomy,
    IntelligenceTaxonomyAlias,
    IntelligenceTaxonomyTerm,
)
from app.models.verification_job import AgentJob
from app.services.agent_studio.extensions import (
    AGENT_STUDIO_MODULES,
    COMPANY_INTELLIGENCE_MODULE,
    STUDIO_CAPABILITY_MODULES,
    enabled_capability_modules,
)
from app.services.agents.registry import PIPELINE_ORDER, get_agent_spec
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from tests.test_company_intelligence_web import classified_company

REPO_ROOT = Path(__file__).resolve().parents[1]

#: The nine registered Agents, written out rather than derived. A test that
#: recomputes the list from the thing it is checking cannot detect a tenth.
EXPECTED_AGENTS = (
    AgentIdentifier.CAPTURE,
    AgentIdentifier.IDENTITY,
    AgentIdentifier.COMPANY,
    AgentIdentifier.RESEARCH,
    AgentIdentifier.EMAIL,
    AgentIdentifier.VERIFICATION,
    AgentIdentifier.INSIGHTS,
    AgentIdentifier.PERSONALIZATION,
    AgentIdentifier.SENDING,
)

#: Every table Company Intelligence owns. Used to prove Studio writes none of them.
CI_MODELS = (
    CompanyIntelligenceVersion,
    CompanyIntelligenceClassification,
    CompanyIntelligenceEvidenceLink,
    CompanyIntelligenceConflict,
    CompanyIntelligenceDecision,
    CompanyIntelligenceJob,
    CompanyIntelligenceBackfillRun,
    CompanyIntelligenceBackfillItem,
    IntelligenceTaxonomy,
    IntelligenceTaxonomyTerm,
    IntelligenceTaxonomyAlias,
)


@pytest.fixture()
def both_on(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Agent Studio and Company Intelligence both switched on."""

    monkeypatch.setenv("APP_ENV", "local")
    monkeypatch.setenv("FEATURES__WORKBENCH", "true")
    monkeypatch.setenv("FEATURES__AGENT_WORKBENCH", "true")
    monkeypatch.setenv("FEATURES__COMPANY_INTELLIGENCE", "true")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture()
def studio_only(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Agent Studio on, Company Intelligence off — its own gate still governs."""

    monkeypatch.setenv("APP_ENV", "local")
    monkeypatch.setenv("FEATURES__WORKBENCH", "true")
    monkeypatch.setenv("FEATURES__AGENT_WORKBENCH", "true")
    monkeypatch.delenv("FEATURES__COMPANY_INTELLIGENCE", raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def client() -> TestClient:
    return TestClient(create_app(get_settings()))


def ci_row_counts(session: Session) -> dict[str, int]:
    return {
        model.__tablename__: session.scalar(select(func.count()).select_from(model)) or 0
        for model in CI_MODELS
    }


def ids(session: Session, column: Any) -> set[uuid.UUID]:
    return set(session.scalars(select(column)).all())


def imported_modules(path: Path) -> set[str]:
    """Every module a script imports, read from its AST rather than its text."""

    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
            names.update(f"{node.module}.{alias.name}" for alias in node.names)
    return names


def route_paths(app: FastAPI) -> set[str]:
    """Every path the application actually serves.

    ``app.routes`` is not enough on this FastAPI version: an included router is
    kept as a single wrapper object holding the original router rather than
    being flattened into the parent's route list. Walking only the top level
    would find no ``/app`` route at all and make every assertion below pass
    vacuously -- which is exactly the failure mode these tests exist to catch.
    """

    seen: set[int] = set()
    found: set[str] = set()

    def walk(routes: Any) -> None:
        for route in routes:
            included = getattr(route, "original_router", None)
            if included is not None:
                if id(included) not in seen:
                    seen.add(id(included))
                    walk(included.routes)
                continue
            path = getattr(route, "path", None)
            if path:
                found.add(path)
            nested = getattr(route, "routes", None)
            if nested and id(route) not in seen:
                seen.add(id(route))
                walk(nested)

    walk(app.routes)
    return found


# --- 1. reachable from Agent Studio -----------------------------------------


def test_company_intelligence_is_reachable_from_admin_agent_studio(
    both_on: None, committed_session: Session
) -> None:
    with client() as http:
        response = http.get("/admin/agents/studio")
        assert response.status_code == 200
        body = response.text

    assert "Company Intelligence" in body
    # The entry point and every advertised surface is a page the owning area
    # already serves, not a Studio-owned route.
    assert COMPANY_INTELLIGENCE_MODULE.entry_path in body
    for surface in COMPANY_INTELLIGENCE_MODULE.surfaces:
        assert surface.path in body
    assert "operator module" in body


def test_the_studio_entry_links_out_instead_of_reimplementing_the_area(
    both_on: None, committed_session: Session
) -> None:
    """Every path Studio advertises must be served by the Company Intelligence router."""

    classified_company(committed_session)
    with client() as http:
        assert http.get("/admin/agents/studio").status_code == 200
        for surface in COMPANY_INTELLIGENCE_MODULE.surfaces:
            assert http.get(surface.path).status_code == 200, surface.path

    ci_paths = {path for path in route_paths(create_app(get_settings())) if "intelligence" in path}
    # Studio adds no Company Intelligence route of its own: every one of them
    # still lives under the area's own /admin prefix.
    assert ci_paths
    assert not any(path.startswith("/admin/agents/studio") for path in ci_paths)
    assert all(path.startswith("/admin/") for path in ci_paths)


def test_the_studio_entry_respects_the_existing_company_intelligence_gate(
    studio_only: None, committed_session: Session
) -> None:
    with client() as http:
        response = http.get("/admin/agents/studio")
        assert response.status_code == 200
        # The owning router is not mounted, so advertising the area would link
        # an operator straight into a 404.
        assert COMPANY_INTELLIGENCE_MODULE.entry_path not in response.text
        assert http.get(COMPANY_INTELLIGENCE_MODULE.entry_path).status_code == 404

    assert enabled_capability_modules(get_settings().features.enabled()) == ()


# --- 2. nothing customer-facing ---------------------------------------------


def test_no_company_intelligence_route_exists_under_app(
    both_on: None, committed_session: Session
) -> None:
    customer_paths = {
        path for path in route_paths(create_app(get_settings())) if path.startswith("/app")
    }
    assert len(customer_paths) > 10, "the customer interface should still be fully mounted"
    for path in customer_paths:
        assert "intelligence" not in path.lower(), path
        assert "studio" not in path.lower(), path

    with client() as http:
        assert http.get("/app").status_code == 200
        for probe in (
            "/app/company-intelligence",
            "/app/agents/studio",
            "/app/intelligence",
        ):
            assert http.get(probe).status_code == 404, probe


def test_the_customer_interface_never_mentions_the_classification_area(
    both_on: None, committed_session: Session
) -> None:
    classified_company(committed_session)
    with client() as http:
        response = http.get("/app")
        assert response.status_code == 200
        assert "Company Intelligence" not in response.text


# --- 3 & 4. not an Agent ----------------------------------------------------


def test_company_intelligence_has_no_agent_identifier(both_on: None) -> None:
    assert tuple(AgentIdentifier) == EXPECTED_AGENTS
    values = {member.value for member in AgentIdentifier}
    assert "company_intelligence" not in values
    assert "intelligence" not in values
    # And it did not sneak in as a Studio module keyed by an Agent either.
    assert COMPANY_INTELLIGENCE_MODULE not in AGENT_STUDIO_MODULES.values()
    assert not hasattr(COMPANY_INTELLIGENCE_MODULE, "agent_id")


def test_pipeline_order_still_contains_exactly_the_nine_registered_agents(
    both_on: None,
) -> None:
    assert len(PIPELINE_ORDER) == 9
    assert PIPELINE_ORDER == EXPECTED_AGENTS
    assert set(AGENT_STUDIO_MODULES) == set(EXPECTED_AGENTS)
    # The non-Agent registry is disjoint from the Agent one by construction.
    assert all(not isinstance(module.key, AgentIdentifier) for module in STUDIO_CAPABILITY_MODULES)


# --- 5. its own queue and its own worker ------------------------------------


def test_company_intelligence_keeps_its_own_queue_and_worker(both_on: None) -> None:
    assert CompanyIntelligenceJob.__tablename__ == "company_intelligence_jobs"
    assert CompanyIntelligenceJob.__tablename__ != AgentJob.__tablename__
    # Company-scoped: no Campaign Contact column exists to enroll through.
    columns = set(CompanyIntelligenceJob.__table__.columns.keys())
    assert "company_id" in columns
    assert not columns & {"campaign_contact_id", "contact_id", "campaign_id", "agent"}

    # Two worker entry points, still separate processes.
    ci_worker = REPO_ROOT / "scripts" / "run_company_intelligence_worker.py"
    agent_worker = REPO_ROOT / "scripts" / "run_agent_worker.py"
    assert ci_worker.is_file()
    assert agent_worker.is_file(), "the Agent worker must still exist separately"

    # Asserted on the import graph rather than on prose: the docstrings mention
    # each other by design, and a text search would either miss a real delegation
    # or trip over a comment.
    ci_imports = imported_modules(ci_worker)
    assert any(name.startswith("app.services.company_intelligence") for name in ci_imports)
    assert not any(name.startswith("app.services.agents") for name in ci_imports), (
        "the standalone worker must not drive the Campaign Contact Agent queue"
    )
    assert "app.models.verification_job" not in ci_imports

    agent_imports = imported_modules(agent_worker)
    assert any(name.startswith("app.services.agents") for name in agent_imports)
    assert not any(
        name.startswith("app.services.company_intelligence") for name in agent_imports
    ), "the Agent worker must not have taken over the Company Intelligence queue"


# --- 6. Company Agent and Company Intelligence are visibly distinct ---------


def test_company_agent_and_company_intelligence_are_visibly_distinct(
    both_on: None, committed_session: Session
) -> None:
    with client() as http:
        response = http.get("/admin/agents/studio")
        assert response.status_code == 200
        body = response.text

    # Both appear, in different places, with different destinations.
    assert AGENT_STUDIO_MODULES[AgentIdentifier.COMPANY].dedicated_path in body
    assert COMPANY_INTELLIGENCE_MODULE.entry_path in body
    assert (
        AGENT_STUDIO_MODULES[AgentIdentifier.COMPANY].dedicated_path
        != COMPANY_INTELLIGENCE_MODULE.entry_path
    )

    # And the page says, in words, how the three neighbouring areas differ.
    named = {distinction.name for distinction in COMPANY_INTELLIGENCE_MODULE.distinctions}
    assert named == {"Company Agent", "Research Agent", "Company Intelligence"}
    for distinction in COMPANY_INTELLIGENCE_MODULE.distinctions:
        assert distinction.difference in body
    assert "not pipeline Agents" in body


# --- 7. Studio reads do not mutate Company Intelligence state ---------------


def test_studio_reads_do_not_mutate_company_intelligence_state(
    both_on: None, committed_session: Session
) -> None:
    company = classified_company(committed_session)
    before = ci_row_counts(committed_session)
    assert before["company_intelligence_versions"] > 0, "the fixture must produce state to guard"
    version_ids = ids(committed_session, CompanyIntelligenceVersion.id)
    decision_ids = ids(committed_session, CompanyIntelligenceDecision.id)

    with client() as http:
        assert http.get("/admin/agents/studio").status_code == 200
        assert http.get("/admin/agents/studio?campaign=").status_code == 200
        for agent_id in PIPELINE_ORDER:
            assert http.get(AGENT_STUDIO_MODULES[agent_id].dedicated_path).status_code == 200, (
                agent_id
            )

    committed_session.expire_all()
    assert ci_row_counts(committed_session) == before
    assert ids(committed_session, CompanyIntelligenceVersion.id) == version_ids
    assert ids(committed_session, CompanyIntelligenceDecision.id) == decision_ids
    # No job was queued by looking at a page.
    assert (
        committed_session.scalar(
            select(func.count())
            .select_from(CompanyIntelligenceJob)
            .where(CompanyIntelligenceJob.company_id == company.id)
        )
        == before["company_intelligence_jobs"]
    )


# --- 8 & 9. both areas' existing surfaces still work ------------------------


def test_existing_company_intelligence_surfaces_still_work(
    both_on: None, committed_session: Session
) -> None:
    company = classified_company(committed_session)
    with client() as http:
        assert http.get("/admin/company-intelligence").status_code == 200
        assert http.get("/admin/company-intelligence/taxonomy").status_code == 200
        assert http.get("/admin/company-intelligence/backfill").status_code == 200
        detail = http.get(f"/admin/companies/{company.id}/intelligence")
        assert detail.status_code == 200
        assert company.name in detail.text


def test_existing_agent_studio_modules_still_work(
    both_on: None, committed_session: Session
) -> None:
    with client() as http:
        shell = http.get("/admin/agents/studio")
        assert shell.status_code == 200
        assert shell.text.count("Position ") == len(PIPELINE_ORDER)
        for agent_id in PIPELINE_ORDER:
            path = AGENT_STUDIO_MODULES[agent_id].dedicated_path
            assert http.get(path).status_code == 200, path
        # The dedicated Studio pages built before this integration.
        for path in (
            "/admin/agents/studio/personalization",
            "/admin/agents/studio/email",
            "/admin/agents/studio/verification",
            "/admin/agents/studio/research",
            "/admin/agents/studio/capture",
            "/admin/agents/studio/company",
            "/admin/agents/studio/insights",
        ):
            assert http.get(path).status_code == 200, path


# --- 10. one Alembic head ---------------------------------------------------


def test_the_assembled_migration_chain_has_exactly_one_head() -> None:
    script = ScriptDirectory.from_config(Config(str(REPO_ROOT / "alembic.ini")))
    heads = script.get_heads()
    assert len(heads) == 1, f"expected one head, found {heads}"

    # Deliberately not pinned to a literal head revision. What this test defends
    # is that the two assembled chains stayed linearised behind a single head —
    # not that nothing has been written since the integration. Pinning the head
    # asserted the second thing, so the first migration to land afterwards
    # failed it while every guarantee it exists for still held. That is a false
    # alarm about the invariant, which is worse than no alarm.
    #
    # The CI-002 revision still has to be on the chain; the ordering assertions
    # below index into the walk and raise if any of these revisions went missing.
    revisions = list(script.walk_revisions())
    assert "a8f3c92d4e17" in {revision.revision for revision in revisions}

    # And it is genuinely linear: the two assembled chains are in one sequence,
    # not hidden behind a merge revision with two parents.
    assert all(len(revision._all_down_revisions) <= 1 for revision in revisions), (
        "a merge revision would make downgrade order ambiguous"
    )
    order = [revision.revision for revision in script.walk_revisions()]
    for later, earlier in (
        ("c41a9d78e5b2", "7b3e1c9a4d20"),
        ("a8f3c92d4e17", "c41a9d78e5b2"),
        ("7b3e1c9a4d20", "f2a91d7c4e60"),
    ):
        assert order.index(later) < order.index(earlier), f"{later} must sit above {earlier}"


# --- 11 & 12. Sending disabled, Identity Studio absent ----------------------


def test_sending_remains_disabled(both_on: None, committed_session: Session) -> None:
    sending = AGENT_STUDIO_MODULES[AgentIdentifier.SENDING]
    assert sending.capabilities.live_execution is False
    assert sending.capabilities.configuration is False
    assert sending.capabilities.preview_testing is False
    assert "remains disabled" in sending.configuration_boundary

    # The authoritative registry, not just the Studio presentation.
    spec = get_agent_spec(AgentIdentifier.SENDING)
    assert spec.default_status is AgentControlStatus.DISABLED
    assert spec.implemented is False

    with client() as http:
        response = http.get(sending.dedicated_path)
        assert response.status_code == 200
        assert "Execution unavailable" in response.text
        assert sending.configuration_boundary in response.text


def test_identity_agent_studio_was_not_added(both_on: None, committed_session: Session) -> None:
    identity = AGENT_STUDIO_MODULES[AgentIdentifier.IDENTITY]
    # Inspection only: the deferred Identity Studio would have brought
    # configuration, preview or reporting with it.
    assert identity.capabilities.configuration is False
    assert identity.capabilities.preview_testing is False
    assert identity.capabilities.reporting is False

    studio_paths = {
        path
        for path in route_paths(create_app(get_settings()))
        if path.startswith("/admin/agents/studio")
    }
    assert "/admin/agents/studio" in studio_paths, "the Studio shell must still be mounted"
    assert "/admin/agents/studio/identity" not in studio_paths, (
        "Identity is served by the generic Agent page; a dedicated route would mean "
        "an Identity Agent Studio was built"
    )
    # No Identity module appeared in the non-Agent registry either.
    assert all(module.key != "identity" for module in STUDIO_CAPABILITY_MODULES)
