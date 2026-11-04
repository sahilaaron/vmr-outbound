"""Guard for the logrotate rules this repository installs.

Ubuntu's `nginx-common` package owns `/var/log/nginx/*.log` through
`/etc/logrotate.d/nginx`, which covers VMR's nginx logs. logrotate refuses to
see one log in two rules: a VMR rule naming a path there either fails with a
duplicate log entry, which makes `logrotate.service` exit 1, or, if it sorts
first, makes logrotate skip the package rule, which stops rotation for every
other site's nginx logs on the host.

This is a static guard. It reads the rule files and runs nothing.
"""

from __future__ import annotations

from pathlib import Path

LOGROTATE_DIR = Path(__file__).resolve().parents[1] / "deploy" / "logrotate"
PACKAGE_OWNED_LOG_DIR = "/var/log/nginx/"


def test_no_logrotate_rule_names_a_package_owned_nginx_log() -> None:
    rules = [path for path in sorted(LOGROTATE_DIR.iterdir()) if path.is_file()]
    assert rules, "no rules found in deploy/logrotate — the reader is broken"

    offenders = [
        path.name for path in rules if PACKAGE_OWNED_LOG_DIR in path.read_text(encoding="utf-8")
    ]
    assert not offenders, (
        f"these rules name a path under {PACKAGE_OWNED_LOG_DIR}, which the nginx package "
        f"rule already owns, so logrotate would see a duplicate log entry: {offenders}"
    )
