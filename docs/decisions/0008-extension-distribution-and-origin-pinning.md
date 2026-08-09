# Decision 0008 — Extension distribution must precede server origin pinning

Status: **Open — decision stub. Records the dependency, does not choose the channel.**

Date: 2026-08-08

## Context

When the VMR application is reachable over HTTPS and the capture extension
authenticates to it, the server will need to know *which* extension is allowed
to post. Today it does not: the origin check accepts any `chrome-extension://`
scheme at all.

`app/api/routes.py`:

```python
if parsed.scheme == "chrome-extension":
    return True
```

That is acceptable while the endpoint is loopback-only, unauthenticated and
gated to `APP_ENV=local`. It is not acceptable on a public host, where the
allow-list should name the one published extension.

## The dependency

Pinning an origin requires a **stable extension ID**, and the ID is a
consequence of how the extension is distributed:

- An **unpacked** extension — how it is loaded today, per
  `docs/DEVELOPMENT.md` — derives its ID from the install path. It differs per
  machine and per re-install, so it cannot be pinned.
- A **packaged** extension has a stable ID derived from its signing key. The
  repository currently contains no packaging tooling, no `key` in
  `extensions/salesnav-capture/manifest.json`, and no distribution reference.

So origin pinning cannot be specified, let alone implemented, until the
distribution channel is chosen. It is on the critical path for the authenticated
remote-capture work, and it has lead time that is not engineering time.

## Decision

Recorded, deliberately, as a dependency rather than a choice:

1. **Server origin pinning for the extension is blocked on a stable extension
   ID.** No pinning work should be planned as if the ID were available.
2. **The distribution channel is Sahil's to choose.** The options carry
   different review, update and privacy-disclosure consequences, and this stub
   does not pick between Chrome Web Store publication and self-hosted
   distribution with a bundled key.
3. **Until then the current rule stands unchanged** — any `chrome-extension://`
   origin is accepted on the loopback-only, `APP_ENV=local` capture routes. This
   stub authorizes no change to that behaviour.

## Consequences

- The authenticated remote-capture slice must treat "publish the extension and
  record its ID" as a prerequisite step, not a follow-up.
- Whichever channel is chosen, the resulting ID becomes server configuration
  (an allow-list setting), not a hard-coded constant, so a re-key does not
  require a code change.
- A `host_permissions` change in that same release will disable the extension
  until the operator re-grants it. That is a distribution-visible event and
  belongs in the release runbook.

## What this stub deliberately does not do

- It does not choose Chrome Web Store versus self-hosted distribution.
- It does not add a `key` to the manifest, any packaging script, or any
  publication metadata.
- It does not change the server origin check, add authentication, or relax any
  existing guard.

Supersede this stub with a full decision once the channel is chosen.
