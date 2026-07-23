/**
 * In-batch (temporary, client-side) deduplication.
 *
 * Rules (from the task spec):
 *  - Deduplicate inside the draft batch by a stable Sales Navigator / LinkedIn
 *    URL where available.
 *  - When identity is UNCERTAIN (no stable URL), keep the record and flag it as
 *    an uncertain-identity duplicate rather than silently dropping it.
 *
 * This is NOT database deduplication (that stays in the VMR backend). It only
 * keeps the operator's temporary review batch clean.
 *
 * UMD module -> Node CommonJS + self.SNCapture.dedupe
 */
(function (root, factory) {
  const mod = factory(
    typeof module !== "undefined" && module.exports
      ? require("./constants.js")
      : (typeof self !== "undefined" ? self : root).SNCapture.constants
  );
  if (typeof module !== "undefined" && module.exports) module.exports = mod;
  const g = typeof self !== "undefined" ? self : root;
  g.SNCapture = Object.assign(g.SNCapture || {}, { dedupe: mod });
})(typeof globalThis !== "undefined" ? globalThis : this, function (constants) {
  "use strict";
  const { WARNINGS } = constants;

  function hasWarning(record, code) {
    return (record.warnings || []).some((w) => w.code === code);
  }
  function addWarning(record, warning) {
    record.warnings = record.warnings || [];
    if (!record.warnings.some((w) => w.code === warning.code && w.field === warning.field)) {
      record.warnings.push(warning);
    }
  }

  /**
   * Merge `incoming` records into `existing` batch records.
   * Returns { records, added, collapsed, uncertain }.
   *   - added: count of new distinct records appended
   *   - collapsed: count of incoming records dropped as exact-URL duplicates
   *   - uncertain: count of incoming records kept but flagged uncertain-identity
   */
  function mergeBatch(existing, incoming) {
    const records = existing ? existing.slice() : [];
    const keyIndex = new Map();
    records.forEach((r, i) => {
      if (r._stableKey) keyIndex.set(r._stableKey, i);
    });

    let added = 0;
    let collapsed = 0;
    let uncertain = 0;

    for (const rec of incoming) {
      const key = rec._stableKey;
      if (key) {
        if (keyIndex.has(key)) {
          // Exact duplicate by stable identity: collapse. Record the fact on the
          // kept record so the operator sees it was seen more than once.
          const keptIdx = keyIndex.get(key);
          const kept = records[keptIdx];
          kept._duplicateHits = (kept._duplicateHits || 1) + 1;
          addWarning(kept, {
            code: WARNINGS.DUPLICATE_COLLAPSED,
            field: "stableKey",
            detail: `seen ${kept._duplicateHits}x (page ${rec.sourcePageNumber ?? "?"}, pos ${rec.sourcePosition ?? "?"})`,
          });
          collapsed += 1;
          continue;
        }
        keyIndex.set(key, records.length);
        records.push(rec);
        added += 1;
      } else {
        // No stable identity -> keep, but flag as uncertain duplicate.
        if (!hasWarning(rec, WARNINGS.DUPLICATE_UNCERTAIN)) {
          addWarning(rec, { code: WARNINGS.DUPLICATE_UNCERTAIN, field: "stableKey" });
        }
        records.push(rec);
        uncertain += 1;
        added += 1;
      }
    }

    return { records, added, collapsed, uncertain };
  }

  /** Summary counts over a batch for the UI. */
  function summarize(records) {
    const list = records || [];
    let withMissing = 0;
    let selectorFailures = 0;
    let uncertain = 0;
    let excluded = 0;
    for (const r of list) {
      const w = r.warnings || [];
      if (r._excluded) excluded += 1;
      if (w.some((x) => x.code === WARNINGS.MISSING_FIELD)) withMissing += 1;
      if (w.some((x) => x.code === WARNINGS.SELECTOR_FAILURE)) selectorFailures += 1;
      if (w.some((x) => x.code === WARNINGS.DUPLICATE_UNCERTAIN)) uncertain += 1;
    }
    return {
      total: list.length,
      included: list.length - excluded,
      excluded,
      withMissingFields: withMissing,
      selectorFailures,
      uncertainIdentity: uncertain,
    };
  }

  return { mergeBatch, summarize, _internals: { addWarning, hasWarning } };
});
