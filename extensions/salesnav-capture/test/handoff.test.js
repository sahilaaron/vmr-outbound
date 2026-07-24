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
  assert.equal(handoff.isOpenableWorkbenchUrl("http://127.0.0.1:8000/contacts/abc"), false);
  assert.equal(handoff.isOpenableWorkbenchUrl("file:///imports/abc"), false);
  assert.equal(handoff.isOpenableWorkbenchUrl(""), false);
  assert.equal(handoff.isOpenableWorkbenchUrl(null), false);
  assert.equal(handoff.isOpenableWorkbenchUrl("not a url"), false);
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
