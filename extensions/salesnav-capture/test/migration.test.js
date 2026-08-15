"use strict";
/**
 * Campaign-era local-state migration (DAT-013, revised by #280).
 *
 * The rule under test is that the transition is EXPLICIT: a v1 draft is cleared
 * rather than silently reinterpreted as a contact-first submission, and never
 * resent; campaign preferences are dropped.
 *
 * What changed in #280: the migration used to COPY each superseded draft into
 * `cc_legacy_v1_archive` and write a one-time notice, because the panel offered
 * the archive as a JSON download. That download, its card and the `downloads`
 * permission are gone, so the archive had no reader left. It is no longer
 * written — and both obsolete keys are cleared from installs that already have
 * them, which is the only reason `CONTACT_STORAGE.LEGACY_ARCHIVE` is still
 * named anywhere.
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
});

test("a campaign-era batch draft is cleared, never resent and never archived", () => {
  const draft = {
    clientBatchId: "batch-1",
    records: [{ rawFullName: "Dana Whitfield" }],
  };
  const result = plan({ [STORAGE.DRAFT_BATCH]: draft });

  assert.equal(result.needed, true);
  assert.ok(result.remove.includes(STORAGE.DRAFT_BATCH));
  // Nothing in the plan sends, rebuilds or copies it.
  assert.equal(result.set[STORAGE.DRAFT_BATCH], undefined);
  assert.equal(
    result.set[CONTACT_STORAGE.LEGACY_ARCHIVE],
    undefined,
    "the archive existed only to be downloaded; writing one now would strand data " +
      "no shipped code can read or remove"
  );
});

test("profile and company drafts are cleared alongside the batch", () => {
  const result = plan({
    [STORAGE.DRAFT_BATCH]: { clientBatchId: "b" },
    [PROFILE_STORAGE.DRAFT_PROFILE]: { clientCaptureId: "p" },
    [PROFILE_STORAGE.DRAFT_COMPANY]: { clientCaptureId: "c" },
  });
  for (const key of [
    STORAGE.DRAFT_BATCH,
    PROFILE_STORAGE.DRAFT_PROFILE,
    PROFILE_STORAGE.DRAFT_COMPANY,
  ]) {
    assert.ok(result.remove.includes(key), `${key} must be cleared`);
  }
  assert.deepEqual(Object.keys(result.set), []);
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

test("an archive left by an earlier version is cleared, not left stranded", () => {
  // The compatibility branch. An install that ran the archiving version is
  // holding captured personal data under a key nothing can now read, show or
  // delete — so the migration removes it. This is the one piece of legacy
  // handling retained, and it is why the key is still named in constants.
  const existing = { archivedAt: "2026-01-01T00:00:00.000Z", drafts: { a: 1 } };
  const result = plan({
    [CONTACT_STORAGE.LEGACY_ARCHIVE]: existing,
    [CONTACT_STORAGE.MIGRATION_NOTICE]: { message: "old" },
  });
  assert.equal(result.needed, true);
  assert.ok(result.remove.includes(CONTACT_STORAGE.LEGACY_ARCHIVE));
  assert.ok(result.remove.includes(CONTACT_STORAGE.MIGRATION_NOTICE));
  assert.deepEqual(Object.keys(result.set), []);
});

test("no migration notice is written, because nothing renders one", () => {
  const result = plan({
    [STORAGE.DRAFT_BATCH]: { clientBatchId: "b" },
    [STORAGE.PREFERENCES]: { lastCampaignId: "camp-1" },
  });
  assert.equal(result.set[CONTACT_STORAGE.MIGRATION_NOTICE], undefined);
  assert.equal(result.notice, undefined);
});

test("runMigration applies the plan to a storage adapter exactly once", async () => {
  const store = {
    [STORAGE.DRAFT_BATCH]: { clientBatchId: "b" },
    [STORAGE.PREFERENCES]: { lastCampaignId: "camp-1" },
    [CONTACT_STORAGE.LEGACY_ARCHIVE]: { drafts: { a: 1 } },
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
  assert.equal(CONTACT_STORAGE.LEGACY_ARCHIVE in store, false);
  assert.equal("lastCampaignId" in store[STORAGE.PREFERENCES], false);

  // Idempotent: running again with nothing left to do changes nothing.
  const second = await migration.runMigration(adapter, { migratedAt: AT });
  assert.equal(second.needed, false);
});
