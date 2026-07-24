"use strict";
const { test } = require("node:test");
const assert = require("node:assert/strict");

const handoff = require("../src/common/handoff.js");

test("sanitizeProfileStageResult keeps only safe identifiers and outcome", () => {
  const body = {
    snapshot_id: "abc-123",
    client_capture_id: "cap-1",
    outcome: "stored",
    warnings: [{ code: "x" }],
    received_at: "2026-07-24T10:00:00Z",
    already_received: true,
    operator_workbench_url: "http://127.0.0.1:8000/profiles/abc-123",
    // Anything echoing captured values must NOT survive sanitization.
    profile: { full_name: "Someone Sensitive" },
  };
  const r = handoff.sanitizeProfileStageResult(body, { campaignId: "c1", stagedAt: "2026-07-24T10:01:00Z" });
  assert.equal(r.snapshotId, "abc-123");
  assert.equal(r.outcome, "stored");
  assert.equal(r.warningCount, 1);
  assert.equal(r.alreadyReceived, true);
  assert.equal(r.workbenchUrl, "http://127.0.0.1:8000/profiles/abc-123");
  assert.equal(r.campaignId, "c1");
  assert.ok(!JSON.stringify(r).includes("Sensitive"));
});

test("profile workbench URLs open only on loopback /profiles/ paths", () => {
  assert.ok(handoff.isOpenableWorkbenchUrl("http://127.0.0.1:8000/profiles/abc"));
  assert.ok(!handoff.isOpenableWorkbenchUrl("https://evil.example.com/profiles/abc"));
  assert.ok(!handoff.isOpenableWorkbenchUrl("http://127.0.0.1:8000/other/abc"));
});

test("capture-id conflict is described as non-retryable with a clear message", () => {
  const d = handoff.describeSendError({
    ok: false,
    error: "receiver_rejected",
    status: 409,
    body: { error: "client_capture_id_conflict", status: 409 },
  });
  assert.equal(d.code, "client_capture_id_conflict");
  assert.equal(d.canRetry, false);
  assert.match(d.headline, /already staged with different content/i);
});
