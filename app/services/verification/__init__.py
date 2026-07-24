"""Exact-address verification (Phase 2, EPIC 05).

A replaceable MillionVerifier adapter, a policy-versioned cache and freshness
model, a Postgres-backed idempotent job queue with bounded retries and
interrupted-worker recovery, and the truthful mapping from provider outcomes to
the four visible states. Nothing here ever treats catch-all, unknown, a provider
error, or an insufficient-credit condition as a valid mailbox.
"""
