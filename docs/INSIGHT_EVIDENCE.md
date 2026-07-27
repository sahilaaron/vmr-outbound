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
