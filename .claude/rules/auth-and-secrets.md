---
description: Auth, session and secret-handling boundaries
paths:
  - "app/core/auth/**"
  - "app/web/**"
  - "app/api/**"
  - "deploy/nginx/**"
  - "deploy/systemd/**"
  - "deploy/ssh/**"
---

# Auth and secret boundaries

Authority: `docs/HOSTED_AUTH.md`, `docs/AGENTS.md`, `docs/PRODUCTION_HARDENING.md`.

- **Secrets never appear in source, prompts, logs, fixtures, or Git history.** This
  includes one-time tokens: a setup or invite token must not reach the nginx access
  log or the uvicorn request log. Keep such values out of the URL query string, and
  keep the logging format from echoing them.
- Passwords are Argon2id (`app/core/auth/passwords.py`). Do not swap in bcrypt or
  PBKDF2, and do not weaken the parameters without an explicit decision.
- Provider credentials are stored under the Fernet envelope, not in plaintext columns.
- Treat every new route as unauthenticated until its policy is stated. When adding a
  route, decide and record: who may call it, what happens to an anonymous caller, and
  whether it is safe to log.
- Changing nginx, systemd or ssh configuration is a deployment change — it needs the
  gate sequence, not an inline edit assumed to be live.
