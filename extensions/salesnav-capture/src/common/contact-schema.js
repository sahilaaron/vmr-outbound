/**
 * Contact-first payload construction and validation (DAT-013).
 *
 *   linkedin-contact-capture/2.0.0 -> docs/contact-capture.schema.json
 *
 * Mirrors docs/CONTACT_CAPTURE_CONTRACT.md. One submission carries one or more
 * people the operator deliberately opened or selected, plus optional labels and
 * an optional note. There is NO campaign in this contract, and this module will
 * refuse to build a payload that contains one.
 *
 * The extension submits observations only: it never matches identities, never
 * decides what becomes canonical, never resolves a label, and never writes to a
 * database. Missing values stay null with a warning — nothing is repaired or
 * invented. The validator systematically enforces the committed JSON Schema
 * (required keys, `additionalProperties: false`, consts, enums, bounds) so the
 * extension cannot send a body the backend contract would reject;
 * test/contact-schema-parity.test.js proves the two definitions agree.
 *
 * UMD module -> Node CommonJS + self.SNCapture.contactSchema
 */
(function (root, factory) {
  const g = typeof self !== "undefined" ? self : root;
  const isNode = typeof module !== "undefined" && module.exports;
  const mod = factory(
    isNode ? require("./constants.js") : g.SNCapture.constants,
    isNode ? require("./normalize.js") : g.SNCapture.normalize
  );
  if (isNode) module.exports = mod;
  g.SNCapture = Object.assign(g.SNCapture || {}, { contactSchema: mod });
})(typeof globalThis !== "undefined" ? globalThis : this, function (constants, normalize) {
  "use strict";

  const {
    CONTACT_CAPTURE_SCHEMA_VERSION,
    CONTACT_CAPTURE_SOURCE_IDENTIFIER,
    CAPTURE_MODES,
    SURFACES,
    LIMITS,
  } = constants;

  const CAPTURE_ID_MIN_LENGTH = 8;
  const CAPTURE_ID_MAX_LENGTH = 128;
  const MAX_CONTACTS = LIMITS.MAX_RECORDS_PER_BATCH;
  const MAX_EXPERIENCES = 100;
  const MAX_PAGE_TITLE = 512;
  const MAX_ABOUT = 8000;
  const EXTRACTION_STATUSES = ["ok", "partial"];
  const SURFACE_VALUES = [SURFACES.PERSON_PROFILE, SURFACES.SALESNAV_PEOPLE_RESULTS];
  const CAPTURE_MODE_VALUES = [
    CAPTURE_MODES.LINKEDIN_PROFILE,
    CAPTURE_MODES.SALESNAV_PEOPLE_SEARCH,
  ];

  function newId() {
    if (typeof crypto !== "undefined" && crypto.randomUUID) return crypto.randomUUID();
    throw new Error("crypto.randomUUID unavailable; cannot mint a capture id");
  }

  // ---- Field lists (must match the committed schema exactly) ---------------

  const ENVELOPE_FIELDS = [
    "schema_version",
    "client_submission_id",
    "capture_mode",
    "submitted_at",
    "source",
    "extension_version",
    "operator_metadata",
    "contacts",
  ];

  const CAPTURE_FIELDS = [
    "client_capture_id",
    "captured_at",
    "source",
    "person",
    "current_employment_hint",
    "experience_observations",
    "extraction",
    "operator_metadata",
    "raw_snapshot",
  ];

  const SOURCE_FIELDS = ["surface", "url", "page_title", "operator_triggered"];

  const PERSON_FIELDS = [
    "linkedin_profile_url",
    "linkedin_public_identifier",
    "salesnav_lead_url",
    "salesnav_member_id",
    "full_name",
    "first_name",
    "last_name",
    "headline",
    "location",
    "connection_count",
    "connection_count_raw",
    "open_to_work_visible",
    "about_text",
    "raw_lines",
    "warnings",
  ];

  const HINT_FIELDS = [
    "company_name",
    "company_linkedin_url",
    "company_linkedin_id",
    "title",
    "role_location",
    "workplace_type",
    "employment_type",
    "tenure_text",
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

  const EXTRACTION_FIELDS = [
    "adapter_version",
    "status",
    "missing_sections",
    "excluded_sections",
    "page_warnings",
  ];

  const METADATA_FIELDS = ["labels", "note"];

  function project(obj, fields) {
    const out = {};
    for (const f of fields) out[f] = obj && obj[f] !== undefined ? obj[f] : null;
    return out;
  }

  // ---- Operator metadata ----------------------------------------------------

  /**
   * Clean a requested label list: trim, collapse whitespace, drop blanks and
   * case-insensitive repeats, bound the count and length. The extension only
   * ever REQUESTS a label by name; the backend owns the canonical registry.
   */
  function sanitizeLabels(raw) {
    if (!Array.isArray(raw)) return [];
    const seen = new Set();
    const out = [];
    for (const item of raw) {
      if (typeof item !== "string") continue;
      const name = item.replace(/\s+/g, " ").trim().slice(0, LIMITS.MAX_LABEL_LENGTH).trim();
      if (!name) continue;
      const key = name.toLowerCase();
      if (seen.has(key)) continue;
      seen.add(key);
      out.push(name);
      if (out.length >= LIMITS.MAX_LABELS) break;
    }
    return out;
  }

  /** Clean an operator note: trim and bound. Empty becomes null, never "". */
  function sanitizeNote(raw) {
    if (typeof raw !== "string") return null;
    const text = raw.trim().slice(0, LIMITS.MAX_NOTE_LENGTH).trim();
    return text || null;
  }

  function operatorMetadata(meta) {
    const m = meta || {};
    return { labels: sanitizeLabels(m.labels), note: sanitizeNote(m.note) };
  }

  // ---- Capture builders ------------------------------------------------------

  /**
   * Build one capture from a person-profile adapter result
   * (profile-extraction.extractProfile output).
   *
   * @param {object} args
   *   extraction: adapter result (status ok|partial)
   *   clientCaptureId: stable idempotency key for this reviewed capture
   *   excludedSections: operator-excluded optional sections
   *   metadata: per-capture { labels, note }
   *   pageTitle: the opened page's title, when known
   */
  function buildProfileCapture(args) {
    const ex = args.extraction;
    const p = ex.profile || {};
    const excluded = args.excludedSections || [];
    const experiences = excluded.includes("experience")
      ? []
      : (ex.experiences || []).map((e) => project(e, EXPERIENCE_FIELDS));
    const current = (ex.experiences || []).filter((e) => e.is_current === true)[0] || null;
    return {
      client_capture_id: args.clientCaptureId,
      captured_at: ex.capturedAt,
      source: {
        surface: SURFACES.PERSON_PROFILE,
        url: ex.sourceUrl != null ? ex.sourceUrl : null,
        page_title: args.pageTitle ? String(args.pageTitle).slice(0, MAX_PAGE_TITLE) : null,
        operator_triggered: true,
      },
      person: {
        linkedin_profile_url: p.linkedin_profile_url != null ? p.linkedin_profile_url : null,
        linkedin_public_identifier: p.public_identifier != null ? p.public_identifier : null,
        salesnav_lead_url: null,
        // A person-profile capture never comes from Sales Navigator.
        salesnav_member_id: null,
        full_name: p.full_name != null ? p.full_name : null,
        first_name: normalize.splitName(p.full_name).firstName,
        last_name: normalize.splitName(p.full_name).lastName,
        headline: p.headline != null ? p.headline : null,
        location: p.displayed_location != null ? p.displayed_location : null,
        connection_count: p.connection_count != null ? p.connection_count : null,
        connection_count_raw: p.connection_count_raw != null ? p.connection_count_raw : null,
        open_to_work_visible: p.open_to_work != null ? p.open_to_work : null,
        about_text: p.about_text != null ? String(p.about_text).slice(0, MAX_ABOUT) : null,
        raw_lines: p.raw_lines || [],
        warnings: p.warnings || [],
      },
      current_employment_hint: {
        company_name: current ? current.company_name : null,
        company_linkedin_url: current ? current.company_linkedin_url : null,
        company_linkedin_id: current ? current.company_linkedin_id : null,
        title: current ? current.job_title : null,
        role_location: current ? current.role_location : null,
        workplace_type: current ? current.workplace_type : null,
        employment_type: current ? current.employment_type : null,
        tenure_text: current ? current.duration_text : null,
      },
      experience_observations: experiences,
      extraction: {
        adapter_version: ex.adapterVersion || null,
        status: ex.status,
        missing_sections: ex.missingSections || [],
        excluded_sections: excluded,
        page_warnings: ex.pageWarnings || [],
      },
      operator_metadata: operatorMetadata(args.metadata),
      raw_snapshot: args.rawSnapshot || ex,
    };
  }

  /**
   * Build one capture from a Sales Navigator result row (an internal record
   * produced by extraction.extractPage). A results row shows a person's current
   * title and company but no employment history, so
   * ``experience_observations`` is empty and the visible role travels as the
   * employment HINT. A row without a `/in/` URL keeps a null identity: the
   * uncertainty is preserved, never repaired from the lead URL.
   */
  function buildResultRowCapture(args) {
    const rec = args.record || {};
    const url = rec.linkedinProfileUrl || null;
    const isProfileUrl = url ? /^https:\/\/[^/]*linkedin\.com\/in\/[^/]+\/?$/.test(url) : false;
    return {
      client_capture_id: args.clientCaptureId,
      captured_at: rec.capturedAt || args.capturedAt || null,
      source: {
        surface: SURFACES.SALESNAV_PEOPLE_RESULTS,
        url: rec.sourceSearchUrl || args.sourceSearchUrl || null,
        page_title: args.pageTitle ? String(args.pageTitle).slice(0, MAX_PAGE_TITLE) : null,
        operator_triggered: true,
      },
      person: {
        linkedin_profile_url: isProfileUrl ? url : null,
        linkedin_public_identifier: isProfileUrl ? publicIdentifier(url) : null,
        salesnav_lead_url: rec.salesNavLeadUrl || null,
        // DAT-019: the opaque Sales Navigator member identifier, verbatim and
        // case-preserved. A declared identifier of its own — never a URL, never
        // the public handle, and never a substitute for either.
        salesnav_member_id: rec.linkedinMemberId || null,
        full_name: rec.rawFullName || null,
        first_name: rec.firstName || null,
        last_name: rec.lastName || null,
        headline: rec.title || null,
        location: rec.location || null,
        connection_count: null,
        connection_count_raw: null,
        open_to_work_visible: null,
        about_text: null,
        raw_lines: [],
        warnings: rec.warnings || [],
      },
      current_employment_hint: {
        company_name: rec.companyName || null,
        company_linkedin_url: rec.companyLinkedInUrl || null,
        company_linkedin_id: null,
        title: rec.title || null,
        role_location: null,
        workplace_type: null,
        employment_type: null,
        tenure_text: null,
      },
      experience_observations: [],
      extraction: {
        adapter_version: args.adapterVersion || null,
        status: (rec.warnings || []).length ? "partial" : "ok",
        missing_sections: [],
        excluded_sections: [],
        page_warnings: [],
      },
      operator_metadata: operatorMetadata(args.metadata),
      raw_snapshot: toRawSnapshot(rec),
    };
  }

  /** The verbatim row, minus the panel's internal `_`-prefixed review aids. */
  function toRawSnapshot(record) {
    const out = {};
    for (const [key, value] of Object.entries(record || {})) {
      if (key.startsWith("_")) continue;
      out[key] = value;
    }
    return out;
  }

  function publicIdentifier(url) {
    try {
      const decoded = decodeURIComponent(new URL(url).pathname.replace(/^\/in\//, ""));
      return decoded.replace(/\/$/, "") || null;
    } catch (_e) {
      return null;
    }
  }

  /** Assemble the submission envelope around one or more captures. */
  function buildSubmission(args) {
    return {
      schema_version: CONTACT_CAPTURE_SCHEMA_VERSION,
      client_submission_id: args.clientSubmissionId,
      capture_mode: args.captureMode,
      submitted_at: args.submittedAt,
      source: CONTACT_CAPTURE_SOURCE_IDENTIFIER,
      extension_version: args.extensionVersion || null,
      operator_metadata: operatorMetadata(args.metadata),
      contacts: args.contacts || [],
    };
  }

  // ---- Validation ------------------------------------------------------------

  function isString(v) { return typeof v === "string"; }
  function isNullableString(v) { return v === null || typeof v === "string"; }
  function isNullableInt(v) { return v === null || Number.isInteger(v); }
  function isNullableBool(v) { return v === null || typeof v === "boolean"; }
  function isIsoDate(v) { return isString(v) && !Number.isNaN(Date.parse(v)); }
  function isPlainObject(v) { return v !== null && typeof v === "object" && !Array.isArray(v); }

  function checkNoExtraKeys(obj, allowed, at, errors) {
    const allowedSet = new Set(allowed);
    for (const key of Object.keys(obj)) {
      if (!allowedSet.has(key)) errors.push(`${at}.${key} is not a declared property`);
    }
  }

  function checkRequired(obj, required, at, errors) {
    for (const key of required) {
      if (!(key in obj)) errors.push(`${at}.${key} is required`);
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
    checkRequired(v, ["year", "month"], at, errors);
    if (!Number.isInteger(v.year) || v.year < 1900 || v.year > 2100) {
      errors.push(`${at}.year must be an integer year`);
    }
    if (!(v.month === null || (Number.isInteger(v.month) && v.month >= 1 && v.month <= 12))) {
      errors.push(`${at}.month must be 1-12 or null`);
    }
  }

  /** MAIN profile page only: https + a linkedin host + /in/<id>[/]. */
  const PROFILE_URL_RE = /^https:\/\/([a-z0-9-]+\.)*linkedin\.com\/in\/[^/]+\/?$/;

  function validateMetadata(meta, at, errors) {
    if (!isPlainObject(meta)) {
      errors.push(`${at} must be an object`);
      return;
    }
    checkNoExtraKeys(meta, METADATA_FIELDS, at, errors);
    checkRequired(meta, METADATA_FIELDS, at, errors);
    checkStringArray(meta.labels, `${at}.labels`, errors);
    if (Array.isArray(meta.labels)) {
      if (meta.labels.length > LIMITS.MAX_LABELS) {
        errors.push(`${at}.labels must not exceed ${LIMITS.MAX_LABELS}`);
      }
      meta.labels.forEach((label, i) => {
        if (isString(label) && (label.length < 1 || label.length > LIMITS.MAX_LABEL_LENGTH)) {
          errors.push(`${at}.labels[${i}] must be 1-${LIMITS.MAX_LABEL_LENGTH} characters`);
        }
      });
    }
    if (!isNullableString(meta.note)) errors.push(`${at}.note must be a string or null`);
    else if (meta.note !== null && meta.note.length > LIMITS.MAX_NOTE_LENGTH) {
      errors.push(`${at}.note must not exceed ${LIMITS.MAX_NOTE_LENGTH} characters`);
    }
  }

  function validateCapture(capture, index, errors) {
    const at = `contacts[${index}]`;
    if (!isPlainObject(capture)) {
      errors.push(`${at} must be an object`);
      return;
    }
    checkNoExtraKeys(capture, CAPTURE_FIELDS, at, errors);
    checkRequired(capture, CAPTURE_FIELDS, at, errors);

    const id = capture.client_capture_id;
    if (
      !isString(id) ||
      id.length < CAPTURE_ID_MIN_LENGTH ||
      id.length > CAPTURE_ID_MAX_LENGTH
    ) {
      errors.push(
        `${at}.client_capture_id must be ${CAPTURE_ID_MIN_LENGTH}-${CAPTURE_ID_MAX_LENGTH} characters`
      );
    }
    if (!isIsoDate(capture.captured_at)) {
      errors.push(`${at}.captured_at must be an ISO-8601 string`);
    }

    const src = capture.source;
    if (!isPlainObject(src)) errors.push(`${at}.source must be an object`);
    else {
      checkNoExtraKeys(src, SOURCE_FIELDS, `${at}.source`, errors);
      checkRequired(src, SOURCE_FIELDS, `${at}.source`, errors);
      if (!SURFACE_VALUES.includes(src.surface)) {
        errors.push(`${at}.source.surface must be one of: ${SURFACE_VALUES.join(", ")}`);
      }
      if (!isNullableString(src.url)) errors.push(`${at}.source.url must be a string or null`);
      if (!isNullableString(src.page_title)) {
        errors.push(`${at}.source.page_title must be a string or null`);
      } else if (src.page_title !== null && src.page_title.length > MAX_PAGE_TITLE) {
        errors.push(`${at}.source.page_title must not exceed ${MAX_PAGE_TITLE} characters`);
      }
      if (src.operator_triggered !== true) {
        errors.push(`${at}.source.operator_triggered must be true`);
      }
    }

    const p = capture.person;
    if (!isPlainObject(p)) errors.push(`${at}.person must be an object`);
    else {
      checkNoExtraKeys(p, PERSON_FIELDS, `${at}.person`, errors);
      checkRequired(p, PERSON_FIELDS, `${at}.person`, errors);
      if (p.linkedin_profile_url !== null) {
        if (!isString(p.linkedin_profile_url) || !PROFILE_URL_RE.test(p.linkedin_profile_url)) {
          errors.push(
            `${at}.person.linkedin_profile_url must be null or an https linkedin.com ` +
              "MAIN profile URL (/in/<id>)"
          );
        }
      }
      for (const f of [
        "linkedin_public_identifier",
        "salesnav_lead_url",
        "salesnav_member_id",
        "full_name",
        "first_name",
        "last_name",
        "headline",
        "location",
        "connection_count_raw",
        "about_text",
      ]) {
        if (!isNullableString(p[f])) errors.push(`${at}.person.${f} must be a string or null`);
      }
      if (isString(p.about_text) && p.about_text.length > MAX_ABOUT) {
        errors.push(`${at}.person.about_text must not exceed ${MAX_ABOUT} characters`);
      }
      if (!isNullableInt(p.connection_count) || (p.connection_count !== null && p.connection_count < 0)) {
        errors.push(`${at}.person.connection_count must be a non-negative integer or null`);
      }
      if (!isNullableBool(p.open_to_work_visible)) {
        errors.push(`${at}.person.open_to_work_visible must be a boolean or null`);
      }
      checkStringArray(p.raw_lines, `${at}.person.raw_lines`, errors);
      checkObjectArray(p.warnings, `${at}.person.warnings`, errors);
      // A capture with no visible identity at all can never be reviewed.
      if (!p.linkedin_profile_url && !p.salesnav_lead_url && !p.full_name) {
        errors.push(`${at}.person has no profile URL, lead URL, or name`);
      }
    }

    const hint = capture.current_employment_hint;
    if (!isPlainObject(hint)) errors.push(`${at}.current_employment_hint must be an object`);
    else {
      checkNoExtraKeys(hint, HINT_FIELDS, `${at}.current_employment_hint`, errors);
      checkRequired(hint, HINT_FIELDS, `${at}.current_employment_hint`, errors);
      for (const f of HINT_FIELDS) {
        if (!isNullableString(hint[f])) {
          errors.push(`${at}.current_employment_hint.${f} must be a string or null`);
        }
      }
    }

    if (!Array.isArray(capture.experience_observations)) {
      errors.push(`${at}.experience_observations must be an array`);
    } else {
      if (capture.experience_observations.length > MAX_EXPERIENCES) {
        errors.push(`${at}.experience_observations must not exceed ${MAX_EXPERIENCES}`);
      }
      capture.experience_observations.forEach((e, i) => {
        const eAt = `${at}.experience_observations[${i}]`;
        if (!isPlainObject(e)) {
          errors.push(`${eAt} must be an object`);
          return;
        }
        checkNoExtraKeys(e, EXPERIENCE_FIELDS, eAt, errors);
        checkRequired(e, EXPERIENCE_FIELDS, eAt, errors);
        if (!Number.isInteger(e.position_index) || e.position_index < 1) {
          errors.push(`${eAt}.position_index must be a positive integer`);
        }
        if (e.layout !== "basic" && e.layout !== "chained") {
          errors.push(`${eAt}.layout must be "basic" or "chained"`);
        }
        for (const f of [
          "company_name",
          "company_linkedin_url",
          "company_linkedin_id",
          "job_title",
          "timeline_text",
          "duration_text",
          "employment_type",
          "role_location",
          "workplace_type",
        ]) {
          if (!isNullableString(e[f])) errors.push(`${eAt}.${f} must be a string or null`);
        }
        validateDatePart(e.start_date, `${eAt}.start_date`, errors);
        validateDatePart(e.end_date, `${eAt}.end_date`, errors);
        if (typeof e.dates_reliable !== "boolean") {
          errors.push(`${eAt}.dates_reliable must be a boolean`);
        }
        if (!isNullableBool(e.is_current)) errors.push(`${eAt}.is_current must be a boolean or null`);
        checkStringArray(e.raw_lines, `${eAt}.raw_lines`, errors);
        checkObjectArray(e.warnings, `${eAt}.warnings`, errors);
        if (!isIsoDate(e.observed_at)) errors.push(`${eAt}.observed_at must be an ISO-8601 string`);
      });
    }

    const extraction = capture.extraction;
    if (!isPlainObject(extraction)) errors.push(`${at}.extraction must be an object`);
    else {
      checkNoExtraKeys(extraction, EXTRACTION_FIELDS, `${at}.extraction`, errors);
      checkRequired(extraction, EXTRACTION_FIELDS, `${at}.extraction`, errors);
      if (!isNullableString(extraction.adapter_version)) {
        errors.push(`${at}.extraction.adapter_version must be a string or null`);
      }
      if (!EXTRACTION_STATUSES.includes(extraction.status)) {
        errors.push(`${at}.extraction.status must be one of: ${EXTRACTION_STATUSES.join(", ")}`);
      }
      checkStringArray(extraction.missing_sections, `${at}.extraction.missing_sections`, errors);
      checkStringArray(extraction.excluded_sections, `${at}.extraction.excluded_sections`, errors);
      checkObjectArray(extraction.page_warnings, `${at}.extraction.page_warnings`, errors);
    }

    validateMetadata(capture.operator_metadata, `${at}.operator_metadata`, errors);
    if (!isPlainObject(capture.raw_snapshot)) {
      errors.push(`${at}.raw_snapshot must be an object`);
    }
  }

  /** Validate a whole submission. Returns { valid, errors }. */
  function validateSubmission(payload) {
    const errors = [];
    if (!isPlainObject(payload)) return { valid: false, errors: ["payload must be an object"] };

    checkNoExtraKeys(payload, ENVELOPE_FIELDS, "payload", errors);
    checkRequired(payload, ENVELOPE_FIELDS, "payload", errors);

    if (payload.schema_version !== CONTACT_CAPTURE_SCHEMA_VERSION) {
      errors.push(`schema_version must equal "${CONTACT_CAPTURE_SCHEMA_VERSION}"`);
    }
    const sid = payload.client_submission_id;
    if (!isString(sid) || sid.length < CAPTURE_ID_MIN_LENGTH || sid.length > CAPTURE_ID_MAX_LENGTH) {
      errors.push(
        `client_submission_id must be ${CAPTURE_ID_MIN_LENGTH}-${CAPTURE_ID_MAX_LENGTH} characters`
      );
    }
    if (!CAPTURE_MODE_VALUES.includes(payload.capture_mode)) {
      errors.push(`capture_mode must be one of: ${CAPTURE_MODE_VALUES.join(", ")}`);
    }
    if (!isIsoDate(payload.submitted_at)) errors.push("submitted_at must be an ISO-8601 string");
    if (payload.source !== CONTACT_CAPTURE_SOURCE_IDENTIFIER) {
      errors.push(`source must equal "${CONTACT_CAPTURE_SOURCE_IDENTIFIER}"`);
    }
    if (!isNullableString(payload.extension_version)) {
      errors.push("extension_version must be a string or null");
    }
    validateMetadata(payload.operator_metadata, "operator_metadata", errors);

    if (!Array.isArray(payload.contacts)) errors.push("contacts must be an array");
    else {
      if (payload.contacts.length < 1) errors.push("contacts must not be empty");
      if (payload.contacts.length > MAX_CONTACTS) {
        errors.push(`contacts must not exceed ${MAX_CONTACTS}`);
      }
      const seen = new Set();
      payload.contacts.forEach((capture, index) => {
        validateCapture(capture, index, errors);
        const id = capture && capture.client_capture_id;
        if (isString(id)) {
          if (seen.has(id)) {
            errors.push(`contacts[${index}].client_capture_id is repeated in this submission`);
          }
          seen.add(id);
        }
      });
    }
    return { valid: errors.length === 0, errors };
  }

  /** Serialize and size-check a payload. Returns { json, bytes, withinLimit }. */
  function serializePayload(payload) {
    const json = JSON.stringify(payload);
    const bytes =
      typeof TextEncoder !== "undefined"
        ? new TextEncoder().encode(json).length
        : Buffer.byteLength(json, "utf8");
    return { json, bytes, withinLimit: bytes <= LIMITS.MAX_PAYLOAD_BYTES };
  }

  return {
    newId,
    sanitizeLabels,
    sanitizeNote,
    operatorMetadata,
    buildProfileCapture,
    buildResultRowCapture,
    buildSubmission,
    validateSubmission,
    serializePayload,
    toRawSnapshot,
    ENVELOPE_FIELDS,
    CAPTURE_FIELDS,
    PERSON_FIELDS,
    HINT_FIELDS,
    EXPERIENCE_FIELDS,
    EXTRACTION_FIELDS,
    METADATA_FIELDS,
    SOURCE_FIELDS,
    MAX_CONTACTS,
    MAX_EXPERIENCES,
  };
});
