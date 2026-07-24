/**
 * Payload construction + validation for the operator-opened LinkedIn capture
 * contracts (DAT-012A):
 *
 *   linkedin-profile-capture/1.0.0  -> docs/profile-intake.schema.json
 *   linkedin-company-capture/1.0.0  -> docs/company-intake.schema.json
 *
 * Mirrors docs/PROFILE_CONTRACT.md. The extension STAGES observations only:
 * this module never matches identities, refreshes contacts, or mutates
 * anything — those are backend responsibilities.
 *
 * UMD module -> Node CommonJS + self.SNCapture.profileSchema
 */
(function (root, factory) {
  const g = typeof self !== "undefined" ? self : root;
  const isNode = typeof module !== "undefined" && module.exports;
  const mod = factory(isNode ? require("./constants.js") : g.SNCapture.constants);
  if (isNode) module.exports = mod;
  g.SNCapture = Object.assign(g.SNCapture || {}, { profileSchema: mod });
})(typeof globalThis !== "undefined" ? globalThis : this, function (constants) {
  "use strict";

  const {
    PROFILE_SCHEMA_VERSION,
    COMPANY_SCHEMA_VERSION,
    PROFILE_SOURCE_IDENTIFIER,
    COMPANY_SOURCE_IDENTIFIER,
    LIMITS,
  } = constants;

  const MAX_EXPERIENCES = 100;

  function newCaptureId() {
    if (typeof crypto !== "undefined" && crypto.randomUUID) return crypto.randomUUID();
    throw new Error("crypto.randomUUID unavailable; cannot mint client_capture_id");
  }

  // ---- Field projections ---------------------------------------------------

  const PROFILE_FIELDS = [
    "linkedin_profile_url",
    "public_identifier",
    "full_name",
    "headline",
    "displayed_location",
    "connection_count",
    "connection_count_raw",
    "open_to_work",
    "observed_at",
    "raw_lines",
    "warnings",
  ];

  const EXPERIENCE_FIELDS = [
    "position_index",
    "layout",
    "company_name",
    "company_linkedin_url",
    "company_linkedin_id",
    "job_title",
    "timeline_text",
    "start_date",
    "end_date",
    "dates_reliable",
    "duration_text",
    "employment_type",
    "role_location",
    "workplace_type",
    "is_current",
    "raw_lines",
    "warnings",
    "observed_at",
  ];

  const COMPANY_FIELDS = [
    "company_linkedin_url",
    "company_linkedin_id",
    "name",
    "website",
    "industry",
    "size_range",
    "employee_count_raw",
    "employee_count",
    "headquarters_text",
    "founded_year",
    "founded_raw",
    "specialties",
    "observed_at",
    "raw_lines",
    "warnings",
  ];

  function project(obj, fields) {
    const out = {};
    for (const f of fields) out[f] = obj && obj[f] !== undefined ? obj[f] : null;
    return out;
  }

  // ---- Payload builders ----------------------------------------------------

  /**
   * Build the person-profile intake payload from an adapter result.
   * @param {object} args
   *   extraction: result of profileExtraction.extractProfile (status ok|partial)
   *   clientCaptureId: stable idempotency key for this reviewed draft
   *   campaignId: operator-selected campaign id or null
   *   extensionVersion: manifest version string
   *   excludedSections: operator-excluded optional sections ([] by default)
   */
  function buildProfilePayload(args) {
    const ex = args.extraction;
    const excluded = args.excludedSections || [];
    const experiences = excluded.includes("experience")
      ? []
      : (ex.experiences || []).map((e) => project(e, EXPERIENCE_FIELDS));
    return {
      schema_version: PROFILE_SCHEMA_VERSION,
      client_capture_id: args.clientCaptureId,
      campaign_id: args.campaignId != null && args.campaignId !== "" ? args.campaignId : null,
      captured_at: ex.capturedAt,
      source: PROFILE_SOURCE_IDENTIFIER,
      source_url: ex.sourceUrl != null ? ex.sourceUrl : null,
      extraction: {
        adapter_version: ex.adapterVersion || null,
        extension_version: args.extensionVersion || null,
        status: ex.status,
        surface: ex.surface,
        missing_sections: ex.missingSections || [],
        excluded_sections: excluded,
        page_warnings: ex.pageWarnings || [],
      },
      profile: project(ex.profile, PROFILE_FIELDS),
      experiences,
    };
  }

  /** Build the company intake payload from a company-adapter result. */
  function buildCompanyPayload(args) {
    const ex = args.extraction;
    return {
      schema_version: COMPANY_SCHEMA_VERSION,
      client_capture_id: args.clientCaptureId,
      campaign_id: args.campaignId != null && args.campaignId !== "" ? args.campaignId : null,
      captured_at: ex.capturedAt,
      source: COMPANY_SOURCE_IDENTIFIER,
      source_url: ex.sourceUrl != null ? ex.sourceUrl : null,
      extraction: {
        adapter_version: ex.adapterVersion || null,
        extension_version: args.extensionVersion || null,
        status: ex.status,
        surface: ex.surface,
        missing_sections: ex.missingSections || [],
        excluded_sections: args.excludedSections || [],
        page_warnings: ex.pageWarnings || [],
      },
      company: project(ex.company, COMPANY_FIELDS),
    };
  }

  // ---- Validation (dependency-free, mirrors the JSON Schemas) --------------

  function isString(v) { return typeof v === "string"; }
  function isNullableString(v) { return v === null || typeof v === "string"; }
  function isNullableInt(v) { return v === null || Number.isInteger(v); }
  function isNullableBool(v) { return v === null || typeof v === "boolean"; }
  function isIsoDate(v) { return isString(v) && !Number.isNaN(Date.parse(v)); }

  function validateDatePart(v, at, errors) {
    if (v === null) return;
    if (!v || typeof v !== "object") {
      errors.push(`${at} must be an object or null`);
      return;
    }
    if (!Number.isInteger(v.year) || v.year < 1900 || v.year > 2100) {
      errors.push(`${at}.year must be an integer year`);
    }
    if (!(v.month === null || (Number.isInteger(v.month) && v.month >= 1 && v.month <= 12))) {
      errors.push(`${at}.month must be 1-12 or null`);
    }
  }

  function validateEnvelope(payload, expectedVersion, expectedSource, errors) {
    const req = (cond, msg) => { if (!cond) errors.push(msg); };
    req(payload.schema_version === expectedVersion, `schema_version must equal "${expectedVersion}"`);
    req(isString(payload.client_capture_id) && payload.client_capture_id.length >= 8,
      "client_capture_id must be a string of at least 8 characters");
    req(payload.campaign_id === null || isString(payload.campaign_id), "campaign_id must be a string or null");
    req(isIsoDate(payload.captured_at), "captured_at must be an ISO-8601 string");
    req(payload.source === expectedSource, `source must equal "${expectedSource}"`);
    req(isNullableString(payload.source_url), "source_url must be a string or null");
    req(payload.extraction && typeof payload.extraction === "object", "extraction must be an object");
    if (payload.extraction && typeof payload.extraction === "object") {
      req(isString(payload.extraction.status), "extraction.status must be a string");
      req(Array.isArray(payload.extraction.missing_sections), "extraction.missing_sections must be an array");
      req(Array.isArray(payload.extraction.page_warnings), "extraction.page_warnings must be an array");
    }
  }

  /** Validate a person-profile payload. Returns { valid, errors }. */
  function validateProfilePayload(payload) {
    const errors = [];
    const req = (cond, msg) => { if (!cond) errors.push(msg); };

    req(payload && typeof payload === "object", "payload must be an object");
    if (!payload || typeof payload !== "object") return { valid: false, errors };

    validateEnvelope(payload, PROFILE_SCHEMA_VERSION, PROFILE_SOURCE_IDENTIFIER, errors);

    req(payload.profile && typeof payload.profile === "object", "profile must be an object");
    if (payload.profile && typeof payload.profile === "object") {
      const p = payload.profile;
      req(isString(p.linkedin_profile_url) && /linkedin\.com\/in\//.test(p.linkedin_profile_url),
        "profile.linkedin_profile_url must be a normalized linkedin.com/in/ URL");
      for (const f of ["public_identifier", "full_name", "headline", "displayed_location", "connection_count_raw"]) {
        req(isNullableString(p[f]), `profile.${f} must be a string or null`);
      }
      req(isNullableInt(p.connection_count) && (p.connection_count === null || p.connection_count >= 0),
        "profile.connection_count must be a non-negative integer or null");
      req(isNullableBool(p.open_to_work), "profile.open_to_work must be a boolean or null");
      req(isIsoDate(p.observed_at), "profile.observed_at must be an ISO-8601 string");
      req(Array.isArray(p.raw_lines), "profile.raw_lines must be an array");
      req(Array.isArray(p.warnings), "profile.warnings must be an array");
    }

    req(Array.isArray(payload.experiences), "experiences must be an array");
    if (Array.isArray(payload.experiences)) {
      req(payload.experiences.length <= MAX_EXPERIENCES, `experiences must not exceed ${MAX_EXPERIENCES}`);
      payload.experiences.forEach((e, i) => {
        const at = `experiences[${i}]`;
        req(e && typeof e === "object", `${at} must be an object`);
        if (!e || typeof e !== "object") return;
        req(Number.isInteger(e.position_index) && e.position_index >= 1, `${at}.position_index must be a positive integer`);
        req(e.layout === "basic" || e.layout === "chained", `${at}.layout must be "basic" or "chained"`);
        for (const f of ["company_name", "company_linkedin_url", "company_linkedin_id", "job_title",
          "timeline_text", "duration_text", "employment_type", "role_location", "workplace_type"]) {
          req(isNullableString(e[f]), `${at}.${f} must be a string or null`);
        }
        validateDatePart(e.start_date, `${at}.start_date`, errors);
        validateDatePart(e.end_date, `${at}.end_date`, errors);
        req(typeof e.dates_reliable === "boolean", `${at}.dates_reliable must be a boolean`);
        req(isNullableBool(e.is_current), `${at}.is_current must be a boolean or null`);
        req(Array.isArray(e.raw_lines), `${at}.raw_lines must be an array`);
        req(Array.isArray(e.warnings), `${at}.warnings must be an array`);
        req(isIsoDate(e.observed_at), `${at}.observed_at must be an ISO-8601 string`);
      });
    }
    return { valid: errors.length === 0, errors };
  }

  /** Validate a company payload. Returns { valid, errors }. */
  function validateCompanyPayload(payload) {
    const errors = [];
    const req = (cond, msg) => { if (!cond) errors.push(msg); };

    req(payload && typeof payload === "object", "payload must be an object");
    if (!payload || typeof payload !== "object") return { valid: false, errors };

    validateEnvelope(payload, COMPANY_SCHEMA_VERSION, COMPANY_SOURCE_IDENTIFIER, errors);

    req(payload.company && typeof payload.company === "object", "company must be an object");
    if (payload.company && typeof payload.company === "object") {
      const c = payload.company;
      req(isString(c.company_linkedin_url) && /linkedin\.com\/(company|school)\//.test(c.company_linkedin_url),
        "company.company_linkedin_url must be a normalized linkedin.com/company/ URL");
      for (const f of ["company_linkedin_id", "name", "website", "industry", "size_range",
        "employee_count_raw", "headquarters_text", "founded_raw", "specialties"]) {
        req(isNullableString(c[f]), `company.${f} must be a string or null`);
      }
      req(isNullableInt(c.employee_count) && (c.employee_count === null || c.employee_count >= 0),
        "company.employee_count must be a non-negative integer or null");
      req(c.founded_year === null || (Number.isInteger(c.founded_year) && c.founded_year >= 1000 && c.founded_year <= 2100),
        "company.founded_year must be a plausible integer year or null");
      req(isIsoDate(c.observed_at), "company.observed_at must be an ISO-8601 string");
      req(Array.isArray(c.raw_lines), "company.raw_lines must be an array");
      req(Array.isArray(c.warnings), "company.warnings must be an array");
    }
    return { valid: errors.length === 0, errors };
  }

  /** Serialize and size-check a payload. Returns { json, bytes, withinLimit }. */
  function serializePayload(payload) {
    const json = JSON.stringify(payload);
    const bytes = typeof TextEncoder !== "undefined"
      ? new TextEncoder().encode(json).length
      : Buffer.byteLength(json, "utf8");
    return { json, bytes, withinLimit: bytes <= LIMITS.MAX_PAYLOAD_BYTES };
  }

  return {
    newCaptureId,
    buildProfilePayload,
    buildCompanyPayload,
    validateProfilePayload,
    validateCompanyPayload,
    serializePayload,
    MAX_EXPERIENCES,
    PROFILE_FIELDS,
    EXPERIENCE_FIELDS,
    COMPANY_FIELDS,
  };
});
