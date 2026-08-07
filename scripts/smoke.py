#!/usr/bin/env python3
"""Smoke-check a running local instance.

Confirms the app is up and the database is reachable, using the same bounded
probes expected by deployment tooling.
Read-only: it performs no import, creates nothing, and sends nothing.

Usage:
    python scripts/smoke.py                         # checks http://127.0.0.1:8000
    python scripts/smoke.py http://127.0.0.1:8000
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request

DEFAULT_BASE = "http://127.0.0.1:8000"


def _get(url: str) -> tuple[int, dict[str, object]]:
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        response = urllib.request.urlopen(req, timeout=10)
    except urllib.error.HTTPError as exc:
        # HTTP status failures still carry the probe's structured response. A
        # readiness 503 is a dependency verdict, not a transport failure.
        response = exc
    with response as resp:
        body = resp.read().decode("utf-8")
        status = int(resp.status)
    try:
        parsed = json.loads(body)
    except json.JSONDecodeError:
        return status, {"raw": body}
    return status, parsed if isinstance(parsed, dict) else {"value": parsed}


def main() -> int:
    base = (sys.argv[1] if len(sys.argv) > 1 else DEFAULT_BASE).rstrip("/")
    ok = True

    try:
        status, health = _get(f"{base}/healthz")
        print(f"[smoke] GET /healthz -> {status} {health.get('status')}")
        if status != 200 or health.get("status") != "ok":
            ok = False
    except (urllib.error.URLError, OSError) as exc:
        print(f"[smoke] ERROR: could not reach {base}/healthz — is the app running? ({exc})")
        return 2

    try:
        status, ready = _get(f"{base}/readyz")
        checks = ready.get("checks")
        db = checks.get("database") if isinstance(checks, dict) else None
        print(f"[smoke] GET /readyz -> {status} database={db}")
        if db != "ok":
            print("[smoke] ERROR: database not reachable (run scripts/dev_up.py).")
            ok = False
    except (urllib.error.URLError, OSError) as exc:
        print(f"[smoke] ERROR: /readyz failed ({exc})")
        return 2

    if ok:
        print("[smoke] OK — app is up and its database dependency is reachable.")
        return 0
    print("[smoke] One or more runtime checks did not pass (see above).")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
