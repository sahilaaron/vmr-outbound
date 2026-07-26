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
 * So the migration is explicit, not silent: a legacy draft is ARCHIVED
 * (preserved verbatim under one archive key, exportable as JSON), the live
 * draft keys are cleared, `lastCampaignId` is dropped from preferences, and a
 * one-time notice records what happened so the side panel can tell the
 * operator. Nothing is deleted without being archived first, and nothing
 * archived is ever transmitted.
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

  /**
   * Decide what to do with a snapshot of `chrome.storage.local`.
   *
   * @param {object} stored a plain read of every relevant storage key
   * @param {{migratedAt: string}} options a caller-supplied timestamp (the
   *        module never reads the clock itself, so results stay deterministic)
   * @returns {{needed: boolean, set: object, remove: string[], notice: object|null}}
   */
  function planMigration(stored, options) {
    const data = stored || {};
    const opts = options || {};
    const set = {};
    const remove = [];

    // Already archived once? Never archive twice — that would overwrite the
    // operator's only copy of the superseded draft.
    const alreadyArchived = !!data[CONTACT_STORAGE.LEGACY_ARCHIVE];

    const archivedDrafts = {};
    let draftCount = 0;
    for (const key of LEGACY_DRAFT_KEYS) {
      const value = data[key];
      if (value == null) continue;
      archivedDrafts[key] = value;
      draftCount += 1;
      remove.push(key);
    }
    for (const key of LEGACY_RESULT_KEYS) {
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
    }

    const needed = draftCount > 0 || remove.length > 0 || prefsChanged;
    if (!needed) return { needed: false, set: {}, remove: [], notice: null };

    if (draftCount > 0 && !alreadyArchived) {
      set[CONTACT_STORAGE.LEGACY_ARCHIVE] = {
        archivedAt: opts.migratedAt || null,
        reason:
          "campaign-era drafts cannot be resubmitted under the contact-first " +
          "contract; archived verbatim and exportable, never resent",
        contracts: ["salesnav-capture/1.0.0", "linkedin-profile-capture/1.0.0"],
        drafts: archivedDrafts,
      };
    }
    if (prefsChanged && nextPrefs) set[STORAGE.PREFERENCES] = nextPrefs;

    const notice = {
      at: opts.migratedAt || null,
      archivedDraftCount: alreadyArchived ? 0 : draftCount,
      clearedKeys: remove.slice(),
      campaignSelectionRemoved: prefsChanged,
      message: buildMessage(alreadyArchived ? 0 : draftCount, prefsChanged),
    };
    set[CONTACT_STORAGE.MIGRATION_NOTICE] = notice;
    return { needed: true, set, remove, notice };
  }

  function buildMessage(draftCount, prefsChanged) {
    const parts = [];
    if (draftCount > 0) {
      parts.push(
        `${draftCount} draft${draftCount === 1 ? "" : "s"} from the campaign-era workflow ` +
          "were archived, not sent. Download them if you still need them, then capture again."
      );
    }
    if (prefsChanged) {
      parts.push("Campaign selection has been removed — captures no longer belong to a campaign.");
    }
    if (!parts.length) parts.push("Local state was updated for the contact-first workflow.");
    return parts.join(" ");
  }

  /** Every storage key `planMigration` needs to inspect. */
  function keysToRead() {
    return LEGACY_DRAFT_KEYS.concat(LEGACY_RESULT_KEYS, [
      STORAGE.PREFERENCES,
      CONTACT_STORAGE.LEGACY_ARCHIVE,
      CONTACT_STORAGE.MIGRATION_NOTICE,
    ]);
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
  };
});
