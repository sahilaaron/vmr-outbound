# Hosted authentication and extension authorization

**Status date:** 14 August 2026

This document describes the current hosted identity boundary and the separate VM Prospector extension authorization boundary after PR #275.

For the exact merged-vs-live distinction, see [`CURRENT_PRODUCT_STATE.md`](CURRENT_PRODUCT_STATE.md).

## 1. Hosted browser identity

The `users` table is the authority for who may sign in.

> Google proves identity. The VMR account record grants access.

There is no public signup.

An administrator creates accounts at:

`/app/admin/users`

Accounts may use any working business or personal email address. Access is not inferred from `@verifiedmarketresearch.com` or any other domain.

Two roles exist:

- `ADMIN`
- `USER`

The configured bootstrap administrator is applied idempotently at startup. Roles and account state are resolved from the account record on every authenticated request, so disabling or demoting a user takes effect on the next request.

## 2. Browser sign-in

Google sign-in is authorization-code + PKCE and is used only to establish identity.

Identity scopes remain:

- `openid`
- `email`
- `profile`

No Gmail scope belongs to this login client.

Password sign-in is also supported for admin-created accounts. Passwords are Argon2id hashes; setup/reset uses a single-use admin-issued link and never generates a temporary password.

Current merged password policy still has a 15-character minimum. A UAT repair reducing the minimum to 8 is under development on `feat/uat-operator-controls`; do not document 8 as live/current until that branch is merged and deployed.

## 3. Sessions and revocation

Hosted browser sessions are signed, secure, HttpOnly cookies with bounded lifetime.

The cookie carries the user's identity plus `auth_version`. Every authenticated request resolves the user record and compares the current version/state.

Consequences:

- disabling a user refuses existing sessions on the next request;
- password reset/change revokes earlier sessions;
- role changes apply on the next request;
- reactivation does not resurrect sessions invalidated by disablement.

## 4. CSRF / hosted request boundary

Cookie-authenticated unsafe requests are protected by the centralized hosted-origin/fetch-metadata backstop plus per-session CSRF token validation.

Extension token requests are a separate non-cookie authorization path and do not gain generic browser-session authority.

## 5. Gmail is separate

Gmail mailbox authorization is intentionally separate from hosted sign-in.

A VMR login does not grant mailbox access. A Gmail connection uses its own OAuth client, token storage and `/gmail/*` routes.

Current Gmail product capability creates drafts only. It does not send email.

See [`GMAIL_DRAFTS.md`](GMAIL_DRAFTS.md).

## 6. VM Prospector: account-linked extension authorization

PR #275 replaced the ordinary hosted manual-backend + reusable `vmrx1` credential experience.

An ordinary hosted user should no longer type or paste:

- backend URL;
- key ID;
- API/capture secret;
- `vmrx1` credential;
- mock receiver target.

The normal user experience is:

1. open VM Prospector;
2. if the browser already has a valid VMR session and the install is linked/approved, reconnect silently;
3. otherwise choose **Sign in to VMR Outbound**;
4. complete first-party authorization through `chrome.identity.launchWebAuthFlow`;
5. continue using the extension without re-entering a shared secret after Chrome restart.

The VMR `users` table remains the authority. Google is never direct extension authority.

## 7. PKCE / token lifecycle

The extension uses a first-party authorization-code + PKCE flow.

Authorization codes are short-lived and single-use. A code is consumed even when presented with the wrong verifier so verifier probing cannot preserve a usable code.

A successful link creates an extension session bound to:

- VMR user id;
- approved extension id;
- installation id;
- capture scope.

Tokens are opaque; the server stores digests rather than replayable token values.

Current merged token model:

- access token: `vmre1…`, approximately 15-minute lifetime;
- refresh token: `vmrr1…`, approximately 30-day lifetime;
- refresh token rotates on every use;
- replay/reuse detection revokes the whole linked session;
- disabled/deleted/revoked user/session state fails closed on the next authorized request.

The extension keeps the short-lived access token in `chrome.storage.session` and durable refresh authority in extension-local storage so Chrome restart does not require manual credential re-entry.

## 8. Exact extension authority

The extension authorization contract remains deliberately narrow.

An account-linked extension token may authorize only:

| Method | Path | Purpose |
| --- | --- | --- |
| `POST` | `/api/intake/contact-captures` | submit reviewed capture |
| `GET` | `/api/contact-labels` | list reusable labels/collections |
| `GET` | `/api/contacts/lookup` | existence/refresh lookup |
| `GET` | `/api/campaigns` | Campaign selector |

Nothing else is extension-authorized.

In particular, extension authority does not grant:

- `/admin`;
- user management;
- provider-spend controls;
- Gmail;
- Sending;
- generic Campaign mutation;
- Company/admin write paths;
- `/api/intake/linkedin-company/stage`.

The contract object is shared so route authorization and tests cannot silently drift apart.

## 9. Origin / cross-site behavior

The extension flow has narrow cross-origin handling for the approved Chrome extension origin where browser policy requires it.

`/extension/token` and `/extension/revoke` receive the specific middleware treatment required for a cookie-less extension client; this does **not** make them unauthenticated business routes. Their handlers still require the appropriate authorization-code/token/session proof.

Origin checks are a browser-policy backstop, not the primary authentication mechanism. The trust boundary rests on the authorization code + PKCE + linked user/install/session/token checks.

No wildcard CORS authority is introduced.

## 10. Revocation / disconnect

The normal user-facing action is **Disconnect** in VM Prospector.

Disconnect revokes the extension link server-side.

Account disablement also invalidates extension authority because owner state is checked on every authorized request.

There is no need for an administrator or user to rotate a shared capture secret for ordinary hosted operation.

## 11. Legacy `vmrx1`

The reusable shared capture credential remains only for local/development compatibility.

Hosted operation refuses it after PR #275.

Old documents, screenshots or handoffs that instruct a Hosted Beta user to paste:

`vmrx1.<key_id>.<secret>`

are historical and superseded for ordinary hosted use.

## 12. Deployment/runtime distinction

Current merged `main` includes PR #275 at:

`c1bd054e45e09a22d3d8cf1e7aec629226f352e4`

The last independently verified live VPS release is still:

`d9750b008919bf2bfe42a848b0b454eeedd66f1f`

Therefore the account-linked extension model is **merged engineering truth** but must not be described as live Hosted Beta behavior until a deployed `/version` reports a containing SHA and real browser UAT succeeds.

## 13. Admin account operations

`/app/admin/users` is administrator-only.

Admin may:

- create user accounts;
- issue a new one-time password link;
- disable/reactivate users;
- grant/remove Admin role subject to last-admin safety rules;
- inspect created/last-sign-in/password-present state.

Accounts are retained rather than deleted so history and future attribution remain intact.

## 14. Invariants

- No public signup.
- User-domain membership does not imply access or Admin role.
- Gmail OAuth is separate from hosted identity.
- Extension authorization is separate from browser session authority.
- Extension authority is exactly the four-route capture contract.
- Approval is not sending authority.
- No automatic Sending is introduced by authentication.
- Secrets/tokens must never be printed in logs, rendered UI or documentation examples as real values.
