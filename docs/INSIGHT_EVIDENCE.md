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

Callers may supply a stable idempotency key. Repeating the same packet with the
same key returns the original Insight and creates no second evidence set.
Reusing that key for changed content is rejected instead of silently treating
two different claims as one retry.

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
