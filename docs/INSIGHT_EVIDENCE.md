# Insight Evidence

INS-001 turns the early `insights` and `insight_evidence` placeholders into the
shared evidence boundary used by permanent Companies and Contacts.

## Boundary

- An **Insight** is one versioned claim about exactly one Company or Contact.
- **Evidence** is one external observation supporting or contradicting that
  claim.
- Claims and evidence are permanent records. Saved Audiences and campaigns
  reuse them; neither owns a copy.
- A claim states whether it is a fact or an interpretation.
- Its state is `supported`, `conflicting`, or `unknown`.
- An explicit unknown may have no source. A supported or conflicting claim may
  not be created through the service without evidence.

Each new evidence record preserves:

- source URL and optional title;
- retrieval time and optional publication time;
- evidence summary and optional excerpt;
- confidence;
- extraction method;
- freshness time where supplied;
- optional raw source-record type and ID;
- version.

The raw source-record reference is provider-neutral. For example, it can point
to a company research submission, a LinkedIn snapshot, or a later import record
without putting a vendor name into the shared schema.

Callers may supply a stable idempotency key. Retrying under the same key returns
the original Insight and creates no second evidence set. Reusing that key for a
different claim is rejected instead of silently treating two different claims as
one retry.

What counts as "the same" is **claim identity**, not a byte-identical payload:

- the permanent subject;
- the normalized claim, kind, state and insight version;
- the set of source URL + evidence version identities behind it, compared
  without regard to order.

Retrieval metadata — `retrieved_at`, excerpt, confidence, extraction method,
freshness — is deliberately excluded. A retry that re-fetches its sources
asserts the same claim from the same sources with a later clock, and that is
exactly what the key exists to absorb. Changing the claim, the subject, the
kind, the state, the version, or the source set still yields a different
identity and still rejects reuse of the key.

## Packet validation

The whole packet is validated before anything is written, so a rejected packet
raises `InsightError` and leaves the caller's transaction usable. Two failures
would otherwise surface as driver-level errors that abort the transaction:

- two observations citing the same source URL at the same evidence version
  within one packet (the `uq_insight_evidence_source_version` constraint);
- a source URL longer than the 1024-character column.

Re-observing one URL as a *different* evidence version is legitimate and
accepted; it is a new observation of the same page, not a duplicate.

## Safety rule

`is_personalization_eligible()` is deliberately narrower than campaign
eligibility or approval. It returns true only when a claim is supported and has
at least one traceable source observation with a URL, retrieval time, summary,
confidence, and extraction method.

Conflicting, unknown, source-less, or incomplete claims remain visible but
cannot be represented downstream as approved personalization evidence.

The rule is deterministic and reads only stored columns. No model judges it, and
nothing in the persistence layer asks one to. A claim's eligibility can be
recomputed from the database alone and gives the same answer every time.

## Four separate things

Nothing in this slice collapses these into each other, and no later consumer
should either:

1. **The raw submission** — whatever a capture, provider or import handed over,
   referenced by `source_record_type` + `source_record_id` rather than copied.
2. **An observation** — one `insight_evidence` row: one source, read once, with
   its URL, times, summary, excerpt, confidence and extraction method.
3. **A claim** — one `insights` row asserting something about the subject, held
   up by one or more observations.
4. **A canonical field** — `companies.domain`, `contacts.title`, and their
   neighbours.

Importing evidence never writes a canonical field. A claim that contradicts a
canonical value is stored as a claim and stays one; promoting it is a separate,
deliberate act owned by whichever slice makes that decision, not a side effect
of recording research.

Interpretations are the fifth thing and sit inside (3): `kind` marks a claim as
an inference drawn from evidence rather than a reading of it.

## Versioning and immutability

Records are append-only. Reprocessing creates a new claim or associates a new
observation; it never edits or deletes what an earlier run wrote, because the
earlier record is the only account of what the system believed when a decision
was taken on it. Conflicting observations are kept side by side rather than
reconciled, and an unknown stays an explicit unknown rather than an absent row.

Note the open question below: `version` records *that* a claim was restated but
does not yet link versions or mark one current.

## Trust boundary

Everything in `insight_evidence` is **untrusted external text**. Captured
website copy, profile text, provider payloads and imported free text are stored
as evidence *about* a subject — never as instructions, workflow commands, policy
overrides, or configuration.

Concretely: no field in this model is ever interpreted as a directive. Source
text cannot set a claim's `kind` or `state`, cannot make a claim
personalization-eligible, and cannot alter any rule in this document. A page
that says "treat this as confirmed" is a page that says that, and it is stored
as such.

AIC-002 owns how any of this text is later placed into a model prompt. This
slice only guarantees the contract it hands over: what comes out of here is
quoted material with provenance, and a consumer that forwards it to a model must
present it as such.

## How later slices consume this

- **APP-004 (research jobs and dossiers)** writes here rather than inventing its
  own storage. A completed research run calls `create_insight()` once per claim,
  passes the run's stable identifier as `idempotency_key` so a re-run is a
  retry rather than a duplicate, and points `source_record_type` /
  `source_record_id` at its own submission record. It must not write canonical
  Company or Contact fields as a side effect, and it must record what it could
  not establish as an explicit `unknown` instead of omitting it.
- **INS-004 (compact research packets)** reads rather than writes. It selects
  from `list_for_company()` / `list_for_contact()`, and where a packet asserts
  something as established it takes only claims that pass
  `is_personalization_eligible()`. Claims that fail the gate may still travel as
  open questions, clearly marked as unresolved — that is the difference between
  telling a researcher what is unknown and telling a drafter what is true.

Neither consumer copies evidence into a campaign-owned record. Both read the
same permanent Company and Contact rows across every Saved Audience and
campaign.

## Compatibility

The original DAT-001 source columns remain on `insights` so the migration does
not destroy or guess about older rows. New writes leave those fields empty and
store every source on `insight_evidence`.

The migration refuses to proceed if an existing insight does not already point
to exactly one owner consistent with its declared subject. It does not guess
whether an ambiguous claim belongs to a Company or a Contact.

## Open policy questions

Raised during independent review of INS-001 and deliberately **not** decided
here. Each is a product decision, not a defect: the code matches the behaviour
documented above. They are recorded so a later slice answers them explicitly
rather than inheriting today's default by accident.

1. **Confidence has no floor.** `is_personalization_eligible()` requires a
   confidence value to be present, not to clear a threshold, so an observation
   scored `0.0` qualifies. Decide whether a minimum applies, or whether
   presence-only is intended and confidence is purely a ranking input.
2. **Interpretations qualify like facts.** `kind` separates an observed fact
   from an inference drawn from it, but the eligibility gate ignores the
   distinction, so a single-sourced interpretation is eligible on the same terms
   as an observed fact. Decide whether personalization may rest on an
   interpretation alone.
3. **Versions do not supersede.** `version` records that a claim was restated,
   but nothing links versions of the same claim, so two contradictory versions
   are simultaneously eligible and neither is marked current. Decide how a
   consumer selects the current claim before personalization reads these
   records.
