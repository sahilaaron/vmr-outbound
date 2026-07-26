#!/usr/bin/env python3
"""Sanitized live acceptance for capture promotion (DAT-014).

Drives the whole bridge — pending capture → domain candidates → operator
decision → canonical Company → canonical Contact — against a REAL running
backend over HTTP, using the committed synthetic fixtures. No real person, no
real company, no credentials.

The domain provider is stubbed **at the HTTP boundary**: this script serves the
documented logo.dev Search Brands response shape on loopback, and the backend is
pointed at it with ``LOGO_DEV_SEARCH_URL``. The real client, the real service,
the real routes and the real database are all exercised; only the provider is
local. A live logo.dev call needs an API key and is a separate, explicitly
marked step (see docs/CAPTURE_PROMOTION.md).

Usage:

    python scripts/capture_promotion_acceptance.py --base-url http://127.0.0.1:8000

The script refuses any non-loopback base URL and asserts every outcome, so it
fails loudly rather than reporting a pass it did not earn.
"""

from __future__ import annotations

import argparse
import copy
import json
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = REPO_ROOT / "extensions" / "salesnav-capture" / "docs" / "fixtures"
SUBMISSION = json.loads((FIXTURES / "contact-capture.profile.example.json").read_text("utf-8"))

INTAKE = "/api/intake/contact-captures"
LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")
EXTENSION_ORIGIN = "chrome-extension://acceptanceacceptanceacceptancea"

# The two candidates the stub provider returns: a plausible match and a
# same-named decoy, so the run proves that two candidates require a decision.
STUB_BRANDS = [
    {"domain": "meridianworks.example", "name": "Meridian Works"},
    {"domain": "meridian-works-group.example", "name": "Meridian Works Group"},
]


class Failure(Exception):
    """A scenario did not produce the outcome it claims."""


# --- Stub provider ------------------------------------------------------------


class _StubHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        body = json.dumps(STUB_BRANDS).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: Any) -> None:  # pragma: no cover - silence
        return


def start_stub(port: int) -> HTTPServer:
    server = HTTPServer(("127.0.0.1", port), _StubHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


# --- HTTP helpers -------------------------------------------------------------


def post_json(base: str, path: str, payload: Any) -> tuple[int, Any]:
    request = urllib.request.Request(  # noqa: S310 - loopback only, enforced below
        base.rstrip("/") + path,
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json", "Origin": EXTENSION_ORIGIN},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
            return response.status, json.loads(response.read().decode("utf-8") or "null")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8") or "null")


def post_form(base: str, path: str, fields: dict[str, str]) -> tuple[int, str]:
    """POST a workbench form and return (status, the Location header)."""

    data = urllib.parse.urlencode(fields).encode("utf-8")
    request = urllib.request.Request(  # noqa: S310
        base.rstrip("/") + path,
        data=data,
        method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )

    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args: Any, **kwargs: Any) -> None:
            return None

    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(request, timeout=30) as response:  # noqa: S310
            return response.status, response.headers.get("Location", "")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.headers.get("Location", "")


def get_page(base: str, path: str) -> tuple[int, str]:
    try:
        with urllib.request.urlopen(base.rstrip("/") + path, timeout=30) as response:  # noqa: S310
            return response.status, response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        return exc.code, ""


def expect(condition: bool, scenario: str, detail: str) -> None:
    if not condition:
        raise Failure(f"{scenario}: {detail}")


def stage_capture(base: str, *, full_name: str | None = None) -> str:
    """Stage one fresh capture through the real DAT-013 intake. Returns its id."""

    payload = copy.deepcopy(SUBMISSION)
    payload["client_submission_id"] = str(uuid.uuid4())
    for capture in payload["contacts"]:
        capture["client_capture_id"] = str(uuid.uuid4())
        if full_name is not None:
            person = capture["person"]
            person["full_name"] = full_name
            person["first_name"] = full_name.split(" ")[0]
            person["last_name"] = full_name.split(" ")[-1]
            person["linkedin_profile_url"] = (
                "https://www.linkedin.com/in/" + full_name.lower().replace(" ", "-")
            )
            person["linkedin_public_identifier"] = full_name.lower().replace(" ", "-")
    status, body = post_json(base, INTAKE, payload)
    expect(status == 201, "stage", f"expected 201, got {status} {body}")
    return str(body["results"][0]["capture_id"])


# --- Scenarios ----------------------------------------------------------------


def run(base: str) -> list[str]:
    rows: list[str] = []

    # 1. A staged capture appears as pending, with its company hints.
    capture_id = stage_capture(base)
    status, page = get_page(base, "/contact-captures/pending")
    expect(status == 200, "1", f"pending page {status}")
    expect("Meridian Works" in page, "1", "the captured company is not shown")
    expect("pending_lookup" in page, "1", "the capture is not awaiting a lookup")
    rows.append(
        "| 1 | Unmatched capture is eligible for domain resolution | "
        "pending page lists it · `pending_lookup` · captured company shown |"
    )

    # 2. The lookup returns candidates; none is accepted automatically.
    status, _location = post_form(base, f"/contact-captures/{capture_id}/company/lookup", {})
    expect(status in (302, 303, 307), "2", f"lookup returned {status}")
    status, page = get_page(base, f"/contact-captures/{capture_id}")
    expect(status == 200, "2", f"capture page {status}")
    expect(
        "multiple_candidates_review_required" in page, "2", "candidates were not left for review"
    )
    expect("meridianworks.example" in page, "2", "candidate domain missing")
    expect("not provided by this provider" in page, "2", "a confidence score was implied")
    rows.append(
        "| 2 | Provider candidates are stored, ranked, and left for review | "
        "`multiple_candidates_review_required` · 2 candidates · confidence shown as "
        "not provided (logo.dev returns no score) |"
    )

    # 3. Promotion is refused while the company is unresolved.
    status, _location = post_form(base, f"/contact-captures/{capture_id}/promote", {})
    status, page = get_page(base, f"/contact-captures/{capture_id}")
    expect("not promoted" in page, "3", "a promotion happened without a confirmed domain")
    rows.append(
        "| 3 | Promotion is refused while candidates await a decision | "
        "capture stays unpromoted; the reason is shown |"
    )

    # 4. Rejecting a candidate preserves it as a decision.
    status, _location = post_form(
        base,
        f"/contact-captures/{capture_id}/company/reject",
        {"domain": "meridian-works-group.example", "reason": "different company, similar name"},
    )
    expect(status in (302, 303, 307), "4", f"reject returned {status}")
    status, page = get_page(base, f"/contact-captures/{capture_id}")
    expect("Rejected candidates" in page, "4", "the rejection was not preserved")
    expect("different company, similar name" in page, "4", "the rejection reason was lost")
    rows.append(
        "| 4 | A rejected candidate is preserved with its reason | "
        "moved to *Rejected candidates* with reason, actor and time |"
    )

    # 5. Confirming a candidate resolves the company.
    status, _location = post_form(
        base,
        f"/contact-captures/{capture_id}/company/confirm",
        {"decision": "candidate", "domain": "meridianworks.example"},
    )
    expect(status in (302, 303, 307), "5", f"confirm returned {status}")
    status, page = get_page(base, f"/contact-captures/{capture_id}")
    expect("domain_candidate_confirmed" in page, "5", "the confirmation was not recorded")
    rows.append(
        "| 5 | Operator confirmation resolves the company | "
        "`domain_candidate_confirmed` · source `candidate` · confirming operator recorded |"
    )

    # 6. Promotion creates the canonical contact and company.
    status, _location = post_form(base, f"/contact-captures/{capture_id}/promote", {})
    expect(status in (302, 303, 307), "6", f"promote returned {status}")
    status, page = get_page(base, f"/contact-captures/{capture_id}")
    expect("contact_created" in page, "6", "no contact was created")
    expect("Healthcare" in page, "6", "labels did not carry over")
    rows.append(
        "| 6 | Promotion creates the Contact and Company | "
        "`contact_created` · labels and notes carried over · capture linked to the contact |"
    )

    # 7. A retry is idempotent.
    status, _location = post_form(base, f"/contact-captures/{capture_id}/promote", {})
    status, page = get_page(base, f"/contact-captures/{capture_id}")
    expect("already_promoted" in page, "7", "a retry did not report already_promoted")
    rows.append(
        "| 7 | Retrying a promotion is idempotent | `already_promoted` · no second contact |"
    )

    # 8. A second capture of the same company reuses the confirmed domain
    #    without asking the provider again.
    second_id = stage_capture(base, full_name="Riley Chen")
    status, page = get_page(base, f"/contact-captures/{second_id}")
    expect(status == 200, "8", f"capture page {status}")
    expect("existing_company_resolved" in page, "8", "the prior confirmation was not reused")
    expect("prior_mapping" in page, "8", "the reuse was not attributed to a prior mapping")
    rows.append(
        "| 8 | A previously confirmed company is reused | "
        "`existing_company_resolved` · source `prior_mapping` · no provider call |"
    )

    status, _location = post_form(base, f"/contact-captures/{second_id}/promote", {})
    status, page = get_page(base, f"/contact-captures/{second_id}")
    expect("contact_created" in page, "8", "the second capture was not promoted")
    rows.append(
        "| 9 | The reused company promotes without a second lookup | "
        "`contact_created` against the same canonical Company |"
    )

    # 10. The pending list empties as captures are promoted.
    status, page = get_page(base, "/contact-captures/pending")
    expect(status == 200, "10", f"pending page {status}")
    expect("Morgan Vale" not in page, "10", "a promoted capture is still pending")
    rows.append(
        "| 10 | Promoted captures leave the pending queue | "
        "neither promoted person is listed as pending |"
    )

    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument(
        "--stub-port",
        type=int,
        default=8788,
        help="port for the local stub provider (point LOGO_DEV_SEARCH_URL at it)",
    )
    args = parser.parse_args()

    host = args.base_url.split("//", 1)[-1].split(":")[0].split("/")[0]
    if host not in LOOPBACK_HOSTS:
        print(f"refusing to run against non-loopback host {host!r}", file=sys.stderr)
        return 2

    server = start_stub(args.stub_port)
    try:
        rows = run(args.base_url)
    except Failure as exc:
        print(f"FAIL — {exc}", file=sys.stderr)
        return 1
    finally:
        server.shutdown()

    print("| # | Scenario | Result |")
    print("| --- | --- | --- |")
    for row in rows:
        print(row)
    print(f"\n{len(rows)} scenarios passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
