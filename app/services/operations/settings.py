"""The effective-control layer: what is actually on, and who decided it.

Hosted Beta UAT found the problem this module exists for. The Agent controls were
enabled and the Research jobs were still paused, because
``FEATURES__COMPANY_RESEARCH`` was false in ``/etc/vmr/vmr.env``. The screen said
"Company research is switched off for this deployment", which was true and
useless: the only way to change it was SSH, an edit and a restart. An
administrator running the product should not need a shell to run the product.

The contract
------------
An operator control is on when **all three** of these are true, and the Admin
screen shows all three separately so the answer is explainable rather than just
displayed:

1. **Deployment capability.** Does this deployment *can*? A provider credential
   is configured, an environment permits it, a prerequisite control is on. This
   is not an opinion and no button changes it — a switch whose provider has no
   API key cannot be turned on, and the screen says which key is missing rather
   than letting somebody turn on a thing that will silently do nothing.
2. **The administrator's operational setting.** The durable row in
   ``operational_settings``. This is the half that moved out of the environment.
3. **The Agent or Campaign control**, where one applies. Unchanged: a Campaign's
   execution switch and the Agent registry still decide what may run for whom,
   and this layer never overrules them.

Deployment defaults, and why the environment is a default and not a ceiling
---------------------------------------------------------------------------
When no row exists for a control, the environment's ``FEATURES__*`` value is
used. That makes this table safe to create empty: every existing deployment keeps
exactly the behaviour it had.

When a row *does* exist it wins. It has to: the whole requirement is that an
administrator can enable Company Research from the application, and Company
Research is false in the environment of the deployment that needs it. Treating
the environment as a ceiling would have satisfied the letter of "operator
control" while leaving the UAT finding exactly where it was.

The capability check is the thing that is *not* overridable, and it is checked on
every read rather than only at write time. A key removed from the environment
after somebody turned a provider on takes effect immediately, and the screen
explains why.

Classification
--------------
Not every ``FEATURES__`` flag became an operator control, and the ones that did
not are listed here with a reason rather than left out silently:

* :data:`DEPLOYMENT_ONLY` — deployment or security boundaries. Several of them
  are enforced by startup validation (``app/core/runtime.py``,
  ``app/core/auth/startup.py``) that runs once, before serving; a switch that
  could turn one on at runtime would walk straight past the validation that
  exists to refuse exactly that state. ``workbench`` also decides whether the
  routers are mounted at all, which no database row can change without a
  restart. These are shown read-only and have no write path.
* :data:`DECLARED_NOT_CONSULTED` — flags that exist in ``FeatureFlags`` and that
  nothing reads. Putting them on an Admin screen would offer a switch that does
  nothing, which is worse than not offering it. They are listed on the screen
  under their own heading, stating plainly that they are inert.
* :data:`PRODUCT_CONTROLS` — everything else: ordinary agent and provider
  operation, administrator-controlled.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.core.features import FeatureFlags
from app.models.enums import AgentIdentifier
from app.models.operational_setting import OperationalSetting
from app.services.audit import record_audit_event


class OperationalSettingError(Exception):
    """A refusal an administrator should read, not a bug."""


# ---------------------------------------------------------------------------
# Capability predicates
# ---------------------------------------------------------------------------
#
# Each returns "why this cannot be enabled", or None when it can. They are
# written as reasons rather than booleans because the reason is what the screen
# has to show: "Logo.dev enrichment: cannot be enabled — provider credential is
# not configured" is an operator instruction, and `False` is not.


@dataclass(frozen=True)
class Capability:
    """Whether a control *could* be on here, and what says otherwise."""

    available: bool
    #: One sentence, shown verbatim next to the control. ``None`` when available.
    reason: str | None = None
    #: What was checked, for the "credential configured: yes/no" line. Never a
    #: secret value — only the name of the thing and whether it is present.
    evidence: tuple[tuple[str, bool], ...] = ()


def _always(_settings: Settings, _stored: Mapping[str, bool]) -> Capability:
    return Capability(available=True)


def _needs_logo_dev(settings: Settings, _stored: Mapping[str, bool]) -> Capability:
    configured = settings.has_logo_dev_key()
    return Capability(
        available=configured,
        reason=(
            None
            if configured
            else (
                "The logo.dev provider credential is not configured. Set LOGO_DEV_API_KEY "
                "in the deployment environment; it cannot be set from this screen."
            )
        ),
        evidence=(("logo.dev credential configured", configured),),
    )


#: Environments where a deterministic simulator standing in for a paid provider
#: is the documented behaviour rather than a lie about what happened.
_SIMULATED_PROVIDER_ENVIRONMENTS = frozenset({"local", "development", "test", "ci"})


def _needs_millionverifier(settings: Settings, _stored: Mapping[str, bool]) -> Capability:
    """MillionVerifier: a credential, or an environment where the simulator answers.

    The credential is not an absolute requirement, and pretending it is would
    have broken local development. With no key configured, the verification
    service deliberately routes to a deterministic simulator — that is a designed
    behaviour with its own tests, and it is how the pipeline is exercised without
    spending credits.

    What must not happen is a *hosted* deployment reporting verification as on
    while a simulator quietly answers, because the whole value of the switch
    there is that somebody is buying real answers. So the credential is required
    exactly where a simulated answer would be misleading.
    """

    configured = settings.has_millionverifier_key()
    simulated_ok = settings.app_env.lower() in _SIMULATED_PROVIDER_ENVIRONMENTS
    available = configured or simulated_ok
    return Capability(
        available=available,
        reason=(
            None
            if available
            else (
                "The MillionVerifier credential is not configured, and this is a hosted "
                "environment where a simulated answer would be misleading. Set "
                "MILLIONVERIFIER_API_KEY in the deployment environment, or rotate a "
                "credential in Verification Studio; it cannot be set from this screen."
            )
        ),
        evidence=(
            ("MillionVerifier credential configured", configured),
            ("Simulator permitted in this environment", simulated_ok),
        ),
    )


def _needs_claude_cli(settings: Settings, _stored: Mapping[str, bool]) -> Capability:
    configured = bool(settings.claude_cli_path)
    return Capability(
        available=configured,
        reason=(
            None
            if configured
            else (
                "No Claude CLI command is configured for this deployment, so there is "
                "nothing to call. Set CLAUDE_CLI_PATH in the environment."
            )
        ),
        evidence=(("Claude CLI command configured", configured),),
    )


def _needs_sheets_audience(settings: Settings, stored: Mapping[str, bool]) -> Capability:
    """The Sheets add-on is useless — and confusing — with no audience configured.

    Turning the surface on without ``SHEETS__ALLOWED_AUDIENCES`` produces a
    deployment that answers every add-on request with the same 401 it gives a
    forged one, which reads on the operator's screen as "the integration is
    broken". Stating the missing configuration here turns that into an
    instruction. The value cannot be set from this screen: it is the check that
    decides which application may present a credential, and a security boundary
    does not belong behind a product toggle.
    """

    if not settings.sheets.allowed_audiences:
        return Capability(
            available=False,
            reason=(
                "No add-on client id is configured. Set SHEETS__ALLOWED_AUDIENCES in the "
                "deployment environment to the client id shown in the add-on's own "
                "sidebar; it cannot be set from this screen."
            ),
            evidence=(("Add-on client id configured", False),),
        )
    if not _resolve(settings, stored, "email_sequences"):
        return Capability(
            available=False,
            reason=(
                "Email sequences are off, so no sheet row could ever reach Ready — a Ready "
                "row is a verified address and a validated seven-message sequence."
            ),
            evidence=(("Email sequences enabled", False),),
        )
    return Capability(available=True)


def _needs_gmail_client(settings: Settings, stored: Mapping[str, bool]) -> Capability:
    configured = bool(settings.gmail.client_id and settings.gmail.client_secret)
    sequences = _resolve(settings, stored, "email_sequences")
    if not configured:
        return Capability(
            available=False,
            reason=(
                "The Gmail OAuth client is not configured. Set GMAIL__CLIENT_ID and "
                "GMAIL__CLIENT_SECRET in the deployment environment; they cannot be set "
                "from this screen."
            ),
            evidence=(("Gmail OAuth client configured", False),),
        )
    if not sequences:
        return Capability(
            available=False,
            reason=(
                "Gmail drafts are created from a reviewed sequence, so Email sequences "
                "must be on first."
            ),
            evidence=(("Gmail OAuth client configured", True), ("Email sequences on", False)),
        )
    return Capability(
        available=True,
        evidence=(("Gmail OAuth client configured", True), ("Email sequences on", True)),
    )


def _requires(*keys: str) -> Callable[[Settings, Mapping[str, bool]], Capability]:
    """A capability that is simply "these other controls are on"."""

    def predicate(settings: Settings, stored: Mapping[str, bool]) -> Capability:
        missing = [key for key in keys if not _resolve(settings, stored, key)]
        labels = {spec.key: spec.label for spec in PRODUCT_CONTROLS}
        if missing:
            names = ", ".join(labels.get(key, key) for key in missing)
            return Capability(
                available=False,
                reason=f"Requires {names}. Turn that on first.",
                evidence=tuple((labels.get(key, key) + " on", False) for key in missing),
            )
        return Capability(
            available=True,
            evidence=tuple((labels.get(key, key) + " on", True) for key in keys),
        )

    return predicate


def _needs_workbench(settings: Settings, _stored: Mapping[str, bool]) -> Capability:
    on = settings.features.workbench
    return Capability(
        available=on,
        reason=(
            None
            if on
            else (
                "This is part of the operator interface, which is switched off for this "
                "deployment (FEATURES__WORKBENCH). That is a deployment decision and "
                "cannot be changed from this screen."
            )
        ),
        evidence=(("Operator interface mounted", on),),
    )


def _needs_model_lookup(settings: Settings, stored: Mapping[str, bool]) -> Capability:
    cli = _needs_claude_cli(settings, stored)
    if not cli.available:
        return cli
    return _requires("automatic_company_domain_resolution")(settings, stored)


# ---------------------------------------------------------------------------
# The registry
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ControlSpec:
    """One administrator-controlled product switch."""

    key: str
    label: str
    #: What turning it on actually does, in the operator's terms. Rendered under
    #: the control, so it is a sentence rather than a phrase.
    summary: str
    #: Which part of the product it belongs to, for grouping on the screen.
    group: str
    capability: Callable[[Settings, Mapping[str, bool]], Capability] = _always
    #: Agents whose paused ``feature_disabled`` work becomes reclaimable when this
    #: control is turned on. See :func:`agents_gated_by`.
    gates_agents: tuple[AgentIdentifier, ...] = field(default_factory=tuple)


PRODUCT_CONTROLS: tuple[ControlSpec, ...] = (
    ControlSpec(
        key="company_research",
        label="Company research",
        summary=(
            "Lets the Research Agent read a company's own website through the registered "
            'workers. A campaign must still opt in with the Agent config {"live": true}; '
            "this does not start a crawl by itself and authorises no AI synthesis."
        ),
        group="Research",
        gates_agents=(AgentIdentifier.RESEARCH,),
    ),
    ControlSpec(
        key="research_claude_fallback",
        label="Research web fallback (Claude CLI)",
        summary=(
            "A second attempt inside the Research Agent, run only after the deterministic "
            "worker has already produced too little. Every claim it returns must carry a "
            "source URL and the supporting text or it is discarded."
        ),
        group="Research",
        capability=lambda settings, stored: (
            _needs_claude_cli(settings, stored)
            if not _needs_claude_cli(settings, stored).available
            else _requires("company_research")(settings, stored)
        ),
        gates_agents=(AgentIdentifier.RESEARCH,),
    ),
    ControlSpec(
        key="company_intelligence",
        label="Company intelligence",
        summary=(
            "Classifies a company from research evidence that has already been committed. "
            "It never browses, never rewrites a canonical company field and never makes a "
            "contact outreach-eligible."
        ),
        group="Research",
        capability=_needs_claude_cli,
    ),
    ControlSpec(
        key="insights_research",
        label="Insights",
        summary=(
            "The Insights Agent's evidence pass over committed research. Turning it off "
            "stops new insights being produced; everything already recorded stays readable."
        ),
        group="Research",
        capability=_needs_claude_cli,
        gates_agents=(AgentIdentifier.INSIGHTS,),
    ),
    ControlSpec(
        key="automatic_company_domain_resolution",
        label="Automatic company-domain resolution",
        summary=(
            "Lets the resolution policy decide a captured company's domain without asking. "
            "It never fabricates a domain, and a provisional answer still opens company "
            "research and nothing that spends money or sends mail."
        ),
        group="Contact acquisition",
    ),
    ControlSpec(
        key="salesnav_domain_enrichment",
        label="logo.dev domain lookup",
        summary=(
            "Allows the outbound call to the logo.dev Search Brands API. It imports nothing "
            "and never auto-accepts a candidate on its own."
        ),
        group="Contact acquisition",
        capability=_needs_logo_dev,
    ),
    ControlSpec(
        key="model_company_domain_lookup",
        label="Model domain fallback",
        summary=(
            "Asks the local Claude CLI for a company's own domain when the provider found "
            "nothing usable. Capped at provisional: it can never reach confirmed."
        ),
        group="Contact acquisition",
        capability=_needs_model_lookup,
    ),
    ControlSpec(
        key="contact_capture_promotion",
        label="Capture promotion",
        summary=(
            "Promotes a staged contact capture into a canonical contact. Suppression stays "
            "authoritative and no promoted contact becomes outreach-eligible by itself."
        ),
        group="Contact acquisition",
        capability=_requires("automatic_company_domain_resolution", "salesnav_domain_enrichment"),
    ),
    ControlSpec(
        key="millionverifier",
        label="MillionVerifier",
        summary=(
            "Allows the Verification Agent to spend MillionVerifier credits. With it off, "
            "the deterministic simulator answers and no credit is spent."
        ),
        group="Providers",
        capability=_needs_millionverifier,
        gates_agents=(AgentIdentifier.VERIFICATION,),
    ),
    ControlSpec(
        key="email_generation",
        label="Email discovery",
        summary=(
            "Lets the Email Agent derive and test candidate addresses for a contact. "
            "Discovery is what produces an address for verification to spend on."
        ),
        group="Providers",
        gates_agents=(AgentIdentifier.EMAIL,),
    ),
    ControlSpec(
        key="drafting",
        label="Personalization drafting",
        summary=(
            "Lets the Personalization Agent write a draft. Approval is still a separate "
            "human decision and there is no sending path of any kind."
        ),
        group="Drafting",
        capability=_needs_claude_cli,
        gates_agents=(AgentIdentifier.PERSONALIZATION,),
    ),
    ControlSpec(
        key="email_sequences",
        label="Email sequences",
        summary=(
            "Personalization writes seven immutable messages for review instead of one "
            "draft. A campaign must also opt in through its cadence settings. Nothing is "
            "scheduled and nothing is sent."
        ),
        group="Drafting",
        gates_agents=(AgentIdentifier.PERSONALIZATION,),
    ),
    ControlSpec(
        key="gmail_drafts",
        label="Gmail drafts",
        summary=(
            "One-click creation of Gmail *drafts* from a reviewed sequence. The scope "
            "requested is gmail.compose and no code path in this application can reach a "
            "send call; a human still presses send in Gmail."
        ),
        group="Drafting",
        capability=_needs_gmail_client,
    ),
    ControlSpec(
        key="google_sheets_integration",
        label="Google Sheets add-on",
        summary=(
            "Lets a Google Sheets add-on submit name-and-company rows into a campaign and "
            "read back the verified address and the seven-message sequence. It adds no "
            "intelligence of its own and cannot send, schedule or draft anything. Turning "
            "it off makes every add-on route answer as though it does not exist."
        ),
        group="Operator interface",
        capability=_needs_sheets_audience,
    ),
    ControlSpec(
        key="csv_import",
        label="Spreadsheet import",
        summary="Allows contacts to be imported into a campaign from a CSV or XLSX file.",
        group="Operator interface",
    ),
    ControlSpec(
        key="suppressions",
        label="Suppression management",
        summary=(
            "Shows the suppression surface in the operator interface. Suppression itself is "
            "always enforced; this only decides whether the screen is offered."
        ),
        group="Operator interface",
        capability=_needs_workbench,
    ),
    ControlSpec(
        key="seller_knowledge_base",
        label="Knowledge Base",
        summary=(
            "The operator-maintained record of what is sold and what may be claimed about "
            "it. Reading and writing knowledge only; it drafts nothing."
        ),
        group="Operator interface",
        capability=_needs_workbench,
    ),
    ControlSpec(
        key="agent_workbench",
        label="Agent monitor and controls",
        summary=(
            "The operator control room over the execution backbone. It adds no execution "
            "capability of its own and cannot release a suppression."
        ),
        group="Operator interface",
        capability=_needs_workbench,
    ),
)

CONTROLS_BY_KEY: Mapping[str, ControlSpec] = {spec.key: spec for spec in PRODUCT_CONTROLS}


#: Deployment or security boundaries. Environment only, shown read-only, no write
#: path. The reason is carried with the name because "why can't I change this?"
#: is the first question the screen has to answer.
DEPLOYMENT_ONLY: Mapping[str, str] = {
    "workbench": (
        "Decides whether the operator interface is mounted at all, which is settled "
        "when the process starts. It is also refused outright in production."
    ),
    "salesnav_intake": (
        "A local-development intake endpoint with no credential boundary of its own. "
        "Startup validation refuses it outside local development, and a switch that "
        "could turn it on afterwards would walk past that validation."
    ),
    "linkedin_profile_intake": (
        "Local-development intake endpoint; refused outside local by startup validation."
    ),
    "linkedin_profile_refresh": (
        "Rewrites canonical contact fields from stored snapshots. It belongs with the "
        "intake it accompanies rather than with ordinary product operation."
    ),
    "linkedin_company_intake": (
        "Local-development intake endpoint; refused outside local by startup validation."
    ),
    "contact_capture_intake": (
        "The extension's intake endpoint. Its safety argument is the per-install bearer "
        "credential and the approved extension origin, both validated at startup; "
        "enabling it hosted without that boundary is refused before serving."
    ),
    "claude_mcp_bridge": (
        "A deployment integration rather than a product control, and there is nothing "
        "behind it yet."
    ),
}

#: Declared in ``FeatureFlags`` and read by nothing. Listed rather than hidden so
#: the screen does not quietly imply the set of switches is the set of
#: behaviours.
DECLARED_NOT_CONSULTED: tuple[str, ...] = (
    "normalization",
    "deduplication",
    "scoring",
    "saleshandy",
)


def _known_keys() -> frozenset[str]:
    return frozenset(FeatureFlags.model_fields)


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------


def stored_values(session: Session) -> dict[str, bool]:
    """Every administrator opinion currently recorded, keyed by control name.

    Unknown keys are dropped rather than trusted: a row left behind by a removed
    control must not be able to change the meaning of a flag that still exists.
    """

    known = _known_keys()
    rows = session.scalars(select(OperationalSetting)).all()
    return {row.key: bool(row.enabled) for row in rows if row.key in known}


def _resolve(settings: Settings, stored: Mapping[str, bool], key: str) -> bool:
    """The effective value of one control, without a database round trip.

    Order: the administrator's row if there is one, otherwise the deployment
    default; then the capability gate, which can only ever turn something *off*.
    """

    spec = CONTROLS_BY_KEY.get(key)
    default = bool(getattr(settings.features, key, False))
    if spec is None:
        # Not an operator control: the environment is the whole answer.
        return default
    wanted = stored.get(key, default)
    if not wanted:
        return False
    return spec.capability(settings, stored).available


def effective_flags(session: Session, settings: Settings | None = None) -> FeatureFlags:
    """The full flag set as the application should actually behave.

    Returns a ``FeatureFlags`` rather than a dict so that every existing reader —
    ``features.enabled()``, ``model_fields`` enumeration, attribute access — keeps
    working unchanged against it. One SELECT.
    """

    resolved = settings or get_settings()
    stored = stored_values(session)
    values = resolved.features.model_dump()
    for key in CONTROLS_BY_KEY:
        values[key] = _resolve(resolved, stored, key)
    return FeatureFlags(**values)


def enabled(session: Session, key: str, settings: Settings | None = None) -> bool:
    """Whether one control is effectively on. The read used inside services."""

    resolved = settings or get_settings()
    if key not in CONTROLS_BY_KEY:
        return bool(getattr(resolved.features, key, False))
    return _resolve(resolved, stored_values(session), key)


def refusal(session: Session, key: str, settings: Settings | None = None) -> str | None:
    """Why ``key`` is not in force right now, phrased for an operator, or ``None``.

    Every surface that refuses because a control is off should say the same thing
    and should say the *specific* thing. A route that answers "enrichment is not
    enabled" when the real cause is a missing provider credential sends somebody
    to the wrong screen; a route that answers "the API key is missing" when an
    administrator simply turned the control off does the same in the other
    direction. One function, so the two cannot drift.
    """

    resolved = settings or get_settings()
    spec = CONTROLS_BY_KEY.get(key)
    if spec is None:
        return None if getattr(resolved.features, key, False) else f"{key} is switched off."

    stored = stored_values(session)
    capability = spec.capability(resolved, stored)
    if not capability.available:
        return f"{spec.label} cannot be enabled here. {capability.reason}"
    if not stored.get(key, bool(getattr(resolved.features, key, False))):
        return (
            f"{spec.label} is switched off. An administrator can turn it on in "
            "Admin → Configuration."
        )
    return None


# ---------------------------------------------------------------------------
# The Admin view
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ControlRow:
    """One control as the Admin Configuration screen needs it."""

    key: str
    label: str
    summary: str
    group: str
    #: What the administrator asked for — the stored row, or the deployment
    #: default when there is none.
    requested: bool
    #: What is actually in force once capability is applied. When this differs
    #: from ``requested`` the screen shows why.
    effective: bool
    capability: Capability
    #: ``True`` when no row exists yet, so the screen can say "deployment default"
    #: rather than implying somebody chose it.
    from_deployment_default: bool
    version: int | None
    updated_by: str | None
    updated_at: object | None
    reason: str | None


@dataclass(frozen=True)
class DeploymentRow:
    """One environment-only setting, shown read-only."""

    key: str
    value: bool
    why: str


@dataclass(frozen=True)
class OperationalConfigurationView:
    controls: tuple[ControlRow, ...]
    deployment_only: tuple[DeploymentRow, ...]
    declared_not_consulted: tuple[str, ...]


def configuration_view(
    session: Session, settings: Settings | None = None
) -> OperationalConfigurationView:
    """Everything the Admin Configuration screen renders about switches."""

    resolved = settings or get_settings()
    rows = {row.key: row for row in session.scalars(select(OperationalSetting)).all()}
    stored = stored_values(session)

    controls: list[ControlRow] = []
    for spec in PRODUCT_CONTROLS:
        row = rows.get(spec.key)
        default = bool(getattr(resolved.features, spec.key, False))
        requested = bool(row.enabled) if row is not None else default
        capability = spec.capability(resolved, stored)
        controls.append(
            ControlRow(
                key=spec.key,
                label=spec.label,
                summary=spec.summary,
                group=spec.group,
                requested=requested,
                effective=requested and capability.available,
                capability=capability,
                from_deployment_default=row is None,
                version=row.version if row is not None else None,
                updated_by=row.updated_by if row is not None else None,
                updated_at=row.updated_at if row is not None else None,
                reason=row.reason if row is not None else None,
            )
        )

    deployment = tuple(
        DeploymentRow(key=key, value=bool(getattr(resolved.features, key, False)), why=why)
        for key, why in DEPLOYMENT_ONLY.items()
    )
    return OperationalConfigurationView(
        controls=tuple(controls),
        deployment_only=deployment,
        declared_not_consulted=DECLARED_NOT_CONSULTED,
    )


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ControlChange:
    """What a write did, so the caller can report it and act on it."""

    key: str
    enabled: bool
    changed: bool
    #: Agents whose paused work the caller should now reconcile. Empty when the
    #: control was turned off or nothing changed.
    reclaim_agents: tuple[AgentIdentifier, ...]


def set_control(
    session: Session,
    *,
    key: str,
    enabled_value: bool,
    actor: str,
    reason: str | None = None,
    expected_version: int | None = None,
    settings: Settings | None = None,
) -> ControlChange:
    """Record an administrator's decision about one operator control.

    Refuses, with a message meant for the screen:

    * an unknown key, or a key that is a deployment or security boundary — this
      is the write path's half of the classification, so a hand-crafted POST
      naming ``DATABASE_URL`` or ``contact_capture_intake`` is refused here and
      not only hidden in the form;
    * turning on a control whose capability is unavailable, naming the missing
      credential or prerequisite rather than accepting a setting that would do
      nothing;
    * a stale ``expected_version``, so two administrators on the same screen
      cannot silently overwrite each other.

    The caller owns the commit, as every other service here does.
    """

    resolved = settings or get_settings()
    if key in DEPLOYMENT_ONLY:
        raise OperationalSettingError(
            f"{key} is a deployment setting and is not editable from this screen. "
            f"{DEPLOYMENT_ONLY[key]}"
        )
    spec = CONTROLS_BY_KEY.get(key)
    if spec is None:
        raise OperationalSettingError(f"{key} is not an operator-controlled setting.")

    stored = stored_values(session)
    if enabled_value:
        capability = spec.capability(resolved, stored)
        if not capability.available:
            raise OperationalSettingError(
                capability.reason or f"{spec.label} cannot be enabled in this deployment."
            )

    row = session.get(OperationalSetting, key)
    if row is None:
        if expected_version not in (None, 0):
            raise OperationalSettingError(
                "This setting changed since the page was loaded. Reload and try again."
            )
        row = OperationalSetting(
            key=key,
            enabled=enabled_value,
            reason=_clean_reason(reason),
            updated_by=actor[:128],
            version=1,
        )
        session.add(row)
        try:
            with session.begin_nested():
                session.flush()
        except IntegrityError as exc:
            # Two administrators created the row at the same instant, both from a
            # page that showed no stored row. One of them was right for about a
            # millisecond. Reporting the conflict is the same answer the version
            # check gives a moment later, and it is better than letting the
            # slower click overwrite a decision it never saw.
            session.expunge(row)
            raise OperationalSettingError(
                "This setting changed since the page was loaded. Reload and try again."
            ) from exc
        _audit(session, spec=spec, actor=actor, previous=None, new=enabled_value, reason=reason)
        return ControlChange(
            key=key,
            enabled=enabled_value,
            changed=True,
            reclaim_agents=spec.gates_agents if enabled_value else (),
        )

    return _update(
        session,
        row=row,
        spec=spec,
        enabled_value=enabled_value,
        actor=actor,
        reason=reason,
        expected_version=expected_version,
        previous=bool(row.enabled),
    )


def _update(
    session: Session,
    *,
    row: OperationalSetting,
    spec: ControlSpec,
    enabled_value: bool,
    actor: str,
    reason: str | None,
    expected_version: int | None,
    previous: bool | None,
) -> ControlChange:
    if expected_version is not None and expected_version != row.version:
        # ``0`` reaches here from a form that rendered before any row existed, and
        # it is a conflict for the same reason a stale integer is: the page the
        # operator decided from is not the page the database is on. ``None`` is
        # the separate case of a programmatic caller with no opinion about
        # concurrency, and it is left alone deliberately.
        raise OperationalSettingError(
            "This setting changed since the page was loaded. Reload and try again."
        )
    was = bool(row.enabled) if previous is None else previous
    if was == enabled_value:
        # Not an error. A double-submitted form, or two administrators agreeing,
        # should not produce a failure page — and it must not bump the version or
        # write an audit event claiming a change that did not happen.
        return ControlChange(key=spec.key, enabled=enabled_value, changed=False, reclaim_agents=())

    row.enabled = enabled_value
    row.reason = _clean_reason(reason)
    row.updated_by = actor[:128]
    row.version = int(row.version) + 1
    session.flush()
    _audit(session, spec=spec, actor=actor, previous=was, new=enabled_value, reason=reason)
    return ControlChange(
        key=spec.key,
        enabled=enabled_value,
        changed=True,
        reclaim_agents=spec.gates_agents if enabled_value else (),
    )


def _audit(
    session: Session,
    *,
    spec: ControlSpec,
    actor: str,
    previous: bool | None,
    new: bool,
    reason: str | None,
) -> None:
    record_audit_event(
        session,
        actor=actor,
        action="operational_setting.updated",
        entity_type="operational_setting",
        entity_id=spec.key,
        previous_state=None if previous is None else ("on" if previous else "off"),
        new_state="on" if new else "off",
        reason=_clean_reason(reason) or f"{spec.label} turned {'on' if new else 'off'}",
        context={"key": spec.key},
    )


def _clean_reason(reason: str | None) -> str | None:
    if reason is None:
        return None
    text = reason.strip()
    return text[:500] or None


def agents_gated_by(key: str) -> tuple[AgentIdentifier, ...]:
    """Agents whose paused work a control turning on should make reclaimable."""

    spec = CONTROLS_BY_KEY.get(key)
    return spec.gates_agents if spec is not None else ()
