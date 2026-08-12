# One-click Gmail draft creation (#267)

An approved hosted operator connects one Gmail mailbox, opens a sequence, clicks
**Create Gmail drafts**, and VMR writes one draft per current message version
into that mailbox. The operator sends each one by hand, in Gmail.

**This feature cannot send.** Not "does not"; cannot. The Gmail adapter
implements draft creation and one bounded draft lookup, there is no send method
on the provider protocol, no route reaches one, and
`tests/test_gmail_draft_integration.py` fails if a send endpoint is ever named
anywhere in the feature.

Not built, and not partially built: scheduling, cadence execution, mailbox
polling, automatic follow-ups, reply detection, Sheets, a sending adapter,
campaign automation, or a generic synchronization service.

---

## 1. Two authorities, three credentials

VMR now carries three separate credentials, and none may stand in for another:

| | What it proves | Where |
|---|---|---|
| `AUTH__*` | who this operator is | `app/core/auth/`, `/auth/*` |
| `EXTENSION_AUTH__*` | one approved capture extension install | `app/core/auth/extension.py` |
| `GMAIL__*` | permission to write a draft into one mailbox | `app/services/gmail/`, `/gmail/*` |

Signing in to VMR requests `openid email profile` and nothing else, exactly as
before. It never implies mailbox access, and no configuration change to the
identity client can grant it: the Gmail client id, secret and consent screen are
separate, and the identity path never consults them.

A Gmail consent begins **only** from an explicit `POST /gmail/connect`, which is
CSRF-protected and same-origin — a `GET` on that path is a 405. Nothing else in
the application starts one.

### The scope, and why

`https://www.googleapis.com/auth/gmail.compose`, plus `openid` and `email`.

`gmail.compose` is the narrowest scope Google documents for
`users.drafts.create`; the alternatives (`gmail.modify`, `https://mail.google.com/`)
are strictly wider and would grant inbox read access this feature has no use
for. Google offers no create-only draft scope, so `gmail.compose` does
technically permit `users.drafts.send` — which is exactly why the adapter
implements no send call and a test asserts it.

`openid email` are there for a specific reason rather than by habit.
`users.getProfile` — the obvious way to ask "whose mailbox is this?" — needs
`gmail.metadata` or wider, so using it would mean requesting *more* mailbox
access than drafting needs merely to learn an address. Asking for the identity
scopes instead makes Google return an ID token in the same response, which names
the account with a signature VMR already knows how to verify, and grants no
additional access to mail.

**Verification implication:** the granted scopes are read from Google's
response and checked for the compose scope before a mailbox is bound. What was
*requested* proves nothing — a consent screen where the operator unticks the
mailbox permission returns a narrower grant, and recording the request would
claim a capability the grant does not carry.

### What is proven before a mailbox is bound

Every one of these, none optional and none inferred:

| Check | Why |
|---|---|
| a live, approved operator session | `/gmail/*` is not on the anonymous allow-list; the callback is not a sign-in path |
| the signed, single-use transaction cookie decodes and has not expired | one consent, one transaction |
| it was signed with the Gmail transaction key | a key of its own, not the sign-in key with a discriminator: a `kind` field only helps the side that checks it, so one shared key would leave a Gmail transaction structurally valid as a sign-in transaction. Separate keys make each token verify for exactly one purpose in **both** directions |
| `state` equals the transaction's, constant-time | the classic CSRF on an OAuth callback |
| **the transaction's owning account equals the signed-in account** | a captured callback replayed into a second operator's browser cannot bind the first operator's mailbox to them. The claim is the durable `users.id`, not the Google subject: a password session has no subject at all, so a subject comparison would have read `"" == ""` between any two password operators and proven nothing |
| PKCE `code_verifier` | the code is bound to a verifier only this process held |
| RS256 signature against Google's JWKS, `aud` = the *Gmail* client id, `iss`, `nonce`, freshness, `email_verified` | reuses `app/core/auth/jwks.py` and `identity.py` verbatim rather than writing a second, weaker verifier |
| the granted scopes contain `gmail.compose` | see above |
| a refresh token was returned | a grant that dies in an hour is not a connection |

Every distinguishable failure of the round trip returns the *same* sentence.
Telling them apart would tell an attacker which check they defeated.

### Who a mailbox belongs to

`gmail_mailbox_grants.user_id` — a foreign key to `users.id`, and the only field
any authorization path reads. Every mailbox function takes that id and nothing
else: there is no variant keyed on an address, a Google subject or a role, so no
caller can reach another operator's mailbox by holding some other identifier for
it. "One live mailbox per owner" is a partial unique index on `user_id`, not a
convention.

This slice originally keyed ownership on `operator_subject`, Google's `sub`,
which was the only durable identity a session had before accounts existed. #273
made accounts durable rows and gave them a password path, and a password session
carries no subject at all — `OperatorSession` keeps the subject "for the audit
trail rather than for any access decision". The column survives as exactly that:
nullable provenance recording which Google sign-in authorized a mailbox, when one
did.

Two consequences worth stating, because both are load-bearing for the Beta:

- an administrator has no mailbox authority over anyone else. `is_admin` gates
  the account directory, and nothing in the Gmail path consults a role;
- disabling an account, or bumping its `auth_version`, refuses that operator's
  next request before any Gmail route runs. The grant row survives — it is the
  record of what was authorized — but nothing can act through it.

---

## 2. Tokens at rest

Fernet ciphertext in `gmail_mailbox_grants.encrypted_refresh_token` and
`encrypted_access_token`, with the key supplied from the environment
(`GMAIL__TOKEN_ENCRYPTION_KEY`) and never from the database. This reuses the
primitive the repository already uses for Agent Studio provider credentials
(`app/services/verification/studio.py`), with a **dedicated key**: rotating or
losing one credential domain's key has no effect on the other.

There is no fallback key and no "encode it for now" path. Without the key the
feature reports itself unavailable, because a token store that quietly degrades
to base64 is worse than one that refuses — the first looks like it is working.

Tokens do not appear in logs, tracebacks, HTML, API responses, settings dumps,
audit events, test snapshots or Git:

- both `GMAIL__CLIENT_SECRET` and `GMAIL__TOKEN_ENCRYPTION_KEY` carry
  `repr=False` / `exclude=True`, so neither reaches `repr(settings)` or
  `settings.model_dump()`;
- `GmailMailboxGrant.__repr__` is written by hand from four non-secret fields,
  so printing the object cannot leak a column;
- every error surfaced from the OAuth client and the Gmail adapter is a fixed
  sentence or a bounded category token (`http_400`, `timeout`, `unauthorized`) —
  Google's own body is never propagated, because it can echo the submitted code;
- the read model the templates see (`app/services/gmail/read.py`,
  `mailbox.MailboxState`) has no token field at all.

**Revocation and expiry** move the grant to `RECONNECT_REQUIRED`, drop the
stored ciphertext, and record a bounded category. The UI shows a
*Reconnect Gmail* state; nothing crashes and nothing retries silently.
**Disconnect** always forgets the local token and asks Google to revoke on a
best-effort basis — refusing to forget a token because a revocation request
failed would leave a usable credential behind for the sake of a request that may
never succeed. The operator is told which of the two happened.

---

## 3. Draft lineage

`gmail_draft_records` — one row per (mailbox account, exact message version) VMR
has tried to draft. Each row names:

Campaign Contact · Email Sequence (`sequence_id` and `sequence_key`) · the
logical message (`message_id`) · **the exact immutable message version**
(`message_version_id`) · the connected mailbox (`mailbox_grant_id`,
`mailbox_account_subject`, `mailbox_address`) · the Gmail draft id · Gmail's
message and thread ids when returned · the recipient · a SHA-256
`content_fingerprint` over the canonical (recipient, subject, body) · the RFC
`Message-ID` VMR minted · status, failure category, attempt count, timestamps.

Never `(contact, position)`. The exact version is the authority: a position
follows an edit onto text nobody drafted.

`ck_gmail_draft_records_created_draft_names_its_gmail_id` states the honesty
rule as a database fact — only a row claiming `CREATED` may carry a Gmail draft
id, and one that claims it must.

---

## 4. Idempotency, and the ambiguous case

The key is `unique (mailbox_account_subject, message_version_id)`.

**Keyed on the Google *account*, not the grant row.** A disconnect-and-reconnect
cycle writes a new grant for the same mailbox; keying on the grant would let
that cycle put a second identical draft in the same human's Drafts folder.

**The row is committed before the Gmail call.** That reservation is what makes
the key exist ahead of the external write, so a crash between the two is a known
state rather than a silent gap. It is also why `create_drafts` commits several
times: one transaction wrapped around several external writes is the arrangement
where a rollback erases the only local record that an external write happened.

Four statuses, because "did Gmail create this draft?" has four truthful answers:

| Status | Means | A retry |
|---|---|---|
| `RESERVED` | committed locally, outcome not yet recorded | **reconciles first** |
| `CREATED` | Gmail returned a draft id | reuses, and says so |
| `UNCONFIRMED` | timeout / dropped connection / 5xx — proves nothing | **reconciles first** |
| `FAILED` | a definite 4xx — proves no draft exists | attempts |

`RESERVED` reconciles for the same reason `UNCONFIRMED` does, and it is worth
being explicit about why, because treating it as "nothing happened yet" was a
real duplicate. A reservation is committed *before* the Gmail call and stays
`RESERVED` across it, so finding one on a later run means the process died
before the call, died after it, or another request is inside that window right
now — and nothing on the row distinguishes the three. Only the first is safe to
re-attempt. Both a worker killed mid-flight and a double-click therefore go
through the lookup below rather than writing a second copy.
`tests/test_gmail_draft_integration.py` pins both cases.

### The bounded reconciliation

Each draft carries a deterministic `Message-ID` derived from the exact message
version. Reconciling an `UNCONFIRMED` row is therefore **one** exact
`rfc822msgid:` query against `users.drafts.list` — not a scan of the mailbox,
not a generic sync service.

- match → adopt the draft id, status `CREATED`, reported as *already existed*;
- no match, and the attempt is younger than `RECONCILIATION_MIN_AGE_SECONDS`
  (60s) → stay `UNCONFIRMED` and say so. Gmail indexes a new draft
  asynchronously, so "not found" a second after the write is not evidence, and
  acting on it is exactly how a retry produces the duplicate this design exists
  to prevent;
- no match after the window → safe to attempt again.

The `Message-ID` is this message's *own* identity, which every RFC 5322 message
needs and which Gmail would otherwise assign. It is not a threading claim — see
§6.

---

## 5. Sequence correctness

The action lives on the **contact page**, which renders all seven bodies, and
deliberately not on the review queue, which renders one at a time. Offering it
there would let an operator put six bodies they have not read into a real
mailbox with one click — exactly the gap between "approved by default" and
"read" that the review model exists to keep visible. The review queue names the
connected mailbox and links to the contact page instead.

The action carries the exact `version_ids` the page rendered, the same way bulk
approval does. If that set is not exactly the set of current versions stored
right now, **nothing is drafted** and the operator is asked to reload. VMR never
silently fetches newer text and drafts something different from what the
operator clicked on.

Also refused outright, with nothing written to Gmail:

- a superseded sequence, a stopped sequence, an incomplete or validation-failed
  generation;
- a sequence whose feature switch or campaign opt-in has since been turned off
  (read-only for review is read-only for drafting — creating a draft is a *more*
  consequential action than the review decision the same page refuses);
- a suppressed contact, or a contact with no confirmed address.

Discarded messages are **skipped**, not refused: the sequence contract says a
discard stops the chain there, and the result line says how many were skipped.

Existing invariants are untouched: seven logical messages, the 0/3/7/12/18/25/35
cadence and its campaign override, immutable versions on edit, a review row
still meaning a human acted, default approval still distinct from human
approval. **Approval does not become send authority, and it does not become
draft authority either** — creating a draft is a separate, explicit operator
action.

### Editing after a draft exists

An edit writes a new message version. The historical draft row is left exactly
as it is — it is the record of what was actually put in the mailbox — and the
new version gets its own row and its own Gmail draft on the next click. Nothing
is rewritten invisibly, and the old Gmail draft is not silently mutated to hold
text it was not created with.

---

## 6. Threading: deferred, deliberately

Follow-up drafts are **standalone**. No `In-Reply-To`, no `References`, no
fabricated Gmail `threadId`.

Gmail anchors a reply thread on the `Message-ID` of a message that was actually
sent. Before the first sequence message has been sent there is no such
predecessor, so a reply-thread draft cannot be correctly established — only
convincingly faked. Faking it would make seven unrelated drafts look like a
conversation that never happened.

VMR's own predecessor lineage is preserved and untouched
(`email_sequence_messages.predecessor_message_id`). Gmail reply threading
belongs to the future delivery/sending adapter, after a real send produces a
real `Message-ID`. See `docs/EMAIL_SEQUENCE.md` §15.

---

## 7. Content

Plain text, built with `email.message.EmailMessage` and submitted as `raw` so
Gmail does not reassemble the MIME. Recipient from the authoritative
`Contact.email`; subject and body from the exact current message version,
unchanged.

Nothing is added: no tracking pixel, no unsubscribe infrastructure, no
signature, no template rewriting, no HTML alternative. There is no canonical
safe HTML representation anywhere in the application, so a `text/html` part
would have to be invented from the plain text — a content transformation this
slice is not permitted to make. Recipient and subject are refused if they carry
a line break, which is the header-injection case.

---

## 8. Configuration required before a live UAT

Nothing below has been done, and none of it belongs in this repository.

1. **A new Google Cloud OAuth 2.0 client** — Web application, in the same
   project or a separate one, *distinct from* the hosted sign-in client.
2. **Its consent screen lists exactly three scopes**: `openid`, `email`,
   `https://www.googleapis.com/auth/gmail.compose`. Anything wider is a finding.
3. **Enable the Gmail API** for that project.
4. **Authorised redirect URI**, byte for byte:
   `<AUTH__PUBLIC_BASE_URL>/gmail/callback` — e.g.
   `https://srv1885453.hstgr.cloud/gmail/callback`.
5. **Add the operator's Gmail address as a test user** while the consent screen
   is unverified, or the consent will be refused.
6. **Set the environment** in `/etc/vmr/vmr.env` and restart:
   `GMAIL__CLIENT_ID`, `GMAIL__CLIENT_SECRET`,
   `GMAIL__TOKEN_ENCRYPTION_KEY` (`Fernet.generate_key()`),
   `GMAIL__MESSAGE_ID_DOMAIN` (a domain the deployment controls),
   `FEATURES__GMAIL_DRAFTS=true`, with `FEATURES__EMAIL_SEQUENCES=true`.
7. **Run the migration**: `alembic upgrade head` (`a7d3e1c85f42`).
8. **Verification implication to record**: `gmail.compose` is a restricted
   scope. An unverified consent screen works for test users and shows the
   unverified warning; publishing it to anyone outside the test-user list
   requires Google's verification review. For a Beta with two or three named
   operators, staying on the test-user list is the smaller path.

Live consent and one real draft-creation UAT happen only after merge and deploy,
and only from an explicit operator click.

---

## 9. Known bounds carried deliberately

- **The reconciliation window is a wait, not a proof.** Within 60 seconds of an
  ambiguous failure the operator is told to try again shortly. That is the
  honest answer, and it is preferable to a guess in either direction.
- **A draft deleted by hand in Gmail still reads as existing.** VMR records that
  it created one; it does not poll the mailbox to find out it is gone, and
  polling is out of scope. The operator's remedy is to edit the message, which
  creates a new version and therefore a new draft.
- **One draft per message, seven at a time.** `docs/EMAIL_SEQUENCE.md` §15
  describes a future delivery model that creates one external draft at a time
  because follow-ups must be replies in a sent thread. That constraint belongs
  to *sending*; this slice is draft-only and standalone, so all seven are
  drafted together, which is what makes one click useful. When the delivery
  adapter is built, one-at-a-time returns with the threading it exists to serve.
- **The per-message draft chip is scoped to the reader's own mailbox.** A
  sequence belongs to a Campaign Contact rather than to an operator, so an
  unscoped read would show operator A the address of the mailbox operator B
  drafted into. `app/services/gmail/read.py` requires the account subject, and
  an operator with no mailbox connected sees no draft state at all — including
  for drafts that do exist in somebody else's.
- **No audit event is written.** The draft lineage row *is* the record, and it
  carries actor and timestamps. A second, weaker copy in the audit table would
  be a claim to maintain rather than a fact.
