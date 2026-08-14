# Campaign access: ownership, assignment and authorization

Who can see and use a campaign, where the rule lives, and what a reviewer should
try to break.

Everything below is enforced in `app/services/campaign_access.py`. If a rule is
not in that module it is not a rule — a template that hides a campaign is a
courtesy, and the module is the control.

## The rules

| Role | Sees | May change assignment |
| --- | --- | --- |
| `ADMIN` | Every campaign, including ones with no recorded owner | Yes, on every campaign |
| `USER` | Campaigns they created, plus campaigns explicitly assigned to them | No |

A campaign may be assigned to any number of users, and a user may be assigned any
number of campaigns. Both facts are rows:

* `campaigns.created_by_user_id` — nullable FK to `users.id`, `ON DELETE SET NULL`.
* `campaign_user_assignments` — `(campaign_id, user_id, assigned_by_user_id, created_at)`
  with `UNIQUE (campaign_id, user_id)`.

Nothing is inferred. There is no rule based on an email domain, a name
convention, or the `actor` string in the audit trail — the answer to "why can
this person see this campaign?" is always a row somebody wrote.

## Historical campaigns

Campaigns created before the migration keep `created_by_user_id = NULL`, and the
migration does not backfill it. The database records `actor = "operator"` — a
constant, not an identity — so any backfill would be a guess that becomes
indistinguishable from a fact.

The consequence is stated rather than hidden:

* Administrators reach every historical campaign, exactly as before.
* A normal user reaches one only after an administrator assigns it.

## Where the rule is applied

**Path.** `require_campaign_path_access` is a router-level dependency on every
router that can carry a `{campaign_id}` path parameter: the v2 operator product,
the legacy web routes, the Admin Workbench, and both API routers. A campaign
route added later is scoped when it is registered, not when somebody remembers.

* An administrator passes without a query. Existence stays the handler's
  business, which already has a specific answer for "no such campaign".
* A path parameter that is not a UUID is left alone — the handler answers it
  better than a 403 would.
* Everything else is refused before the handler body runs, writes included.

**Query and form.** A campaign named in a query string or a form body is checked
by the handler that reads it, because only the handler knows which parameter
carries one. The current set:

* `GET /app/review?campaign=` and `GET /app/contacts/{id}?campaign=`
* `POST /contacts/add-to-campaign` (form `campaign_id`)
* `POST /api/intake/contact-captures` (body `campaign_id`)

**Id-keyed writes.** Review decisions are keyed by a draft or sequence id, not by
a campaign id, so the path dependency never sees them. Every one of them resolves
its campaign and asks the same question:
`POST /app/review/{draft_id}/approve|discard`,
`POST /app/review/sequence/messages/{version_id}/approve|discard|edit`,
`POST /app/review/sequence/{sequence_id}/approve`, and
`POST /app/review/sequence/{sequence_id}/gmail-drafts`.

**Lists.** `campaigns.list_campaigns` and
`seller.campaign_offerings.campaigns_for_offering` take a required `actor`
argument. A caller that genuinely wants everything passes
`campaign_access.UNENFORCED`, which is a visible claim in a diff; an omission is
an error at the call site.

The review queue, the sequence queue and the contact page's membership list carry
a `campaign_ids` restriction separate from the operator's own campaign filter.
`None` means unrestricted; an empty set means nothing, which is the direction a
set-membership filter has to fail in.

## Refusals

One shape, from one handler in `app/main.py`:

```json
{"error": "campaign_access_denied", "status": 403, "message": "..."}
```

403 rather than 404, for the reason `AdminRequiredError` already records:
campaign names are unique and administered, so a 404 hides little and sends
somebody who genuinely needs access looking for a broken link instead of asking
for the assignment they are missing. The body names no campaign, no owner and no
assignee.

`may_access_campaign` answers *access*, not existence. An administrator gets
`True` for any well-formed id and issues no query. For everybody else, "does not
exist" and "belongs to somebody else" are the same `False`, so the function is
not an existence oracle for ids a caller is guessing at.

## Where authentication is off

Local development and the test suite run with `AUTH__ENABLED` off. There is then
no account directory, no role and no user id, and the whole application is
already unauthenticated, so `CampaignActor.enforced` is `False` and every check
passes — the same trade `require_admin` next door already makes.

## The extension

A capture credential proves an *installation*, not a person: it carries no user
id and no role. Account linking is being built on a separate branch
(`fix/uat-extension-account-linking`), which is not merged here.

Until it lands:

* `GET /api/campaigns` keeps today's behaviour for a credential with no linked
  user. Narrowing it to nothing would break the shipped extension.
* Filing a capture into a campaign calls `may_access_campaign`, which refuses as
  soon as a user *is* resolvable and is not entitled to it.

When the branch lands it has one thing to do: put the resolved user id and role
into the request scope the way the session middleware already does
(`operator_user_id`, `operator_role`, `auth_enforced`). Every rule above then
applies to the extension with no further change, `GET /api/campaigns` returns
only that user's campaigns, and filing into an unauthorized campaign fails
closed.

## The administrator's screen

`/app/campaigns/{id}` carries a "Who can use this campaign" panel for
administrators: the creator, the assignees with who granted each one and when,
an unassign button per assignee, and an assign control whose options come from
the `users` table. The edit page states the same facts read-only and links back.

The two writes are `POST /app/admin/campaigns/{id}/assign` and `/unassign`, on a
router that carries `require_admin`, under a path prefix the middleware already
withholds. `assign_user` and `unassign_user` refuse a non-administrator actor
again inside the service: the router guard stops the request, the service guard
stops a future caller that arrives another way.

Unassigning takes effect on the next request. Access is computed from these rows
per request rather than copied into a session cookie, so revocation does not wait
for a sign-out or an expiry.

## One reclassification

`GET /app/agents` moved to the administrator surface, joining the control POST
that was already there. The monitor names every campaign carrying an Agent
override and lists jobs across all of them, and it is not scoped to one person's
campaigns; scoping it would mean rewriting the reader the administrator surfaces
share. Per-campaign Agent work is untouched — rerun, override and stage actions
live under `/app/campaigns/{id}/...` and stay with whoever the campaign is
assigned to.

`tests/test_route_authorization.py` records this as a decision, and its
conformance test fails if any route's classification changes without one.
