# Build handoff — Gmail draft integration, reconciled onto the accounts-aware main

## Branch and commits

| | |
| --- | --- |
| Repository | `sahilaaron/vmr-outbound` |
| Branch | `feat/gmail-draft-integration` |
| PR | #271 — **not merged by this thread** |
| Reconciled onto | `fc2b1b33e29d451a717abde100437c80d92c84c1` (`main`, containing #272 and #273) |
| Previous Gmail candidate | `2e9d18b3e8b51554f000365acffa6bbbb23e19d3` |
| Original base | `8720b7f4cbda8e9b193551355998ecfc363987be` |
| Merge commit | `88bfc9fb` — `git merge --no-ff origin/main` |
| Reconciliation commit | `8d9414d3f6822dcf9aceb1a808ae8e372cbbbb6d` |
| Head SHA | this handoff's own commit, reported in the session log |
| Alembic head | `b45732880eff` — exactly one |

No rebase, no squash, no force-push, no rewritten history. `origin/main` is an
ancestor of the head.

## What the reconciliation had to decide

`main` moved twice while this branch was out: hosted capture promotion (#272)
and durable email/password accounts (#273).

Nine files were touched by both sides. Three conflicted, all because the two
sides appended to the same place rather than because they disagreed:
`app/models/enums.py` (both added enums at EOF — both kept), `app/web/v2/routes.py`
(both added an import on the same line — both kept, both used), and
`docs/HOSTED_AUTH.md` (both rewrote the "deliberately does not do" list — main's
newer list kept whole, except the bullet claiming there is no Gmail draft
support, which this branch makes untrue).

## The integration defect this found, and the fix

**Gmail mailbox ownership was keyed on Google's `sub`.** `GmailMailboxGrant`
stored `operator_subject`, taken from `OperatorSession.subject`, and every
lookup, bind and disconnect matched on it. That was correct when written,
because every session was then a Google session.

#273 ended that. An account is now a row in `users`, and it can sign in with a
password and never touch Google. Such a session carries `user_id` and an **empty**
subject — `OperatorSession` says so itself, and adds that the subject is
"retained for the audit trail rather than for any access decision".

Composed unchanged, the two slices produce a Gmail feature the Hosted Beta
cannot use:

* `connected_grant` and `latest_grant` short-circuit on the empty subject, so a
  password operator always sees "no mailbox connected";
* `bind_mailbox` would insert `''` and violate `operator_subject_not_blank`, so
  connecting is a 500 rather than a refusal;
* the callback's anti-confused-deputy check compared the transaction's operator
  subject with the signed-in operator's. Between any two password operators that
  is `"" == ""`, which proves nothing.

The Hosted Beta operator is an administrator-created account with a password.
This is the exact UAT persona, so the feature was dead on the path the UAT walks.

**Fix — ownership moves to the durable user, and nothing else changes.**

| | |
| --- | --- |
| `GmailMailboxGrant.user_id` | FK `users.id`, `NOT NULL`, `ondelete RESTRICT`. The only field any authorization path reads |
| `uq_gmail_mailbox_grants_connected` | partial unique index moved onto `user_id` — "one live mailbox per owner" stays a database fact |
| `operator_subject` | now nullable **provenance**: which Google sign-in authorized this mailbox, when one did. Read by nothing |
| `session_account_id()` | new, in `app/core/auth/accounts.py` — resolves a session to its durable account UUID |
| mailbox service | `connected_grant`, `latest_grant`, `mailbox_state`, `disconnect` take `user_id`; `bind_mailbox` takes `user_id` plus optional `operator_subject` |
| OAuth transaction | the `"operator"` claim is now `str(user_id)`, compared constant-time against the signed-in account |

No table was added, no column dropped, and the draft lineage is untouched. The
middleware already published the account id it needed; the reconciliation
consumes it rather than inventing anything.

## Migrations

Two heads existed after the merge — `a7d3e1c85f42` (Gmail) and `b8f13a6c47d2`
(accounts), genuine siblings both descending from `0926b59b7912`. Resolved the
way the project asks for:

* `40bb1177a2fa` — an **empty merge revision** joining the two lineages. No
  schema change. Re-pointing either migration's `down_revision` would have
  rewritten reviewed ancestry and asserted an ordering the two slices never had.
* `b45732880eff` — the ownership move. Backfills `user_id` through
  `users.google_subject`, which is the same identifier the column already held
  and is uniquely indexed, so the `UPDATE … FROM` cannot match two accounts. A
  row it cannot match is **refused, not guessed**. In practice there are none:
  the feature has never been deployed or switched on.

Proven on a disposable scratch database: `alembic heads` (one) → `upgrade head`
→ `alembic check` clean → `downgrade base` → `upgrade head` → `alembic check`
clean → still one head. The live schema was then read back: `user_id uuid NOT
NULL`, `fk_gmail_mailbox_grants_user_id_users → users (RESTRICT)`,
`operator_subject` nullable, `UNIQUE (user_id) WHERE status = 'CONNECTED'`.

## Adversarial review

A focused security review attacked the reconciled tree on all fourteen named
scenarios — cross-user mailbox use, admin inheritance, disabled stale session,
callback binding confusion, state replay and tampering, token exposure,
duplicate drafts, CSRF, open redirect, identity mismatch, pre-durable-user
leftovers, extension credential, approval-as-send, and any send endpoint. All
fourteen hold. It also re-confirmed the platform login scopes
(`openid email profile`, a separate constant, module and client from the Gmail
grant), the `0, 3, 7, 12, 18, 25, 35` cadence, and the absence of any scheduler,
polling or Sheets dependency.

Two findings it raised were **pre-existing on the Gmail branch** but broke named
contracts, so they were repaired here rather than filed:

1. **`malformed_response` was classified as definite.** That path only runs on a
   response already accepted as 2xx — Gmail had acted — so calling it definite
   sent the row to `FAILED`, which the reconciler never revisits, and the
   operator's next click created a **second identical draft in a stranger-facing
   mailbox**. Now ambiguous at all three sites, so it reconciles instead.
2. **`GmailTokenGrant` exposed raw tokens under the default dataclass `repr`,**
   unlike `GmailSettings` and `GmailMailboxGrant` where the same hazard was
   closed. Both fields are now `field(repr=False)`.

Three further findings were out of scope for a reconciliation and are recorded in
`docs/POST_LAUNCH_BACKLOG.md`: a concurrent-callback `IntegrityError` whose
failure direction is safe, two users holding grants on one Gmail account, and the
absent margin between the request-timeout ceiling and the reconciliation
quarantine.

## Validation

| Gate | Result |
| --- | --- |
| `ruff check .` | pass |
| `ruff format --check .` | pass |
| `mypy app` | `Success: no issues found in 273 source files` |
| Migrations | one head; full upgrade / check / downgrade / re-upgrade / check round trip |
| `test_hosted_auth`, `test_user_accounts`, `test_extension_capture_auth`, `test_hosted_capture_promotion`, `test_capture_promotion`, `test_v2_beta1_operator_ui`, `test_hosted_auth_raw_asgi` | **574 passed, 0 failed** |
| `test_gmail_draft_integration` | **74 passed, 0 failed** (includes the 7 new ownership regressions) |

Not run locally: the full suite. It takes about four hours on the build machine
and GitHub CI runs it on every push, which is the authority. `tests/test_migrations.py`
and two readiness probes in `tests/test_production_hardening.py` fail on this
Windows machine for known environmental reasons (an Anaconda `alembic` on PATH,
and psycopg's incompatibility with the default `ProactorEventLoop`); both are
pre-existing on `main` and green in CI.

## Remaining staging prerequisites for the Gmail OAuth UAT

None of this was done here — no deployment, no VPS env or nginx change, no Google
Cloud configuration, and no feature flag enabled.

1. **A Google Cloud OAuth client for Gmail, separate from the sign-in client.**
   `GMAIL__CLIENT_ID` and `GMAIL__CLIENT_SECRET`. The redirect URI must be the
   staging origin plus `/gmail/callback`, matched byte for byte.
2. **`gmail.compose` on that client's consent screen**, with each operator's own
   Gmail address added as a **test user**. The scope is restricted; the test-user
   list avoids Google verification review for a Beta of two or three people.
3. **`GMAIL__TOKEN_ENCRYPTION_KEY`** — a Fernet key of its own, never the session
   secret and never the Agent Studio key. Install it the way the staging secret
   was installed for capture promotion.
4. **`FEATURES__GMAIL_DRAFTS=true`**, which is off by default and stays off until
   the three values above are present. It is not sufficient on its own:
   `gmail_enabled()` requires **`FEATURES__EMAIL_SEQUENCES`** as well, because a
   draft is created from a reviewed sequence and there is nothing to draft
   without one.
5. **An operator account that can sign in.** The UAT persona is an
   administrator-created user with a password, which is exactly the case this
   reconciliation repaired. Nothing else is required of it — no Google link.
6. **`AUTH__PUBLIC_BASE_URL`** already set to the staging origin, since the setup
   link and the Gmail redirect are both built from it rather than from `Host`.

The UAT then walks: sign in → review the seven-message sequence → **Connect
Gmail** → create drafts → confirm they are in that operator's own Drafts folder →
confirm nothing was sent.

## What this thread did not do

No PR merged, no deployment, no VPS env or nginx change, no Google Cloud change,
no feature flag enabled, no sending, no scheduler, no polling, no Sheets, and no
change to the approval boundary. Approval remains a precondition for drafting and
is not sending authority; creating a draft is not sending.
