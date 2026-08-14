# Fable hostile UX / IA / UI audit context

**Prepared:** 14 August 2026

Use this brief together with the live Hosted Beta and [`CURRENT_PRODUCT_STATE.md`](CURRENT_PRODUCT_STATE.md).

## Audit target

Audit the actual Hosted Beta as a B2B outbound operating product, not as a collection of backend features.

Do not begin high-fidelity redesign yet. First determine whether the information architecture, navigation, workflow, terminology and role model are coherent.

## Live now

Last independently verified live SHA:

`d9750b008919bf2bfe42a848b0b454eeedd66f1f`

Live facts:

- hosted app is deployed and usable behind authentication;
- durable Admin-created VMR user accounts exist;
- real VM Prospector captures have reached Hosted Beta;
- capture→domain→Contact→Campaign filing has been proven on a real cohort;
- 18 distinct Campaign Contacts currently exist in the UAT campaign from the original 50-capture cohort;
- 32 original captures remain intentionally unresolved/pending for operator company/domain confirmation;
- the real pipeline is currently blocked at Research because Company Research is effectively off;
- Gmail Drafts and Email Sequences are enabled;
- automatic Sending is not implemented;
- the VPS currently lacks the Claude CLI executable, so model fallback is unavailable despite the feature switch being enabled.

## Merged but not yet verified live

Current merged `main`:

`c1bd054e45e09a22d3d8cf1e7aec629226f352e4`

PR #275 is merged and changes VM Prospector hosted authentication:

- ordinary users should not configure a backend URL or paste `vmrx1`;
- extension links to the operator's VMR account;
- first-party authorization-code + PKCE;
- short-lived access token + rotating refresh authority;
- disabled/revoked user/session fails closed;
- exact four-route capture authority only.

Treat that as imminent product behavior but not live evidence until deployed and browser-UAT proven.

## Known engineering repair in progress

Branch:

`feat/uat-operator-controls`

This branch is **not** current product truth yet.

It is implementing:

- password minimum 8 instead of 15;
- Campaign creator ownership;
- multi-user Campaign assignment;
- Admin sees all Campaigns;
- normal user sees only created/assigned Campaigns;
- server-side campaign/review/membership authorization;
- Admin-operable ordinary product controls instead of requiring `.env` edits for routine operation;
- supported recovery for work paused because a product control is off;
- `/app/agents` as an Admin-only global operational surface.

Testing on that branch also found and repaired directly related authorization gaps in review decisions, review fallback rendering and CampaignContact-id routes.

Do not waste the audit merely rediscovering those defects. You may still critique whether the resulting interaction/IA is understandable.

## Product invariants

Preserve these unless explicitly challenging them at the product-strategy level:

- Contact-first: save the person first; decide what to do with them later.
- Permanent Contacts and Companies are not owned by Campaigns.
- Research is authoritative evidence gathering.
- Company Intelligence is bounded context and does not silently become sourced Research authority.
- Human edits create immutable version lineage.
- A review row represents a real human action.
- Default approval is not human approval.
- Approval is not sending authority.
- Gmail mailbox authorization is separate from app login and extension authorization.
- Current product creates Gmail drafts; it does not automatically send.
- Seven sequence messages exactly: days 0, 3, 7, 12, 18, 25, 35.

## User roles to audit

### Normal user

Expected mental model:

- sign in;
- work only their own/assigned Campaigns;
- use VM Prospector to save prospects;
- resolve relevant exceptions;
- see Contacts progress through the pipeline;
- review/edit/approve personalized outreach;
- create Gmail drafts;
- not understand or operate server infrastructure.

### Admin

Expected mental model:

- see every Campaign;
- create/manage users;
- assign users to Campaigns;
- operate ordinary product controls;
- monitor global Agent/jobs state;
- inspect/recover exceptions;
- never need SSH/`.env` for ordinary day-to-day product operation.

### New user

Assume no knowledge of:

- AgentIdentifier;
- feature flags;
- queues;
- provider capability gates;
- migration/runtime architecture;
- internal stage names unless the UI teaches them.

## Areas to attack

Audit:

- global navigation and route hierarchy;
- distinction between `/app`, `/admin`, Agent Studio and global Agent monitor;
- Campaign creation/detail/setup/assignment;
- Contact capture and Campaign filing;
- pending domain/company resolution;
- Contact detail and pipeline state;
- Research/Email/Verification/Insights/Personalization progression;
- review/edit/approval;
- Gmail handoff;
- People/user admin;
- operational configuration;
- Admin/global vs Campaign-scoped controls;
- terminology and error language;
- blocked/paused/unavailable states;
- empty states;
- progressive disclosure;
- density and table ergonomics;
- where engineering concepts leak into product UX;
- duplicated or contradictory control planes.

## Output expected

Produce:

1. ruthless current-state findings ranked by operator impact;
2. current IA map;
3. proposed IA;
4. ideal journeys for Admin, normal user and first-time user;
5. page inventory: KEEP / MERGE / MOVE / SPLIT / REDESIGN / REMOVE / NEW;
6. top 20 UX debts;
7. redesign principles;
8. product decisions/questions that must be settled before high-fidelity UI work.

After the operator-controls branch is eventually deployed, perform a short delta audit of the new Campaign ownership/assignment and Admin configuration experiences rather than restarting the whole audit.
