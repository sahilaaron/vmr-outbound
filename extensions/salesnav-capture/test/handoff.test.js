"use strict";
const { test } = require("node:test");
const assert = require("node:assert/strict");
const handoff = require("../src/common/handoff.js");

// --- isOpenableWorkbenchUrl: only a known local workbench destination --------

test("openable workbench URL: loopback /imports/ and /workbench/ are allowed", () => {
  assert.equal(handoff.isOpenableWorkbenchUrl("http://127.0.0.1:8000/imports/abc-123"), true);
  assert.equal(handoff.isOpenableWorkbenchUrl("http://localhost:8000/imports/abc"), true);
  assert.equal(handoff.isOpenableWorkbenchUrl("http://127.0.0.1:8787/workbench/imports/stg_1"), true);
});

test("openable workbench URL: non-loopback, bad scheme, or unexpected path refused", () => {
  assert.equal(handoff.isOpenableWorkbenchUrl("http://evil.example/imports/abc"), false);
  assert.equal(handoff.isOpenableWorkbenchUrl("https://linkedin.com/imports/abc"), false);
  assert.equal(handoff.isOpenableWorkbenchUrl("javascript:alert(1)//127.0.0.1/imports/"), false);
  // Unexpected paths stay refused even on loopback.
  assert.equal(handoff.isOpenableWorkbenchUrl("http://127.0.0.1:8000/admin/abc"), false);
  assert.equal(handoff.isOpenableWorkbenchUrl("http://127.0.0.1:8000/"), false);
  assert.equal(handoff.isOpenableWorkbenchUrl("file:///imports/abc"), false);
  assert.equal(handoff.isOpenableWorkbenchUrl(""), false);
  assert.equal(handoff.isOpenableWorkbenchUrl(null), false);
  assert.equal(handoff.isOpenableWorkbenchUrl("not a url"), false);
});

test("contact-first record destinations are openable on loopback", () => {
  // DAT-013 added the two destinations the contact-first flow returns.
  assert.equal(handoff.isOpenableWorkbenchUrl("http://127.0.0.1:8000/contact-captures/abc"), true);
  assert.equal(
    handoff.isOpenableWorkbenchUrl("http://127.0.0.1:8000/contact-captures/submissions/abc"),
    true
  );
  assert.equal(handoff.isOpenableWorkbenchUrl("http://127.0.0.1:8000/contacts/abc"), true);
  assert.equal(handoff.isOpenableWorkbenchUrl("http://evil.example/contact-captures/abc"), false);
});

// --- sanitizeStageResult: safe, recoverable summary only --------------------

test("sanitizeStageResult keeps ids/counts + openable URL, drops raw body", () => {
  const body = {
    staging_id: "b1",
    client_batch_id: "cb-1",
    record_count: 3,
    warnings: [{ code: "x" }, { code: "y" }],
    already_received: true,
    expires_at: "2026-07-24T00:00:00Z",
    operator_workbench_url: "http://127.0.0.1:8000/imports/b1",
    secret_extra: "should not be copied",
  };
  const r = handoff.sanitizeStageResult(body, { campaignId: "camp-1", stagedAt: "2026-07-23T00:00:00Z" });
  assert.equal(r.stagingId, "b1");
  assert.equal(r.clientBatchId, "cb-1");
  assert.equal(r.recordCount, 3);
  assert.equal(r.warningCount, 2);
  assert.equal(r.alreadyReceived, true);
  assert.equal(r.workbenchUrl, "http://127.0.0.1:8000/imports/b1");
  assert.equal(r.campaignId, "camp-1");
  // No arbitrary body fields leak into the persisted result.
  assert.equal(Object.prototype.hasOwnProperty.call(r, "secret_extra"), false);
});

test("sanitizeStageResult drops a non-loopback workbench URL", () => {
  const r = handoff.sanitizeStageResult(
    { staging_id: "b2", operator_workbench_url: "http://evil.example/imports/b2" },
    {}
  );
  assert.equal(r.workbenchUrl, null);
});

// --- describeSendError: stable, distinct, PII-free classification ------------

test("describeSendError distinguishes backend error codes", () => {
  const cases = {
    campaign_invalid: /campaign is invalid/i,
    validation_failed: /validation/i,
    payload_too_large: /too large/i,
    unauthorized: /refused/i,
    timeout: /timed out/i,
    client_batch_id_conflict: /already staged with different content/i,
    internal_error: /unexpected/i,
  };
  for (const [code, re] of Object.entries(cases)) {
    const d = handoff.describeSendError({ ok: false, error: "receiver_rejected", status: 409, body: { error: code } });
    assert.equal(d.code, code, `code for ${code}`);
    assert.match(d.headline, re, `headline for ${code}`);
  }
});

test("describeSendError distinguishes transport failures", () => {
  assert.equal(handoff.describeSendError({ error: "timeout" }).code, "timeout");
  assert.equal(handoff.describeSendError({ error: "network_error" }).code, "network_error");
  assert.equal(handoff.describeSendError({ error: "permission_denied" }).code, "permission_denied");
  assert.equal(handoff.describeSendError({ error: "origin_not_allowed" }).code, "origin_not_allowed");
  assert.equal(handoff.describeSendError({ error: "empty_batch" }).canRetry, false);
});

test("transport failures describe the transport, not one deployment shape", () => {
  // These two failures come from the connection, not from the target. The copy
  // used to ask whether the backend was "running on the configured loopback
  // port" — a guess dressed as a diagnosis, since the same abort is produced by
  // a busy backend, a mistyped address, or a host the server refuses. Naming
  // loopback also bakes today's only deployment shape into wording that will
  // outlive it.
  for (const code of ["timeout", "network_error"]) {
    const d = handoff.describeSendError({ error: code });
    assert.equal(d.code, code);
    assert.equal(d.canRetry, true, `${code} must stay retryable`);
    const copy = `${d.headline} ${d.detail}`;
    assert.doesNotMatch(copy, /loopback/i, `${code} copy must not name loopback`);
    assert.doesNotMatch(copy, /localhost|127\.0\.0\.1/i, `${code} copy must not name a local host`);
    assert.doesNotMatch(copy, /\bport\b/i, `${code} copy must not name a port`);
    assert.match(d.headline, /backend/i, `${code} must still say what did not answer`);
    assert.ok(d.detail, `${code} should give the operator something to check`);
  }
});

test("configuration failures stay specific about what is supported", () => {
  // The mirror image of the test above. These are not transport failures: they
  // are the extension refusing a target, and the operator can only act on them
  // if the copy says what a valid target looks like. Genericising these would
  // make them useless.
  const origin = handoff.describeSendError({ error: "origin_not_allowed" });
  assert.match(origin.headline, /127\.0\.0\.1|localhost/i);
  assert.equal(origin.canRetry, false);
  assert.match(handoff.describeSendError({ error: "permission_denied" }).headline, /permission/i);
});

test("describeSendError never surfaces the raw response body", () => {
  const d = handoff.describeSendError({
    ok: false,
    error: "receiver_rejected",
    status: 422,
    body: { error: "validation_failed", details: ["records[0].firstName secret value", "x"] },
  });
  // Only a count is shown, never the detail strings themselves.
  assert.match(d.detail, /2 validation issue/);
  assert.doesNotMatch(d.detail, /secret value/);
  assert.equal(d.canRetry, false);
});

test("describeSendError falls back safely for an unknown rejection", () => {
  const d = handoff.describeSendError({ ok: false, error: "receiver_rejected", status: 500, body: null });
  assert.match(d.headline, /HTTP 500/);
  assert.equal(d.code, "receiver_rejected");
});
