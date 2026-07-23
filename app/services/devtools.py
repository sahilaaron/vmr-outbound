"""Guarded local-development data tools (fixtures and reset).

These controls exist so the workbench can be exercised with representative,
fully synthetic data on a developer machine. They are deliberately hard to
misuse:

* Every action calls :func:`ensure_local_database` first, which refuses to run
  unless ``APP_ENV`` is ``local`` **and** the configured database host is a
  loopback address. A live/remote database (RDS or anything else reachable over
  the network) can never be targeted, regardless of flags.
* The web layer additionally hides these tools outside local development and
  requires an explicit typed confirmation for the destructive action.
* Fixture data is synthetic only (AGENTS.md: "Add fixtures with synthetic data
  only") — no real people, companies, or addresses.
* Actions record audit events where a database survives to hold them (the
  reset re-records its own audit trail after clearing).
"""

from __future__ import annotations

import io
from dataclasses import dataclass
from urllib.parse import urlparse

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.db.base import Base
from app.models.campaign import Campaign
from app.models.enums import CampaignStatus, SuppressionReason, SuppressionType
from app.services.audit import record_audit_event
from app.services.campaigns import create_campaign
from app.services.imports.importer import ImportSummary, run_import
from app.services.suppressions import add_suppression

_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})

_ACTOR = "devtools"


class LocalOnlyViolation(Exception):
    """Raised when a local-only tool is invoked against a non-local target."""


def ensure_local_database(settings: Settings | None = None) -> None:
    """Refuse to proceed unless the environment and database are local-only."""

    settings = settings or get_settings()
    if settings.app_env.lower() != "local":
        raise LocalOnlyViolation(
            f"local data tools are disabled outside local development (APP_ENV={settings.app_env!r})"
        )
    host = urlparse(settings.database_url.replace("postgresql+psycopg", "postgresql")).hostname
    if host not in _LOOPBACK_HOSTS:
        raise LocalOnlyViolation(
            f"local data tools refuse to run against non-loopback database host {host!r}"
        )


# --- Synthetic fixture data ---------------------------------------------------

_FIXTURE_CSV = """first_name,last_name,company_name,company_domain,email,title,country,industry,company_size
Asha,Verma,Northwind Analytics,northwind-analytics.example.com,asha.verma@northwind-analytics.example.com,Head of Operations,India,Analytics,51-200
Daniel,Okafor,Harbor Logistics,harborlogistics.example.com,d.okafor@harborlogistics.example.com,VP Supply Chain,Nigeria,Logistics,201-500
Mei,Chen,Quartz Materials,quartzmaterials.example.com,,Procurement Director,Singapore,Manufacturing,501-1000
Lucas,Ferreira,Aurora Renewables,aurorarenewables.example.com,lucas.ferreira@aurorarenewables.example.com,Plant Manager,Brazil,Energy,201-500
Sofia,Marino,Vantage Freight,vantagefreight.example.com,sofia.marino@vantagefreight.example.com,COO,Italy,Logistics,51-200
Priya,Nair,Cobalt Software,cobaltsoftware.example.com,priya.nair@cobaltsoftware.example.com,Director of IT,India,Software,201-500
,,Broken Row Ltd,,not-an-email,CEO,,,
Omar,Haddad,Cedar Foods,cedarfoods.example.com,omar.haddad@cedarfoods.example.com,Founder,UAE,Food & Beverage,11-50
Asha,Verma,Northwind Analytics,northwind-analytics.example.com,asha.verma@northwind-analytics.example.com,Head of Operations,India,Analytics,51-200
Nina,Kovacs,Blocked Corp,blockedcorp.example.com,nina.kovacs@blockedcorp.example.com,CFO,Hungary,Finance,51-200
"""

_FIXTURE_XLSX_SHEETS: dict[str, list[list[str]]] = {
    "Mining Contacts": [
        ["First Name", "Surname", "Company", "Website", "Email Address", "Job Title", "Country"],
        [
            "Elena",
            "Petrova",
            "Granite Extraction Co",
            "graniteextraction.example.com",
            "elena.petrova@graniteextraction.example.com",
            "Operations Director",
            "Kazakhstan",
        ],
        [
            "Tomas",
            "Lindqvist",
            "Boreal Minerals",
            "borealminerals.example.com",
            "t.lindqvist@borealminerals.example.com",
            "Head of Exploration",
            "Sweden",
        ],
        [
            "Grace",
            "Mwangi",
            "Rift Valley Aggregates",
            "riftvalleyagg.example.com",
            "",
            "Quarry Manager",
            "Kenya",
        ],
    ],
    "Cement Contacts": [
        ["First Name", "Surname", "Company", "Website", "Email Address", "Job Title", "Country"],
        [
            "Rahul",
            "Kapoor",
            "Deccan Cement Works",
            "deccancement.example.com",
            "rahul.kapoor@deccancement.example.com",
            "Plant Head",
            "India",
        ],
        [
            "Marta",
            "Silva",
            "Atlantica Cimentos",
            "atlanticacimentos.example.com",
            "marta.silva@atlanticacimentos.example.com",
            "Technical Director",
            "Portugal",
        ],
    ],
    "Notes": [],
}

_FIXTURE_XLSX_MAPPING: dict[str, str] = {
    "First Name": "first_name",
    "Surname": "last_name",
    "Company": "company_name",
    "Website": "company_domain",
    "Email Address": "email",
    "Job Title": "title",
    "Country": "country",
}

_FIXTURE_SUPPRESSED_EMAIL = "nina.kovacs@blockedcorp.example.com"


def fixture_csv_bytes() -> bytes:
    """The synthetic CSV fixture (valid, invalid, duplicate, suppressed rows)."""

    return _FIXTURE_CSV.encode("utf-8")


def fixture_xlsx_bytes() -> bytes:
    """Build the synthetic multi-sheet XLSX fixture in memory via openpyxl."""

    from openpyxl import Workbook

    workbook = Workbook()
    default_sheet = workbook.active
    first = True
    for sheet_name, rows in _FIXTURE_XLSX_SHEETS.items():
        if first and default_sheet is not None:
            worksheet = default_sheet
            worksheet.title = sheet_name
            first = False
        else:
            worksheet = workbook.create_sheet(title=sheet_name)
        for row in rows:
            worksheet.append(row)
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


@dataclass
class FixtureLoadResult:
    """Outcome of loading one synthetic fixture."""

    campaign_name: str
    summary: ImportSummary


def _get_or_create_campaign(session: Session, name: str) -> Campaign:
    existing = session.query(Campaign).filter(Campaign.name == name).first()
    if existing is not None:
        return existing
    return create_campaign(
        session,
        name=name,
        description="Synthetic local-development demo campaign. Safe to delete.",
        status=CampaignStatus.DRAFT,
        actor=_ACTOR,
    )


def load_csv_fixture(session: Session) -> FixtureLoadResult:
    """Load the representative synthetic CSV into a demo campaign."""

    ensure_local_database()
    add_suppression(
        session,
        suppression_type=SuppressionType.EMAIL,
        value=_FIXTURE_SUPPRESSED_EMAIL,
        reason=SuppressionReason.OPT_OUT,
        source="devtools fixture",
        notes="Synthetic opt-out so the suppressed outcome is demonstrable locally.",
        actor=_ACTOR,
    )
    campaign = _get_or_create_campaign(session, "Demo — CSV Import")
    session.commit()
    summary = run_import(
        session,
        campaign_id=campaign.id,
        content=fixture_csv_bytes(),
        filename="demo_contacts.csv",
        actor=_ACTOR,
    )
    return FixtureLoadResult(campaign_name=campaign.name, summary=summary)


def load_xlsx_fixture(session: Session) -> FixtureLoadResult:
    """Load the synthetic multi-sheet XLSX into a demo campaign (mapped columns)."""

    ensure_local_database()
    campaign = _get_or_create_campaign(session, "Demo — XLSX Import")
    session.commit()
    summary = run_import(
        session,
        campaign_id=campaign.id,
        content=fixture_xlsx_bytes(),
        filename="demo_workbook.xlsx",
        sheet_selection=[0, 1],
        column_mapping=dict(_FIXTURE_XLSX_MAPPING),
        actor=_ACTOR,
    )
    return FixtureLoadResult(campaign_name=campaign.name, summary=summary)


def clear_local_data(session: Session) -> list[str]:
    """Delete all application data from the LOCAL database (schema retained).

    Truncates every application table except ``alembic_version`` so migration
    state survives. Returns the truncated table names. A fresh audit event
    recording the reset is written afterwards, so the wipe itself leaves a
    trace.
    """

    ensure_local_database()
    tables = [
        table.name for table in Base.metadata.sorted_tables if table.name != "alembic_version"
    ]
    quoted = ", ".join(f'"{name}"' for name in tables)
    session.execute(text(f"TRUNCATE TABLE {quoted} RESTART IDENTITY CASCADE"))
    record_audit_event(
        session,
        actor=_ACTOR,
        action="devtools.local_reset",
        entity_type="database",
        entity_id="local",
        new_state="cleared",
        reason="operator requested a local test-data reset from the workbench",
        context={"tables_cleared": len(tables)},
    )
    session.commit()
    return tables


def reset_to_demo_state(session: Session) -> list[FixtureLoadResult]:
    """Clear local data, then load both synthetic fixtures (known demo state)."""

    ensure_local_database()
    clear_local_data(session)
    return [load_csv_fixture(session), load_xlsx_fixture(session)]
