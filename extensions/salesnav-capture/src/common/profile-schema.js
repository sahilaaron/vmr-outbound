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
 * The validators below systematically enforce the committed JSON Schemas
 * (required keys, `additionalProperties: false`, consts, enums, length and
 * range bounds, array item types) so the extension never sends a payload the
 * backend contract would reject. Kept dependency-free on purpose (the
 * extension ships zero runtime dependencies); test/schema-parity.test.js
 * proves agreement with the committed schema files, so the two definitions
 * cannot drift silently. URL identity fields are validated by PARSING (URL),
 * never by substring matching — consistent with src/common/surface.js.
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
    SURFACES,
    LIMITS,
  } = constants;

  const MAX_EXPERIENCES = 100;
  const CAPTURE_ID_MIN_LENGTH = 8;
  const CAPTURE_ID_MAX_LENGTH = 128;
  const EXTRACTION_STATUSES = ["ok", "partial"];

  function newCaptureId() {
    if (typeof crypto !== "undefined" && crypto.randomUUID) return crypto.randomUUID();
    throw new Error("crypto.randomUUID unavailable; cannot mint client_capture_id");
  }

  // ---- Field projections ---------------------------------------------------

  const ENVELOPE_FIELDS = [
    "schema_version",
    "client_capture_id",
    "campaign_id",
    "captured_at",
    "source",
    "source_url",
    "extraction",
  ];

  const EXTRACTION_FIELDS = [
    "adapter_version",
    "extension_version",
    "status",
    "surface",
    "missing_sections",
    "excluded_sections",
    "page_warnings",
  ];

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

  // ---- Identity URL validation (parsed, never substring) -------------------
  //
  // Same rules as surface.js route detection, plus the wire contract's HTTPS
  // requirement. A hostname must be exactly linkedin.com or a subdomain of it,
  // so deceptive hosts like `linkedin.com.evil.example`,
  // `example.com/linkedin.com/in/person`, or `evil.example/company/linkedin.com`
  // can never pass.

  function parseHttpsLinkedInUrl(value) {
    if (typeof value !== "string") return null;
    let u;
    try {
      u = new URL(value);
    } catch (_e) {
      return null;
    }
    if (u.protocol !== "https:") return null;
    const host = u.hostname.toLowerCase();
    if (host !== "linkedin.com" && !host.endsWith(".linkedin.com")) return null;
    if (u.search || u.hash || u.username || u.password) return null;
    return u;
  }

  /** MAIN profile page only: https + linkedin host + /in/<id>[/]. */
  function isValidProfileIdentityUrl(value) {
    const u = parseHttpsLinkedInUrl(value);
    if (!u) return false;
    return /^\/in\/[^/]+\/?$/.test(u.pathname);
  }

  /**
   * Company page only: https + linkedin host + /company/<id>[/about][/].
   * `/school/` pages are NOT a supported surface in the first release
   * (PageSurfaceDetector rejects them; the validator must agree).
   */
  function isValidCompanyIdentityUrl(value) {
    const u = parseHttpsLinkedInUrl(value);
    if (!u) return false;
    return /^\/company\/[^/]+(\/about)?\/?$/.test(u.pathname);
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

  // ---- Systematic validation (dependency-free, mirrors the JSON Schemas) ---

  function isString(v) { return typeof v === "string"; }
  function isNullableString(v) { return v === null || typeof v === "string"; }
  function isNullableInt(v) { return v === null || Number.isInteger(v); }
  function isNullableBool(v) { return v === null || typeof v === "boolean"; }
  function isIsoDate(v) { return isString(v) && !Number.isNaN(Date.parse(v)); }
  function isPlainObject(v) { return v !== null && typeof v === "object" && !Array.isArray(v); }

  /** `additionalProperties: false` — reject undeclared keys wherever declared. */
  function checkNoExtraKeys(obj, allowed, at, errors) {
    const allowedSet = new Set(allowed);
    for (const key of Object.keys(obj)) {
      if (!allowedSet.has(key)) errors.push(`${at}.${key} is not a declared property`);
    }
  }

  function checkStringArray(v, at, errors) {
    if (!Array.isArray(v)) {
      errors.push(`${at} must be an array`);
      return;
    }
    v.forEach((item, i) => {
      if (!isString(item)) errors.push(`${at}[${i}] must be a string`);
    });
  }

  function checkObjectArray(v, at, errors) {
    if (!Array.isArray(v)) {
      errors.push(`${at} must be an array`);
      return;
    }
    v.forEach((item, i) => {
      if (!isPlainObject(item)) errors.push(`${at}[${i}] must be an object`);
    });
  }

  function validateDatePart(v, at, errors) {
    if (v === null) return;
    if (!isPlainObject(v)) {
      errors.push(`${at} must be an object or null`);
      return;
    }
    checkNoExtraKeys(v, ["year", "month"], at, errors);
    for (const key of ["year", "month"]) {
      if (!(key in v)) errors.push(`${at}.${key} is required`);
    }
    if (!Number.isInteger(v.year) || v.year < 1900 || v.year > 2100) {
      errors.push(`${at}.year must be an integer year`);
    }
    if (!(v.month === null || (Number.isInteger(v.month) && v.month >= 1 && v.month <= 12))) {
      errors.push(`${at}.month must be 1-12 or null`);
    }
  }

  function validateEnvelope(payload, expected, errors) {
    const req = (cond, msg) => { if (!cond) errors.push(msg); };
    req(payload.schema_version === expected.version, `schema_version must equal "${expected.version}"`);
    req(
      isString(payload.client_capture_id) &&
        payload.client_capture_id.length >= CAPTURE_ID_MIN_LENGTH &&
        payload.client_capture_id.length <= CAPTURE_ID_MAX_LENGTH,
      `client_capture_id must be a string of ${CAPTURE_ID_MIN_LENGTH}-${CAPTURE_ID_MAX_LENGTH} characters`
    );
    req(payload.campaign_id === null || isString(payload.campaign_id), "campaign_id must be a string or null");
    req(isIsoDate(payload.captured_at), "captured_at must be an ISO-8601 string");
    req(payload.source === expected.source, `source must equal "${expected.source}"`);
    req(isNullableString(payload.source_url), "source_url must be a string or null");

    const extraction = payload.extraction;
    req(isPlainObject(extraction), "extraction must be an object");
    if (isPlainObject(extraction)) {
      checkNoExtraKeys(extraction, EXTRACTION_FIELDS, "extraction", errors);
      for (const key of EXTRACTION_FIELDS) {
        if (!(key in extraction)) errors.push(`extraction.${key} is required`);
      }
      req(isNullableString(extraction.adapter_version), "extraction.adapter_version must be a string or null");
      req(isNullableString(extraction.extension_version), "extraction.extension_version must be a string or null");
      req(
        EXTRACTION_STATUSES.includes(extraction.status),
        `extraction.status must be one of: ${EXTRACTION_STATUSES.join(", ")}`
      );
      req(extraction.surface === expected.surface, `extraction.surface must equal "${expected.surface}"`);
      checkStringArray(extraction.missing_sections, "extraction.missing_sections", errors);
      checkStringArray(extraction.excluded_sections, "extraction.excluded_sections", errors);
      checkObjectArray(extraction.page_warnings, "extraction.page_warnings", errors);
    }
  }

  /** Validate a person-profile payload. Returns { valid, errors }. */
  function validateProfilePayload(payload) {
    const errors = [];
    const req = (cond, msg) => { if (!cond) errors.push(msg); };

    req(isPlainObject(payload), "payload must be an object");
    if (!isPlainObject(payload)) return { valid: false, errors };

    checkNoExtraKeys(payload, [...ENVELOPE_FIELDS, "profile", "experiences"], "payload", errors);
    for (const key of [...ENVELOPE_FIELDS, "profile", "experiences"]) {
      if (!(key in payload)) errors.push(`payload.${key} is required`);
    }
    validateEnvelope(
      payload,
      {
        version: PROFILE_SCHEMA_VERSION,
        source: PROFILE_SOURCE_IDENTIFIER,
        surface: SURFACES.PERSON_PROFILE,
      },
      errors
    );

    const p = payload.profile;
    req(isPlainObject(p), "profile must be an object");
    if (isPlainObject(p)) {
      checkNoExtraKeys(p, PROFILE_FIELDS, "profile", errors);
      for (const key of PROFILE_FIELDS) {
        if (!(key in p)) errors.push(`profile.${key} is required`);
      }
      req(
        isValidProfileIdentityUrl(p.linkedin_profile_url),
        "profile.linkedin_profile_url must be an https linkedin.com MAIN profile URL (/in/<id>)"
      );
      for (const f of ["public_identifier", "full_name", "headline", "displayed_location", "connection_count_raw"]) {
        req(isNullableString(p[f]), `profile.${f} must be a string or null`);
      }
      req(
        isNullableInt(p.connection_count) && (p.connection_count === null || p.connection_count >= 0),
        "profile.connection_count must be a non-negative integer or null"
      );
      req(isNullableBool(p.open_to_work), "profile.open_to_work must be a boolean or null");
      req(isIsoDate(p.observed_at), "profile.observed_at must be an ISO-8601 string");
      checkStringArray(p.raw_lines, "profile.raw_lines", errors);
      checkObjectArray(p.warnings, "profile.warnings", errors);
    }

    req(Array.isArray(payload.experiences), "experiences must be an array");
    if (Array.isArray(payload.experiences)) {
      req(payload.experiences.length <= MAX_EXPERIENCES, `experiences must not exceed ${MAX_EXPERIENCES}`);
      payload.experiences.forEach((e, i) => {
        const at = `experiences[${i}]`;
        req(isPlainObject(e), `${at} must be an object`);
        if (!isPlainObject(e)) return;
        checkNoExtraKeys(e, EXPERIENCE_FIELDS, at, errors);
        for (const key of EXPERIENCE_FIELDS) {
          if (!(key in e)) errors.push(`${at}.${key} is required`);
        }
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
        checkStringArray(e.raw_lines, `${at}.raw_lines`, errors);
        checkObjectArray(e.warnings, `${at}.warnings`, errors);
        req(isIsoDate(e.observed_at), `${at}.observed_at must be an ISO-8601 string`);
      });
    }
    return { valid: errors.length === 0, errors };
  }

  /** Validate a company payload. Returns { valid, errors }. */
  function validateCompanyPayload(payload) {
    const errors = [];
    const req = (cond, msg) => { if (!cond) errors.push(msg); };

    req(isPlainObject(payload), "payload must be an object");
    if (!isPlainObject(payload)) return { valid: false, errors };

    checkNoExtraKeys(payload, [...ENVELOPE_FIELDS, "company"], "payload", errors);
    for (const key of [...ENVELOPE_FIELDS, "company"]) {
      if (!(key in payload)) errors.push(`payload.${key} is required`);
    }
    validateEnvelope(
      payload,
      {
        version: COMPANY_SCHEMA_VERSION,
        source: COMPANY_SOURCE_IDENTIFIER,
        surface: SURFACES.COMPANY_PROFILE,
      },
      errors
    );

    const c = payload.company;
    req(isPlainObject(c), "company must be an object");
    if (isPlainObject(c)) {
      checkNoExtraKeys(c, COMPANY_FIELDS, "company", errors);
      for (const key of COMPANY_FIELDS) {
        if (!(key in c)) errors.push(`company.${key} is required`);
      }
      req(
        isValidCompanyIdentityUrl(c.company_linkedin_url),
        "company.company_linkedin_url must be an https linkedin.com company URL (/company/<id>[/about]); school pages are not supported"
      );
      for (const f of ["company_linkedin_id", "name", "website", "industry", "size_range",
        "employee_count_raw", "headquarters_text", "founded_raw", "specialties"]) {
        req(isNullableString(c[f]), `company.${f} must be a string or null`);
      }
      req(
        isNullableInt(c.employee_count) && (c.employee_count === null || c.employee_count >= 0),
        "company.employee_count must be a non-negative integer or null"
      );
      req(
        c.founded_year === null ||
          (Number.isInteger(c.founded_year) && c.founded_year >= 1000 && c.founded_year <= 2100),
        "company.founded_year must be a plausible integer year or null"
      );
      req(isIsoDate(c.observed_at), "company.observed_at must be an ISO-8601 string");
      checkStringArray(c.raw_lines, "company.raw_lines", errors);
      checkObjectArray(c.warnings, "company.warnings", errors);
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
    isValidProfileIdentityUrl,
    isValidCompanyIdentityUrl,
    MAX_EXPERIENCES,
    CAPTURE_ID_MAX_LENGTH,
    PROFILE_FIELDS,
    EXPERIENCE_FIELDS,
    COMPANY_FIELDS,
  };
});
