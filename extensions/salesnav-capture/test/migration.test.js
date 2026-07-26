"use strict";
/**
 * Campaign-era local-state migration (DAT-013).
 *
 * The rule under test is that the transition is EXPLICIT: a v1 draft is
 * archived verbatim and cleared, never silently reinterpreted as a
 * contact-first submission and never resent; campaign preferences are dropped;
 * and the operator is told what happened.
 */
const { test } = require("node:test");
const assert = require("node:assert/strict");

const migration = require("../src/common/migration.js");
const constants = require("../src/common/constants.js");

const AT = "2026-07-26T12:00:00.000Z";
const { STORAGE, PROFILE_STORAGE, CONTACT_STORAGE, DEFAULT_PREFERENCES } = constants;

function plan(stored) {
  return migration.planMigration(stored, { migratedAt: AT });
}

test("a clean install needs no migration", () => {
  const result = plan({});
  assert.equal(result.needed, false);
  assert.deepEqual(result.set, {});
  assert.deepEqual(result.remove, []);
  assert.equal(result.notice, null);
});

test("a campaign-era batch draft is archived verbatim and cleared, never resent", () => {
  const draft = {
    clientBatchId: "batch-1",
    records: [{ rawFullName: "Dana Whitfield" }],
  };
  const result = plan({ [STORAGE.DRAFT_BATCH]: draft });

  assert.equal(result.needed, true);
  const archive = result.set[CONTACT_STORAGE.LEGACY_ARCHIVE];
  assert.deepEqual(archive.drafts[STORAGE.DRAFT_BATCH], draft);
  assert.equal(archive.archivedAt, AT);
  assert.ok(archive.contracts.includes("salesnav-capture/1.0.0"));
  assert.ok(result.remove.includes(STORAGE.DRAFT_BATCH));
  // The archive is storage only: nothing in the plan sends or rebuilds it.
  assert.equal(result.set[STORAGE.DRAFT_BATCH], undefined);
});

test("profile and company drafts are archived alongside the batch", () => {
  const result = plan({
    [STORAGE.DRAFT_BATCH]: { clientBatchId: "b" },
    [PROFILE_STORAGE.DRAFT_PROFILE]: { clientCaptureId: "p" },
    [PROFILE_STORAGE.DRAFT_COMPANY]: { clientCaptureId: "c" },
  });
  const archive = result.set[CONTACT_STORAGE.LEGACY_ARCHIVE];
  assert.equal(Object.keys(archive.drafts).length, 3);
  assert.equal(result.notice.archivedDraftCount, 3);
});

test("stale staged-result summaries are cleared", () => {
  const result = plan({
    [STORAGE.LAST_RESULT]: { stagingId: "s" },
    [PROFILE_STORAGE.LAST_PROFILE_RESULT]: { snapshotId: "p" },
  });
  assert.equal(result.needed, true);
  assert.ok(result.remove.includes(STORAGE.LAST_RESULT));
  assert.ok(result.remove.includes(PROFILE_STORAGE.LAST_PROFILE_RESULT));
});

test("the campaign preference is dropped and the mock receiver retargeted", () => {
  const result = plan({
    [STORAGE.PREFERENCES]: {
      backendBaseUrl: "http://127.0.0.1:8000",
      lastCampaignId: "camp-1",
      mockReceiverUrl: "http://127.0.0.1:8787/api/intake/sales-navigator/stage",
      sendTarget: "backend",
    },
  });
  const prefs = result.set[STORAGE.PREFERENCES];
  assert.equal("lastCampaignId" in prefs, false);
  assert.equal(prefs.mockReceiverUrl, DEFAULT_PREFERENCES.mockReceiverUrl);
  // Unrelated operator settings survive untouched.
  assert.equal(prefs.backendBaseUrl, "http://127.0.0.1:8000");
  assert.equal(prefs.sendTarget, "backend");
  assert.equal(result.notice.campaignSelectionRemoved, true);
});

test("a custom mock receiver the operator chose is left alone", () => {
  const result = plan({
    [STORAGE.PREFERENCES]: {
      lastCampaignId: "camp-1",
      mockReceiverUrl: "http://localhost:9999/custom",
    },
  });
  assert.equal(result.set[STORAGE.PREFERENCES].mockReceiverUrl, "http://localhost:9999/custom");
});

test("migration is idempotent and never overwrites an existing archive", () => {
  const existing = { archivedAt: "2026-01-01T00:00:00.000Z", drafts: { a: 1 } };
  const result = plan({
    [CONTACT_STORAGE.LEGACY_ARCHIVE]: existing,
    [STORAGE.DRAFT_BATCH]: { clientBatchId: "b" },
  });
  assert.equal(result.set[CONTACT_STORAGE.LEGACY_ARCHIVE], undefined);
  assert.equal(result.notice.archivedDraftCount, 0);
  assert.ok(result.remove.includes(STORAGE.DRAFT_BATCH));

  // Running again with nothing left to do is a no-op.
  assert.equal(plan({ [CONTACT_STORAGE.LEGACY_ARCHIVE]: existing }).needed, false);
});

test("the notice explains what happened in the operator's terms", () => {
  const result = plan({
    [STORAGE.DRAFT_BATCH]: { clientBatchId: "b" },
    [STORAGE.PREFERENCES]: { lastCampaignId: "camp-1" },
  });
  assert.match(result.notice.message, /archived, not sent/);
  assert.match(result.notice.message, /Campaign selection has been removed/);
});

test("runMigration applies the plan to a storage adapter exactly once", async () => {
  const store = {
    [STORAGE.DRAFT_BATCH]: { clientBatchId: "b" },
    [STORAGE.PREFERENCES]: { lastCampaignId: "camp-1" },
  };
  const adapter = {
    async get(keys) {
      const out = {};
      for (const key of keys) if (key in store) out[key] = store[key];
      return out;
    },
    async set(values) {
      Object.assign(store, values);
    },
    async remove(keys) {
      for (const key of keys) delete store[key];
    },
  };

  const first = await migration.runMigration(adapter, { migratedAt: AT });
  assert.equal(first.needed, true);
  assert.equal(STORAGE.DRAFT_BATCH in store, false);
  assert.ok(store[CONTACT_STORAGE.LEGACY_ARCHIVE]);
  assert.equal("lastCampaignId" in store[STORAGE.PREFERENCES], false);

  const second = await migration.runMigration(adapter, { migratedAt: AT });
  assert.equal(second.needed, false);
});
