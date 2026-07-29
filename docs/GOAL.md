## Current Goal

Deliver one cohesive MVP with this defining outcome:

> **A user can capture 2,000 Sales Navigator contacts in the morning and begin sending AI-personalized verified emails that afternoon.**

This repository now prioritizes one complete Contact-to-send pipeline over isolated feature development.

## Canonical operating flow

1. Capture a person from Sales Navigator or LinkedIn into a permanent Contact.
2. Optionally auto-add that Contact to a selected Campaign through the extension.
3. Apply persistent Labels, backed by reusable Collections.
4. Resolve Contact identity and Company identity.
5. Resolve or reuse the Company domain.
6. Research the Company and store sourced facts.
7. Generate and verify email candidates in the locked order.
8. Generate AI company insights and campaign-specific scoring.
9. Accept the Contact into the Campaign as a Campaign Contact.
10. Generate personalized email copy using Campaign guardrails and seller context.
11. Review and approve the exact message version.
12. Submit approved records to the sending integration.
13. Track execution and failures through the Workbench.

## Capture and Campaign rule

Contacts are permanent and campaign-independent.

- A Campaign is never required to capture a Contact.
- The extension may hold one optional active Campaign selection.
- When present, that selection means: after Contact resolution, create or update Campaign Contact membership automatically.
- When absent, capture still completes normally.
- Campaign selection does not own, duplicate or alter the permanent Contact.
- Labels remain optional and may be applied with or without a Campaign.

## Collections and extension Labels

The backend uses **Collections**. The extension calls them **Labels**.

- A Contact may belong to many Collections.
- A Campaign may reference many Collections.
- The extension autocompletes existing Campaigns and Labels from the backend.
- Selected Labels and the optional Campaign persist across subsequent captures until deselected.
- Selected Labels attach to each captured Contact.
- A selected Campaign auto-adds each resolved Contact to that Campaign.

## Locked Agent order

The frontend calls workers **Agents**. The canonical order is:

1. Capture Agent
2. Identity Agent
3. Company Agent
4. Research Agent
5. Email Agent
6. Verification Agent
7. Insights Agent
8. Personalization Agent
9. Sending Agent

Each Agent must support visible state, retries, failure inspection, global enablement and Campaign-level overrides.

## Locked email-finding policy

Search at most three candidates per Contact and stop after a verified address is found.

### Company has more than 50 employees

1. `firstname.lastname`
2. `finitiallastname`
3. `lastnamefinitial`

### Company has 50 or fewer employees

1. `firstname`
2. `firstname.lastname`
3. `finitiallastname`

The strategy should be implemented through a versioned policy boundary so ordering can change later without rewriting the pipeline.

## Required MVP surfaces

- Workbench home
- Campaign list and creation
- Campaign workspace
- Contacts table and Contact detail
- Collections management
- Chrome extension Campaign and Label selectors
- Jobs and Agent monitoring
- Global Agent controls
- Campaign-level Agent controls
- Review queue
- Ready-to-send queue
- Sending and outcome status

## Workbench requirements

The Workbench is the operating control room. It must show:

- stage counts for every active Campaign;
- all Agents and their current states;
- queue depth, throughput and recent activity;
- waiting, running, paused, retrying, failed and completed jobs;
- drill-down to affected Contacts, Companies and Campaign Contacts;
- retry and pause controls;
- global Agent on/off controls;
- Campaign-level Agent overrides;
- an emergency stop for new sending work.

## Data boundaries

- Contact and Company are permanent canonical records.
- Campaign Contact owns Campaign-specific fit, acceptance, personalization, approval and send state.
- Collection membership is reusable and does not make a Contact outreach-eligible.
- Sourced facts remain separate from AI-derived insights.
- Exact-address verification remains separate from email-pattern observations.
- Suppression remains authoritative over every downstream stage.

## MVP acceptance criteria

The MVP is ready when one operator can:

- create and configure a Campaign;
- capture between 100 and 2,000 Contacts from operator-opened Sales Navigator pages;
- optionally persist a Campaign selection that auto-adds captured Contacts;
- persist one or more Labels across captures;
- see captures converge into permanent Contacts and Companies without duplication;
- reuse a resolved domain across Contacts sharing the same Sales Navigator company identity;
- run Company research, email discovery and verification automatically;
- generate AI insights and Campaign-specific personalized email copy;
- inspect failures and Agent state from the Workbench;
- pause or disable Agents globally or within one Campaign;
- review exact message versions before sending;
- submit only approved and currently eligible Campaign Contacts to the sending integration;
- retry completed or failed work without duplicate Contacts, Companies, memberships or messages.

## Immediate build order

1. Repository and planning consolidation
2. Campaign, Collection and Campaign Contact model alignment
3. Optional extension Campaign auto-add and persistent Labels
4. Agent orchestration and job-state model
5. Workbench Agent monitor and controls
6. Company research integration
7. Locked email-finding policy and verification sequence
8. AI insights and Campaign-specific personalization
9. Review and ready-to-send workflow
10. Sending integration and outcome tracking
11. End-to-end dry run and controlled pilot

## Explicitly deferred

The MVP does not require:

- autonomous LinkedIn navigation;
- CAPTCHA solving or access-control bypass;
- a general workflow builder;
- advanced analytics or experimentation;
- autonomous replies;
- omnichannel outreach;
- CRM replacement;
- multi-tenant SaaS;
- public billing or administration;
- arbitrary multi-agent orchestration outside the locked pipeline.

## Scope rule

A new feature belongs in the active MVP only when it materially advances a captured Contact toward a verified, personalized and approved email ready for sending, or when it provides necessary operational control over that path.
