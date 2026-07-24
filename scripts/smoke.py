#!/usr/bin/env python3
"""Smoke-check a running local instance.

Confirms the app is up and the database is reachable, and reports which feature
switches are enabled — so "is it actually working?" has a one-command answer.
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
    with urllib.request.urlopen(req, timeout=10) as resp:
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
        status, health = _get(f"{base}/health")
        print(f"[smoke] GET /health -> {status} {health.get('status')}")
        raw_features = health.get("features_enabled")
        features = [str(f) for f in raw_features] if isinstance(raw_features, list) else []
        print(f"[smoke] features enabled: {', '.join(features) or '(none)'}")
        for needed in ("workbench", "csv_import", "salesnav_intake"):
            mark = "on" if needed in features else "OFF"
            print(f"[smoke]   {needed}: {mark}")
            if needed not in features:
                ok = False
    except (urllib.error.URLError, OSError) as exc:
        print(f"[smoke] ERROR: could not reach {base}/health — is the app running? ({exc})")
        return 2

    try:
        status, ready = _get(f"{base}/ready")
        db = ready.get("database")
        print(f"[smoke] GET /ready -> {status} database={db}")
        if db != "ok":
            print("[smoke] ERROR: database not reachable (run scripts/dev_up.py).")
            ok = False
    except (urllib.error.URLError, OSError) as exc:
        print(f"[smoke] ERROR: /ready failed ({exc})")
        return 2

    if ok:
        print("[smoke] OK — app is up, database reachable, import features enabled.")
        return 0
    print("[smoke] Some checks did not pass (see above). Enable the flags shown in dev_up.py.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
