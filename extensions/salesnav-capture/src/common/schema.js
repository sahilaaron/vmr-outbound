/**
 * Payload construction, validation, and export serialization for the backend
 * handoff contract. Mirrors docs/BACKEND_CONTRACT.md and docs/intake.schema.json.
 *
 * The extension STAGES data only. This module builds the request body; it never
 * creates contacts, normalizes authoritatively, or verifies anything.
 *
 * UMD module -> Node CommonJS + self.SNCapture.schema
 */
(function (root, factory) {
  const g = typeof self !== "undefined" ? self : root;
  const isNode = typeof module !== "undefined" && module.exports;
  const mod = factory(
    isNode ? require("./constants.js") : g.SNCapture.constants,
    isNode ? require("./normalize.js") : g.SNCapture.normalize
  );
  if (isNode) module.exports = mod;
  g.SNCapture = Object.assign(g.SNCapture || {}, { schema: mod });
})(typeof globalThis !== "undefined" ? globalThis : this, function (constants, normalize) {
  "use strict";
  const { SCHEMA_VERSION, SOURCE_IDENTIFIER, LIMITS } = constants;

  // Fields that make up a raw record in the outgoing payload (internal `_`
  // fields are stripped).
  const RECORD_FIELDS = [
    "firstName",
    "lastName",
    "rawFullName",
    "title",
    "companyName",
    "location",
    "linkedinProfileUrl",
    "salesNavLeadUrl",
    "companyLinkedInUrl",
    "salesNavCompanyUrl",
    "visibleCompanyMetadata",
    "sourceSearchUrl",
    "sourcePageNumber",
    "sourcePosition",
    "capturedAt",
    "warnings",
  ];

  function newBatchId() {
    if (typeof crypto !== "undefined" && crypto.randomUUID) return crypto.randomUUID();
    // Fallback (should not be reached in Chrome/Node 22): time-free is not
    // possible here, so require crypto. Throw rather than emit a weak id.
    throw new Error("crypto.randomUUID unavailable; cannot mint client_batch_id");
  }

  /** Project an internal record to the wire shape (drops `_`-prefixed fields). */
  function toWireRecord(rec) {
    const out = {};
    for (const f of RECORD_FIELDS) {
      out[f] = rec[f] === undefined ? null : rec[f];
    }
    return out;
  }

  /**
   * Build the intake payload.
   * @param {object} args
   *   records: internal record objects (already filtered to included ones)
   *   clientBatchId: stable per-batch id (idempotency key)
   *   campaignId: operator-selected campaign id (may be null in dev)
   *   capturedAt: ISO timestamp of batch creation
   *   currentSearchUrl: last Sales Navigator search URL seen
   *   extractionMeta: { extensionVersion, pagesCaptured, statuses, warningsSummary }
   */
  function buildPayload(args) {
    const records = (args.records || []).map(toWireRecord);
    return {
      schema_version: SCHEMA_VERSION,
      client_batch_id: args.clientBatchId,
      campaign_id: args.campaignId != null ? args.campaignId : null,
      captured_at: args.capturedAt,
      source: SOURCE_IDENTIFIER,
      current_search_url: args.currentSearchUrl != null ? args.currentSearchUrl : null,
      extraction_metadata: Object.assign(
        {
          extension_version: null,
          pages_captured: null,
          record_count: records.length,
          capture_statuses: [],
          warnings_summary: {},
        },
        args.extractionMeta || {}
      ),
      records,
    };
  }

  // ---- Lightweight structural validation (no external deps) ---------------

  function isString(v) { return typeof v === "string"; }
  function isNullableString(v) { return v === null || typeof v === "string"; }

  /**
   * Validate a payload against the contract. Returns { valid, errors:[...] }.
   * Kept dependency-free and in sync with docs/intake.schema.json.
   */
  function validatePayload(payload) {
    const errors = [];
    const req = (cond, msg) => { if (!cond) errors.push(msg); };

    req(payload && typeof payload === "object", "payload must be an object");
    if (!payload || typeof payload !== "object") return { valid: false, errors };

    req(payload.schema_version === SCHEMA_VERSION, `schema_version must equal "${SCHEMA_VERSION}"`);
    req(isString(payload.client_batch_id) && payload.client_batch_id.length > 0, "client_batch_id must be a non-empty string");
    req(payload.campaign_id === null || isString(payload.campaign_id), "campaign_id must be a string or null");
    req(isString(payload.captured_at) && !Number.isNaN(Date.parse(payload.captured_at)), "captured_at must be an ISO-8601 string");
    req(isString(payload.source), "source must be a string");
    req(isNullableString(payload.current_search_url), "current_search_url must be a string or null");
    req(payload.extraction_metadata && typeof payload.extraction_metadata === "object", "extraction_metadata must be an object");
    req(Array.isArray(payload.records), "records must be an array");

    if (Array.isArray(payload.records)) {
      req(payload.records.length > 0, "records must not be empty");
      // The legacy campaign-era wire ceiling, which is the per-REQUEST one. It
      // never followed the operator's reviewed-set ceiling and must not start
      // now that the two are different numbers.
      req(payload.records.length <= LIMITS.MAX_CONTACTS_PER_SUBMISSION, `records must not exceed ${LIMITS.MAX_CONTACTS_PER_SUBMISSION}`);
      payload.records.forEach((r, i) => {
        const at = `records[${i}]`;
        req(r && typeof r === "object", `${at} must be an object`);
        if (!r || typeof r !== "object") return;
        // Every human-visible string field is nullable but must be string|null.
        for (const f of ["firstName", "lastName", "rawFullName", "title", "companyName", "location", "linkedinProfileUrl", "salesNavLeadUrl", "companyLinkedInUrl", "salesNavCompanyUrl", "sourceSearchUrl", "capturedAt"]) {
          req(isNullableString(r[f]), `${at}.${f} must be a string or null`);
        }
        req(r.visibleCompanyMetadata === null || Array.isArray(r.visibleCompanyMetadata), `${at}.visibleCompanyMetadata must be an array or null`);
        req(r.sourcePageNumber === null || Number.isInteger(r.sourcePageNumber), `${at}.sourcePageNumber must be an integer or null`);
        req(r.sourcePosition === null || Number.isInteger(r.sourcePosition), `${at}.sourcePosition must be an integer or null`);
        req(Array.isArray(r.warnings), `${at}.warnings must be an array`);
        // A record with no stable identity at all is allowed but must carry a URL
        // field or a warning explaining why (defense against silent empties).
        const hasAnyIdentity = r.linkedinProfileUrl || r.salesNavLeadUrl || r.rawFullName;
        req(!!hasAnyIdentity, `${at} has no name or URL (empty record not allowed)`);
      });
    }
    return { valid: errors.length === 0, errors };
  }

  /** Serialize and size-check a payload. Returns { json, bytes, withinLimit }. */
  function serializePayload(payload) {
    const json = JSON.stringify(payload);
    const bytes = byteLength(json);
    return { json, bytes, withinLimit: bytes <= LIMITS.MAX_PAYLOAD_BYTES };
  }

  function byteLength(str) {
    if (typeof TextEncoder !== "undefined") return new TextEncoder().encode(str).length;
    return Buffer.byteLength(str, "utf8");
  }

  // ---- CSV export ---------------------------------------------------------
  //
  // RESTORED, and deliberately the SAME format it was before it was removed.
  // The first sixteen columns are byte-for-byte the historical contract — same
  // header names, same order — so a spreadsheet, script or saved import mapping
  // written against the old export still reads a file produced today.
  //
  // Two columns are APPENDED at the end, and appended is the operative word: an
  // existing reader keyed on the old headers is unaffected, while an operator
  // exporting today does not silently lose two identifiers the extension
  // genuinely observed. They arrived with the Sales Navigator identity work
  // after the export was removed:
  //
  //   linkedin_member_id   the opaque member identifier, verbatim. NOT a URL and
  //                        NOT the public handle.
  //   linkedin_alias_url   LinkedIn's resolving alias for that member id.
  //                        Navigation and evidence only; never the handle.
  //
  // Nothing here is inferred, enriched or repaired. A column is empty when the
  // page did not show the value, which is the same rule the capture itself
  // follows — an export that quietly filled a gap would be a worse record of
  // what was captured than the capture is.
  const CSV_COLUMNS = [
    ["rawFullName", "raw_full_name"],
    ["firstName", "first_name"],
    ["lastName", "last_name"],
    ["title", "title"],
    ["companyName", "company_name"],
    ["location", "location"],
    ["linkedinProfileUrl", "linkedin_profile_url"],
    ["salesNavLeadUrl", "sales_nav_lead_url"],
    ["companyLinkedInUrl", "company_linkedin_url"],
    ["salesNavCompanyUrl", "sales_nav_company_url"],
    ["visibleCompanyMetadata", "visible_company_metadata"],
    ["sourceSearchUrl", "source_search_url"],
    ["sourcePageNumber", "source_page_number"],
    ["sourcePosition", "source_position"],
    ["capturedAt", "captured_at"],
    ["warnings", "warnings"],
    ["linkedinMemberId", "linkedin_member_id"],
    ["linkedinAliasUrl", "linkedin_alias_url"],
  ];

  function csvCell(value) {
    let s;
    if (value == null) s = "";
    else if (Array.isArray(value)) {
      s = value
        .map((v) => (v && typeof v === "object" ? JSON.stringify(v) : String(v)))
        .join(" | ");
    } else if (typeof value === "object") s = JSON.stringify(value);
    else s = String(value);
    // RFC-4180 quoting; also neutralize spreadsheet formula injection.
    if (/^[=+\-@\t\r]/.test(s)) s = "'" + s;
    if (/[",\n\r]/.test(s)) s = '"' + s.replace(/"/g, '""') + '"';
    return s;
  }

  function toCsv(records) {
    const header = CSV_COLUMNS.map(([, h]) => h).join(",");
    const rows = (records || []).map((r) =>
      CSV_COLUMNS.map(([k]) => csvCell(r[k])).join(",")
    );
    return [header, ...rows].join("\r\n");
  }

  return {
    newBatchId,
    toWireRecord,
    buildPayload,
    validatePayload,
    serializePayload,
    byteLength,
    toCsv,
    csvCell,
    RECORD_FIELDS,
    CSV_COLUMNS,
  };
});
