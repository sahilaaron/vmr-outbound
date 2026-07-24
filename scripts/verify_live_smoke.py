#!/usr/bin/env python3
"""Operator-run, single live MillionVerifier smoke check (VER-007).

Performs EXACTLY ONE real MillionVerifier request for one address you control, to
confirm real credentials and truthful mapping/storage/display end to end. Every
other verification behaviour is proven offline against the simulator; this is the
one deliberate live acceptance step.

Prerequisites (see docs/VERIFICATION_RUNBOOK.md):

* a real MillionVerifier key in your local, git-ignored ``.env`` as
  ``MILLIONVERIFIER_API_KEY=...`` (never commit it), and the app restarted;
* the verification feature enabled: ``FEATURES__MILLIONVERIFIER=true``;
* the local database reachable (``python scripts/dev_up.py``).

Usage:

    FEATURES__MILLIONVERIFIER=true \\
      python scripts/verify_live_smoke.py --email you@your-domain.com --confirm

Safety: refuses to run without a real (non-test) key, without the feature
enabled, or without ``--confirm``; refuses documented test keys; refuses when a
fresh cached result would skip the call; and never falls back to a simulated
success. The API key is never printed, logged, or written anywhere.
"""

from __future__ import annotations

import argparse

from app.core.config import get_settings
from app.db.session import session_scope
from app.services.verification.live_smoke import (
    LiveSmokeError,
    LiveSmokeResult,
    run_live_smoke,
)


def _fmt(value: object) -> str:
    if value is None:
        return "—"
    if isinstance(value, bool):
        return "yes" if value else "no"
    return str(value)


def _print_result(result: LiveSmokeResult) -> None:
    result_code = f"{_fmt(result.provider_result)} / {_fmt(result.provider_result_code)}"
    signals = f"{_fmt(result.is_role)} / {_fmt(result.is_free)} / {_fmt(result.did_you_mean)}"
    evidence = f"{_fmt(result.evidence_stored)} (source={_fmt(result.evidence_source)})"
    ledger = (
        f"{_fmt(result.ledger_recorded)} (cache={_fmt(result.ledger_cache_status)}, "
        f"charge={_fmt(result.ledger_charge_status)})"
    )
    ui = f"{_fmt(result.status_visual)} — {_fmt(result.status_explanation)}"
    lines = [
        "",
        "MillionVerifier live smoke — sanitized result (no secrets)",
        "-" * 58,
        f"  normalized email        : {result.normalized_email}",
        f"  live HTTP client used   : {_fmt(result.live_provider_selected)}",
        f"  provider request made   : {_fmt(result.provider_request_made)}",
        f"  transport ok            : {_fmt(result.transport_ok)}",
        f"  provider livemode       : {_fmt(result.livemode)}",
        f"  provider result / code  : {result_code}",
        f"  canonical mapped result : {_fmt(result.canonical_result)}",
        f"  precise internal status : {_fmt(result.precise_status)}",
        f"  role / free / suggestion: {signals}",
        f"  subresult               : {_fmt(result.subresult)}",
        f"  checked at              : {_fmt(result.checked_at)}",
        f"  policy version          : {_fmt(result.policy_version)}",
        f"  billed this call        : {_fmt(result.credited)}",
        f"  credits remaining       : {_fmt(result.credits_remaining)}",
        f"  evidence stored         : {evidence}",
        f"  evidence id             : {_fmt(result.evidence_id)}",
        f"  ledger recorded         : {ledger}",
        f"  UI status               : {ui}",
        f"  provider error          : {_fmt(result.provider_error)}",
    ]
    for w in result.warnings:
        lines.append(f"  WARNING: {w}")
    lines.append("")
    print("\n".join(lines))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run one deliberate live MillionVerifier check.")
    parser.add_argument("--email", required=True, help="One address you control to verify.")
    parser.add_argument(
        "--confirm",
        action="store_true",
        help="Required. Confirms you intend to consume one MillionVerifier credit.",
    )
    parser.add_argument(
        "--allow-existing-fresh",
        action="store_true",
        help="Advanced: proceed even if fresh cached evidence exists (a cache hit "
        "would NOT prove a live call). Prefer an address with no fresh evidence.",
    )
    args = parser.parse_args(argv)

    settings = get_settings()
    print("=" * 58)
    print("MillionVerifier LIVE smoke test — LIVE MODE REQUESTED")
    print("A single real provider request will be made and one credit may be spent.")
    print(
        f"Feature enabled: {settings.features.millionverifier} · "
        f"key configured: {settings.has_millionverifier_key()} (value never shown)"
    )
    print("=" * 58)

    try:
        with session_scope() as session:
            result = run_live_smoke(
                session,
                email=args.email,
                confirm=args.confirm,
                settings=settings,
                allow_existing_fresh=args.allow_existing_fresh,
            )
    except LiveSmokeError as exc:
        print(f"[live-smoke] REFUSED: {exc}")
        return 2

    _print_result(result)

    ok = (
        result.provider_request_made
        and result.transport_ok
        and result.live_provider_selected
        and result.evidence_source != "simulated"
    )
    if ok:
        print("[live-smoke] PASS — an authentic live MillionVerifier interaction was recorded.")
        return 0
    print("[live-smoke] INCOMPLETE — see warnings above; not a completed live smoke test.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
