"""Vendored deterministic company-website collector.

Imported from the standalone ``company-website-researcher`` prototype
(v0.1.1) and reduced to collection only. Per #173 the standalone SQLite
queue, CLI runtime, filesystem reporting, and the optional LLM
interpreter stage are deliberately absent: the application owns job
state, persistence, retries, idempotency and evidence contracts.

Modules here are vendored third-party-shaped code. Prefer fixing a
defect upstream in the prototype and re-vendoring over editing in place;
the one file written for this repository is ``collect.py``.
"""

from __future__ import annotations

__version__ = "0.1.1"
PROGRAM_NAME = "company-website-researcher"
USER_AGENT = (
    f"CompanyResearchBot/{__version__} "
    "(+local research prototype; contact site owner via normal channels)"
)
