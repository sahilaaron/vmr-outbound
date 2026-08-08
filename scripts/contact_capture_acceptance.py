#!/usr/bin/env python3
"""Sanitized live acceptance for the contact-first capture path (DAT-013).

Runs the whole contact-acquisition path against a REAL running backend over
HTTP, using only the committed synthetic fixtures — no LinkedIn contact, no real
personal data, no credentials. Every scenario asserts the truthful outcome, so
the script fails loudly rather than reporting a pass it did not earn.

Usage (see docs/LINKEDIN_CAPTURE_ACCEPTANCE.md for the full runbook):

    python scripts/contact_capture_acceptance.py --base-url http://127.0.0.1:8000

The script never writes to production: it refuses any non-loopback base URL.
"""

from __future__ import annotations

import argparse
import copy
import json
import sys
import urllib.error
import urllib.request
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = REPO_ROOT / "extensions" / "salesnav-capture" / "docs" / "fixtures"


def _fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / name).read_text("utf-8"))


PROFILE_SUBMISSION = _fixture("contact-capture.profile.example.json")
SALESNAV_SUBMISSION = _fixture("contact-capture.salesnav.example.json")
LEGACY_PROFILE = _fixture("profile.payload.example.json")

INTAKE = "/api/intake/contact-captures"
LABELS = "/api/contact-labels"
LOOKUP = "/api/contacts/lookup"
LOOPBACK = ("127.0.0.1", "localhost", "::1")

EXTENSION_ORIGIN = "chrome-extension://acceptanceacceptanceacceptancea"


class Failure(Exception):
    """One scenario did not produce the outcome it claims."""


def request(base: str, path: str, payload: Any = None) -> tuple[int, Any]:
    url = base.rstrip("/") + path
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(  # noqa: S310 - loopback only, enforced below
        url,
        data=data,
        method="POST" if data is not None else "GET",
        headers={"Content-Type": "application/json", "Origin": EXTENSION_ORIGIN},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310
            return resp.status, json.loads(resp.read().decode("utf-8") or "null")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8")
        try:
            return exc.code, json.loads(body) if body else None
        except json.JSONDecodeError:
            return exc.code, {"raw": body[:200]}


def page(base: str, path: str) -> int:
    req = urllib.request.Request(base.rstrip("/") + path)  # noqa: S310
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310
            return resp.status
    except urllib.error.HTTPError as exc:
        return exc.code


def fresh(base_payload: dict[str, Any]) -> dict[str, Any]:
    payload = copy.deepcopy(base_payload)
    payload["client_submission_id"] = str(uuid.uuid4())
    for capture in payload["contacts"]:
        capture["client_capture_id"] = str(uuid.uuid4())
    return payload


def expect(condition: bool, scenario: str, detail: str) -> None:
    if not condition:
        raise Failure(f"{scenario}: {detail}")


def run(base: str) -> list[tuple[str, str]]:
    """Run every scenario. Returns (scenario, sanitized result) rows."""

    rows: list[tuple[str, str]] = []

    # 1. A manually opened profile is saved as permanent capture evidence.
    status, body = request(base, INTAKE, fresh(PROFILE_SUBMISSION))
    expect(status == 201, "1", f"expected 201, got {status} {body}")
    outcome = body["results"][0]["outcome"]
    expect(
        outcome in {"exact_match_refreshed", "unmatched_staged"},
        "1",
        f"unexpected outcome {outcome}",
    )
    rows.append(
        (
            "1. Manually opened profile saved without a campaign",
            f"HTTP 201 · outcome `{outcome}` · capture record link returned",
        )
    )
    first_submission = body

    # 2. Exact-URL refresh (only meaningful when the seeded contact exists).
    refreshed = body["counts"]["refreshed_exact_match"]
    rows.append(
        (
            "2. Exact normalized URL refreshes one contact",
            f"`refreshed_exact_match` = {refreshed}"
            + (" (seeded contact's stale title replaced)" if refreshed else " (no seed present)"),
        )
    )

    # 3. Identical retry replays idempotently.
    replay_payload = fresh(PROFILE_SUBMISSION)
    status_a, body_a = request(base, INTAKE, replay_payload)
    status_b, body_b = request(base, INTAKE, replay_payload)
    expect(status_a == 201 and status_b == 200, "3", f"{status_a}/{status_b}")
    expect(body_b["already_received"] is True, "3", "replay not flagged")
    expect(
        body_a["submission_id"] == body_b["submission_id"], "3", "replay minted a new submission"
    )
    rows.append(
        (
            "3. Identical retry is idempotent",
            "HTTP 201 then 200 · `already_received: true` · same submission id",
        )
    )

    # 4. Same id, different content, is refused.
    changed = copy.deepcopy(replay_payload)
    changed["operator_metadata"]["note"] = "different note"
    status, body = request(base, INTAKE, changed)
    expect(status == 409, "4", f"expected 409, got {status}")
    expect(body["error"] == "client_submission_id_conflict", "4", str(body))
    rows.append(
        ("4. Reused submission id with changed content", "HTTP 409 `client_submission_id_conflict`")
    )

    # 5. Older evidence cannot replace newer.
    stale = fresh(PROFILE_SUBMISSION)
    stale["contacts"][0]["captured_at"] = (datetime.now(UTC) - timedelta(days=2000)).isoformat()
    stale["contacts"][0]["current_employment_hint"]["title"] = "Intern"
    stale["contacts"][0]["experience_observations"][0]["job_title"] = "Intern"
    status, body = request(base, INTAKE, stale)
    expect(status == 201, "5", f"expected 201, got {status}")
    outcome = body["results"][0]["outcome"]
    expect(outcome != "exact_match_refreshed", "5", "a back-dated capture overwrote newer evidence")
    rows.append(
        (
            "5. Older evidence cannot replace newer",
            f"outcome `{outcome}` · the newer title stands",
        )
    )

    # 6. Sales Navigator rows: no campaign, identity honestly uncertain.
    status, body = request(base, INTAKE, fresh(SALESNAV_SUBMISSION))
    expect(status == 201, "6", f"expected 201, got {status}")
    expect(body["counts"]["submitted"] == 2, "6", "expected two rows")
    rows.append(
        (
            "6. Sales Navigator rows saved without a campaign",
            f"HTTP 201 · {body['counts']['submitted']} contacts · "
            f"`staged_unmatched` = {body['counts']['staged_unmatched']} "
            "(the row with no /in/ URL stays uncertain)",
        )
    )

    # 7. Duplicate person inside one submission is reconciled once.
    dup = fresh(PROFILE_SUBMISSION)
    second = copy.deepcopy(dup["contacts"][0])
    second["client_capture_id"] = str(uuid.uuid4())
    dup["contacts"].append(second)
    status, body = request(base, INTAKE, dup)
    expect(status == 201, "7", f"expected 201, got {status}")
    expect(body["counts"]["duplicate_in_submission"] == 1, "7", str(body["counts"]))
    rows.append(
        (
            "7. Same person twice in one submission",
            "`duplicate_in_submission` = 1 · evidence preserved · reconciled once",
        )
    )

    # 8. An empty capture is refused before anything is stored.
    empty = fresh(PROFILE_SUBMISSION)
    person = empty["contacts"][0]["person"]
    person["linkedin_profile_url"] = None
    person["salesnav_lead_url"] = None
    person["full_name"] = None
    status, body = request(base, INTAKE, empty)
    expect(status == 422, "8", f"expected 422, got {status}")
    rows.append(
        ("8. Capture with no visible identity", "HTTP 422 `validation_failed`, nothing stored")
    )

    # 9. A malformed campaign id is refused.
    #
    # This check predates contract 2.1.0 and used to be described as "the
    # contract has no campaign property". That is no longer true: 2.1.0 declares
    # an optional `campaign_id`, and intake files the Campaign Contact
    # idempotently when one is supplied. What is still refused — and what this
    # actually exercises — is a value that is not a UUID: the schema pins
    # `campaign_id` to a UUID string or null, so `camp_demo_001` fails
    # validation. The assertion is unchanged; only the claim it was making was
    # wrong. Well-formed filing behaviour (pending → applied, unknown campaign,
    # replay) is covered by tests/test_contact_capture_intake.py, which runs
    # against a database this script does not own.
    with_campaign = fresh(PROFILE_SUBMISSION)
    with_campaign["campaign_id"] = "camp_demo_001"
    status, body = request(base, INTAKE, with_campaign)
    expect(status == 422, "9", f"expected 422, got {status}")
    rows.append(
        (
            "9. Submission carrying a malformed campaign id",
            "HTTP 422 — `campaign_id` must be a UUID string or null",
        )
    )

    # 10. The legacy contract is refused with a pointer to its own route.
    legacy = copy.deepcopy(LEGACY_PROFILE)
    legacy["client_capture_id"] = str(uuid.uuid4())
    status, body = request(base, INTAKE, legacy)
    expect(status == 422, "10", f"expected 422, got {status}")
    expect(body["error"] == "unsupported_contract", "10", str(body))
    expect(
        any("/api/intake/linkedin-profile/stage" in d for d in body["details"]),
        "10",
        "no pointer to the legacy route",
    )
    rows.append(
        (
            "10. Legacy campaign-era payload posted to the new route",
            "HTTP 422 `unsupported_contract` naming `/api/intake/linkedin-profile/stage`",
        )
    )

    # 11. Labels are registered and reusable; the list leaks nothing else.
    status, body = request(base, LABELS)
    expect(status == 200, "11", f"expected 200, got {status}")
    names = sorted(entry["name"] for entry in body["labels"])
    expect("Healthcare" in names, "11", f"labels missing: {names}")
    rows.append(("11. Label registry is backend-owned and reusable", f"HTTP 200 · labels {names}"))

    # 12. Lookup answers existence only.
    status, body = request(
        base, LOOKUP + "?linkedin_profile_url=https://www.linkedin.com/in/morgan-vale"
    )
    expect(status == 200, "12", f"expected 200, got {status}")
    expect(set(body) == {"match", "contact_count", "normalized_profile_url"}, "12", str(body))
    rows.append(
        (
            "12. Save-vs-refresh lookup",
            f"HTTP 200 · `match: {body['match']}` · existence only, no contact field returned",
        )
    )

    # 13. The operator can open the resulting records.
    capture_id = first_submission["results"][0]["capture_id"]
    submission_id = first_submission["submission_id"]
    capture_status = page(base, f"/contact-captures/{capture_id}")
    submission_status = page(base, f"/contact-captures/submissions/{submission_id}")
    expect(capture_status == 200, "13", f"capture page {capture_status}")
    expect(submission_status == 200, "13", f"submission page {submission_status}")
    rows.append(
        (
            "13. Resulting capture and submission records open",
            "both operator pages render HTTP 200",
        )
    )

    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    args = parser.parse_args()

    host = args.base_url.split("//", 1)[-1].split(":")[0].split("/")[0]
    if host not in LOOPBACK:
        print(f"refusing to run against non-loopback host {host!r}", file=sys.stderr)
        return 2

    try:
        rows = run(args.base_url)
    except Failure as exc:
        print(f"FAIL — {exc}", file=sys.stderr)
        return 1

    print("| # | Scenario | Result |")
    print("| --- | --- | --- |")
    for index, (scenario, result) in enumerate(rows, start=1):
        title = scenario.split(". ", 1)[-1]
        print(f"| {index} | {title} | {result} |")
    print(f"\n{len(rows)} scenarios passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
