"""Staged CSV import for authorized contact batches (DAT-002).

The public entry point is :func:`app.services.imports.importer.run_import`. The
pipeline is split into small, independently testable units:

* :mod:`app.services.imports.normalization` — conservative field normalization.
* :mod:`app.services.imports.validation` — per-row column/value validation.
* :mod:`app.services.imports.dedup` — deterministic, conservative matching.
* :mod:`app.services.imports.importer` — staged orchestration and summary.
"""

from __future__ import annotations
