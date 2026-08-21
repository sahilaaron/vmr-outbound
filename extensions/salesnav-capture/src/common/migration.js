/**
 * Legacy local-state migration (DAT-013).
 *
 * The campaign-era extension kept three kinds of state in
 * `chrome.storage.local`: a Sales Navigator draft batch, a reviewed profile
 * draft, and preferences that remembered the last selected campaign — plus the
 * staged-result summaries those produced.
 *
 * The contact-first workflow cannot honestly reinterpret any of it:
 *
 *  - a v1 draft was reviewed and approved *for a campaign*, and its
 *    `client_batch_id` / `client_capture_id` are idempotency keys the backend
 *    may already have accepted under the old contract;
 *  - re-sending the same reviewed content under a new contract would either
 *    conflict or silently create a second copy of the same evidence.
 *
 * So the superseded draft keys are cleared and `lastCampaignId` is dropped from
 * preferences. Nothing archived is ever transmitted.
 *
 * WHAT THIS USED TO DO, AND WHY IT NO LONGER DOES. A legacy draft was copied
 * verbatim into one archive key first, and the panel showed an "Archived drafts
 * can still be downloaded" card offering it as a JSON download. That was the
 * archive's only purpose — nothing else ever read it. With the extension's
 * download capability removed there is no way to reach it and no reason to keep
 * writing it, so the archive is no longer created.
 *
 * THE ONE PIECE OF COMPATIBILITY LOGIC RETAINED, and why it cannot be deleted
 * yet: an install that ran an earlier version already has `cc_legacy_v1_archive`
 * and `cc_migration_notice` sitting in `chrome.storage.local`. Neither is
 * readable by anything that ships now. Leaving them would strand captured
 * personal data in local storage indefinitely with no code path that can ever
 * show or remove it, so both keys are cleared when they are found. That branch
 * goes when there are no installs left that could still be carrying them.
 *
 * The function is pure over a plain object so it is fully unit-testable without
 * a browser.
 *
 * UMD module -> Node CommonJS + self.SNCapture.migration
 */
(function (root, factory) {
  const g = typeof self !== "undefined" ? self : root;
  const isNode = typeof module !== "undefined" && module.exports;
  const mod = factory(isNode ? require("./constants.js") : g.SNCapture.constants);
  if (isNode) module.exports = mod;
  g.SNCapture = Object.assign(g.SNCapture || {}, { migration: mod });
})(typeof globalThis !== "undefined" ? globalThis : this, function (constants) {
  "use strict";

  const { STORAGE, PROFILE_STORAGE, CONTACT_STORAGE, DEFAULT_PREFERENCES } = constants;

  // Every key the campaign-era extension could have written. Draft keys are
  // archived and cleared; result keys are cleared (they only summarize a batch
  // that no longer has a live reviewed source).
  const LEGACY_DRAFT_KEYS = [
    STORAGE.DRAFT_BATCH,
    PROFILE_STORAGE.DRAFT_PROFILE,
    PROFILE_STORAGE.DRAFT_COMPANY,
  ];
  const LEGACY_RESULT_KEYS = [
    STORAGE.LAST_RESULT,
    PROFILE_STORAGE.LAST_PROFILE_RESULT,
    PROFILE_STORAGE.LAST_COMPANY_RESULT,
  ];
  // Preference keys the contact-first workflow has no concept of.
  const REMOVED_PREFERENCE_KEYS = ["lastCampaignId"];
  // The reviewed-set ceiling BEFORE one save became a chunked push. It was both
  // the default and the hard maximum, so an install carrying exactly this value
  // is carrying the old default rather than a choice anybody made — and leaving
  // it would silently cap an upgraded install at a tenth of what it can now
  // save, with no visible reason and no message. A value the operator actually
  // chose (anything else) is left alone.
  const SUPERSEDED_MAX_RECORDS = 500;
  // Keys written by an earlier version of this module that nothing reads any
  // more. See the module docstring: they are cleared rather than left behind.
  const OBSOLETE_KEYS = [CONTACT_STORAGE.LEGACY_ARCHIVE, CONTACT_STORAGE.MIGRATION_NOTICE];

  /**
   * Decide what to do with a snapshot of `chrome.storage.local`.
   *
   * @param {object} stored a plain read of every relevant storage key
   * @param {{migratedAt: string}} options a caller-supplied timestamp (the
   *        module never reads the clock itself, so results stay deterministic)
   * @returns {{needed: boolean, set: object, remove: string[]}}
   */
  function planMigration(stored, options) {
    const data = stored || {};
    const opts = options || {};
    void opts;
    const set = {};
    const remove = [];

    let draftCount = 0;
    for (const key of LEGACY_DRAFT_KEYS) {
      const value = data[key];
      if (value == null) continue;
      draftCount += 1;
      remove.push(key);
    }
    for (const key of LEGACY_RESULT_KEYS) {
      if (data[key] != null) remove.push(key);
    }
    // The export-only state an earlier version left behind.
    for (const key of OBSOLETE_KEYS) {
      if (data[key] != null) remove.push(key);
    }

    const prefs = data[STORAGE.PREFERENCES];
    let prefsChanged = false;
    let nextPrefs = null;
    if (prefs && typeof prefs === "object") {
      nextPrefs = Object.assign({}, prefs);
      for (const key of REMOVED_PREFERENCE_KEYS) {
        if (key in nextPrefs) {
          delete nextPrefs[key];
          prefsChanged = true;
        }
      }
      // A campaign-era mock receiver pointed at the old staging route.
      if (
        typeof nextPrefs.mockReceiverUrl === "string" &&
        /\/api\/intake\/(sales-navigator|linkedin-profile)\/stage$/.test(nextPrefs.mockReceiverUrl)
      ) {
        nextPrefs.mockReceiverUrl = DEFAULT_PREFERENCES.mockReceiverUrl;
        prefsChanged = true;
      }
      // The stored copy of a ceiling that has moved. See SUPERSEDED_MAX_RECORDS.
      if (nextPrefs.maxRecordsPerBatch === SUPERSEDED_MAX_RECORDS) {
        nextPrefs.maxRecordsPerBatch = DEFAULT_PREFERENCES.maxRecordsPerBatch;
        prefsChanged = true;
      }
    }

    const needed = draftCount > 0 || remove.length > 0 || prefsChanged;
    if (!needed) return { needed: false, set: {}, remove: [] };

    if (prefsChanged && nextPrefs) set[STORAGE.PREFERENCES] = nextPrefs;
    return { needed: true, set, remove };
  }

  /** Every storage key `planMigration` needs to inspect. */
  function keysToRead() {
    return LEGACY_DRAFT_KEYS.concat(LEGACY_RESULT_KEYS, OBSOLETE_KEYS, [STORAGE.PREFERENCES]);
  }

  /**
   * Apply a plan to `chrome.storage.local`. Kept separate from `planMigration`
   * so the decision logic stays pure and testable.
   */
  async function runMigration(storage, options) {
    const stored = await storage.get(keysToRead());
    const plan = planMigration(stored, options);
    if (!plan.needed) return plan;
    if (Object.keys(plan.set).length) await storage.set(plan.set);
    if (plan.remove.length) await storage.remove(plan.remove);
    return plan;
  }

  return {
    planMigration,
    runMigration,
    keysToRead,
    LEGACY_DRAFT_KEYS,
    LEGACY_RESULT_KEYS,
    REMOVED_PREFERENCE_KEYS,
    OBSOLETE_KEYS,
    SUPERSEDED_MAX_RECORDS,
  };
});
