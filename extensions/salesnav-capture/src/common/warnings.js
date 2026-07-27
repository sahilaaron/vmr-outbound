/**
 * UI-013 — one classification rule for capture warnings.
 *
 * A capture warning answers one of two different questions, and before this
 * module the panel could not tell them apart:
 *
 *   1. "Something about this record is wrong, missing, or uncertain."
 *      The operator has to look. -> REVIEW_FAULT
 *
 *   2. "Here is where this value came from."
 *      Nothing is wrong. The record is complete and the note exists so a
 *      derivation can never be mistaken for an observation. -> PROVENANCE
 *
 * Treating (2) as (1) is what DAT-011 found: every Sales Navigator row carrying
 * `derived_value` was badged *Needs review*, which flagged essentially the whole
 * batch and destroyed the signal for the rows that genuinely needed attention.
 *
 * Classification lives HERE and nowhere else. The listings panel and the person
 * profile controller each keep their own operator-facing wording — their
 * sentences differ by surface — but neither decides what counts as a fault.
 *
 * Unknown codes classify as REVIEW_FAULT deliberately. A code this module has
 * not been taught about might describe a real problem, so the safe default is
 * the visible state, not the quiet one. Adding a code to PROVENANCE is an
 * explicit act with a reason attached.
 *
 * This module classifies. It never filters: every warning stays in the record
 * and stays renderable. Nothing here removes evidence.
 *
 * UMD module -> Node CommonJS + self.SNCapture.warnings
 */
(function (root, factory) {
  const mod = factory(
    typeof module !== "undefined" && module.exports
      ? require("./constants.js")
      : (typeof self !== "undefined" ? self : root).SNCapture.constants
  );
  if (typeof module !== "undefined" && module.exports) module.exports = mod;
  const g = typeof self !== "undefined" ? self : root;
  g.SNCapture = Object.assign(g.SNCapture || {}, { warnings: mod });
})(typeof globalThis !== "undefined" ? globalThis : this, function (constants) {
  "use strict";
  const { WARNINGS } = constants;

  /** The record has a problem: missing, unreadable, uncertain, or unsupported. */
  const REVIEW_FAULT = "review_fault";
  /** The record is fine; the warning records how a value was produced. */
  const PROVENANCE = "provenance";

  /**
   * Only codes with a documented reason to be informational are listed here.
   * Everything else — declared or not — is a review fault.
   *
   * `derived_value` (common/extraction.js): the profile URL was computed from
   *   the Sales Navigator lead URL. The warning carries `field` and `from`, so
   *   the derivation is inspectable. The value is present and usable; how it was
   *   produced is bookkeeping, not a defect.
   *
   * `duplicate_collapsed` (common/dedupe.js): the row was seen more than once
   *   and the copies were collapsed onto the kept record. Its own emitting
   *   comment says the warning exists "so the operator sees it was seen more
   *   than once" — a fact about the page, not a fault in the person.
   *
   * Note what is NOT here. `duplicate_uncertain_identity` looks adjacent but is
   * the opposite case: the row had no stable identity, so the dedupe could not
   * be performed and the record's identity is genuinely uncertain.
   */
  const PROVENANCE_CODES = new Set([WARNINGS.DERIVED_VALUE, WARNINGS.DUPLICATE_COLLAPSED]);

  /** @returns {"review_fault"|"provenance"} */
  function classify(code) {
    return PROVENANCE_CODES.has(code) ? PROVENANCE : REVIEW_FAULT;
  }

  function isProvenance(code) {
    return classify(code) === PROVENANCE;
  }

  function isReviewFault(code) {
    return classify(code) === REVIEW_FAULT;
  }

  /** Flatten one or more warning lists into a single array, skipping empties. */
  function flatten(lists) {
    const out = [];
    for (const list of lists || []) {
      for (const w of list || []) if (w && w.code) out.push(w);
    }
    return out;
  }

  /**
   * Split warnings by class, preserving order and every entry.
   * @returns {{faults: object[], provenance: object[]}}
   */
  function split(warningList) {
    const faults = [];
    const provenance = [];
    for (const w of warningList || []) {
      if (!w || !w.code) continue;
      (isProvenance(w.code) ? provenance : faults).push(w);
    }
    return { faults, provenance };
  }

  /** True when at least one warning is a genuine review fault. */
  function hasReviewFault(warningList) {
    return (warningList || []).some((w) => w && w.code && isReviewFault(w.code));
  }

  /**
   * True when there is at least one warning and every one is provenance.
   * This is the state UI-013 exists to stop mislabelling.
   */
  function isProvenanceOnly(warningList) {
    const list = (warningList || []).filter((w) => w && w.code);
    return list.length > 0 && !hasReviewFault(list);
  }

  /** Distinct codes, order preserved — what the surfaces render badges from. */
  function codes(warningList) {
    return Array.from(new Set((warningList || []).filter((w) => w && w.code).map((w) => w.code)));
  }

  return {
    REVIEW_FAULT,
    PROVENANCE,
    PROVENANCE_CODES,
    classify,
    isProvenance,
    isReviewFault,
    flatten,
    split,
    hasReviewFault,
    isProvenanceOnly,
    codes,
  };
});
