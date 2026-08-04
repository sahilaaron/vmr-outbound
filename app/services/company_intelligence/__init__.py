"""Company Intelligence (CI-001).

Structured, versioned, evidence-linked understanding of a Company, derived only
from Research evidence that has already been committed.

The package is layered, and the layering is the contract:

* :mod:`normalization` — deterministic text comparison. No database, no policy.
* :mod:`taxonomy` — controlled, versioned vocabularies and the only place a
  written value is turned into a canonical term.
* :mod:`seed` — publishes the first-release vocabularies from committed data.
* :mod:`inputs` — assembles exactly what a producer is allowed to read, and the
  digest that makes production idempotent.
* :mod:`producer` — validates a structured answer and persists one version.
  Deterministic in everything except the answer itself.
* :mod:`review` — append-only operator decisions.
* :mod:`read` — the typed read model every other feature consumes. Nothing
  outside this package should query the intelligence tables directly.
* :mod:`jobs` / :mod:`backfill` — durable, bounded, resumable production work.

Two rules hold across all of it. Company Intelligence **reads** Research and
never writes it. And nothing here makes a Contact outreach-eligible, releases a
suppression, or reaches Sending — a classification is understanding, not
permission.
"""
