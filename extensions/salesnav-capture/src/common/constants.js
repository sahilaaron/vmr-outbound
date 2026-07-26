/**
 * Shared constants for the VMR contact-capture extension.
 *
 * UMD-style module: works as a CommonJS module in Node (tests) and as a global
 * `self.SNCapture.constants` when loaded as a classic script in the content
 * script, service worker, or side panel. No bundler, no remote code.
 *
 * The directory name (`salesnav-capture`) is historical: the extension began as
 * a Sales Navigator listing capture and is now the contact-acquisition edge of
 * the platform. The path is kept so the committed contract schemas, backend
 * loaders, and issue history stay continuous.
 */
(function (root, factory) {
  const mod = factory();
  if (typeof module !== "undefined" && module.exports) module.exports = mod;
  const g = typeof self !== "undefined" ? self : root;
  g.SNCapture = Object.assign(g.SNCapture || {}, { constants: mod });
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  // ---- Contracts -----------------------------------------------------------
  //
  // The CONTACT-FIRST contract (DAT-013) is what the normal workflow uses. A
  // submission carries one or more reviewed people and has no campaign field at
  // all. See docs/CONTACT_CAPTURE_CONTRACT.md.
  const CONTACT_CAPTURE_SCHEMA_VERSION = "linkedin-contact-capture/2.0.0";
  const CONTACT_CAPTURE_SOURCE_IDENTIFIER = "chrome-extension:linkedin-contact-capture";

  // LEGACY, campaign-era contracts. Retained so previously staged batches and
  // snapshots stay readable and so the backend transition is explicit; the
  // extension no longer produces the first two. Company evidence still uses its
  // own contract because a company page is not a person.
  const SCHEMA_VERSION = "salesnav-capture/1.0.0";
  const PROFILE_SCHEMA_VERSION = "linkedin-profile-capture/1.0.0";
  const COMPANY_SCHEMA_VERSION = "linkedin-company-capture/1.0.0";

  const SOURCE_IDENTIFIER = "chrome-extension:salesnav-capture";
  const PROFILE_SOURCE_IDENTIFIER = "chrome-extension:linkedin-profile-capture";
  const COMPANY_SOURCE_IDENTIFIER = "chrome-extension:linkedin-company-capture";

  // Which reviewed workflow produced a submission.
  const CAPTURE_MODES = {
    LINKEDIN_PROFILE: "linkedin_profile",
    SALESNAV_PEOPLE_SEARCH: "salesnav_people_search",
  };

  // Safety limits (client-side; the backend enforces its own).
  const LIMITS = {
    // Maximum people retained in one reviewed submission. Prevents runaway
    // captures; the operator still includes or excludes each row by hand.
    MAX_RECORDS_PER_BATCH: 500,
    // Reject a serialized payload larger than this before sending.
    MAX_PAYLOAD_BYTES: 5 * 1024 * 1024, // 5 MB
    // Longest a single result-page capture pass may scroll for (ms).
    CAPTURE_SCROLL_BUDGET_MS: 8000,
    // Operator metadata bounds, mirroring contact-capture.schema.json.
    MAX_LABELS: 25,
    MAX_LABEL_LENGTH: 64,
    MAX_NOTE_LENGTH: 2000,
  };

  // Chrome storage keys (non-secret preferences + recoverable draft batch +
  // the last successful submission result, kept so the operator can reopen the
  // saved contacts after the panel closes without recapturing).
  const STORAGE = {
    DRAFT_BATCH: "sn_draft_batch",
    PREFERENCES: "sn_preferences",
    LAST_RESULT: "sn_last_stage_result",
  };

  // Default, overridable operator preferences. No secrets, no remote URLs, and
  // deliberately no campaign: acquisition never needs one.
  const DEFAULT_PREFERENCES = {
    // Local VMR backend base URL. Loopback only by default.
    backendBaseUrl: "http://127.0.0.1:8000",
    // Where a "Save" goes during development: "mock" | "backend".
    sendTarget: "mock",
    // Mock/local receiver used only for testing the send flow.
    mockReceiverUrl: "http://127.0.0.1:8787/api/intake/contact-captures",
    maxRecordsPerBatch: 500,
    // Labels the operator most recently applied, offered again next time. Plain
    // names only — the backend owns the canonical label registry.
    recentLabels: [],
  };

  // Origins the extension is allowed to talk to for handoff. Loopback + the
  // configured backend base URL only. LinkedIn is a *read* surface, never a
  // POST target.
  const ALLOWED_BACKEND_ORIGIN_PATTERNS = [
    /^http:\/\/127\.0\.0\.1(:\d+)?$/,
    /^http:\/\/localhost(:\d+)?$/,
    /^http:\/\/\[::1\](:\d+)?$/,
  ];

  // Backend routes. The contact-capture route is the one the normal workflow
  // uses; the rest are the legacy campaign-era intakes.
  const CONTACT_CAPTURE_PATH = "/api/intake/contact-captures";
  const CONTACT_LABELS_PATH = "/api/contact-labels";
  const CONTACT_LOOKUP_PATH = "/api/contacts/lookup";
  const INTAKE_PATH = "/api/intake/sales-navigator/stage";
  const PROFILE_INTAKE_PATH = "/api/intake/linkedin-profile/stage";
  const COMPANY_INTAKE_PATH = "/api/intake/linkedin-company/stage";

  // Page surfaces the extension can be looking at. Detected by
  // src/common/surface.js (PageSurfaceDetector); each surface maps to one
  // side-panel mode. Detection never navigates — it only classifies the page
  // the operator already opened.
  const SURFACES = {
    SALESNAV_PEOPLE_RESULTS: "salesnav_people_results",
    PERSON_PROFILE: "linkedin_person_profile",
    COMPANY_PROFILE: "linkedin_company_profile",
    CHALLENGE: "challenge_or_login",
    UNAVAILABLE: "unavailable_profile",
    UNSUPPORTED: "unsupported_page",
  };

  // Record-level warning codes (stable strings for UI + backend).
  const WARNINGS = {
    MISSING_FIELD: "missing_field",
    SELECTOR_FAILURE: "selector_failure",
    DUPLICATE_UNCERTAIN: "duplicate_uncertain_identity",
    DUPLICATE_COLLAPSED: "duplicate_collapsed",
    MALFORMED_URL: "malformed_url",
    NO_STABLE_IDENTITY: "no_stable_identity",
    // Profile/company capture warning codes (DAT-012).
    MISSING_SECTION: "missing_section",
    UNPARSED_TIMELINE: "unparsed_timeline",
    UNRECOGNIZED_LAYOUT: "unrecognized_layout",
    UNPARSED_VALUE: "unparsed_value",
  };

  // Page-level capture status.
  const CAPTURE_STATUS = {
    OK: "ok",
    // Profile parsed, but one or more expected sections/fields are missing.
    // Missing data stays null with warnings — never fabricated.
    PARTIAL: "partial",
    UNSUPPORTED_PAGE: "unsupported_page",
    STRUCTURE_UNRECOGNIZED: "structure_unrecognized",
    CHALLENGE_DETECTED: "challenge_detected",
    UNAVAILABLE_PROFILE: "unavailable_profile",
    EMPTY: "empty",
  };

  // Chrome storage keys for the person/company capture drafts (kept separate
  // from the results batch so the two workflows never clobber each other).
  const PROFILE_STORAGE = {
    DRAFT_PROFILE: "li_draft_profile",
    LAST_PROFILE_RESULT: "li_last_profile_result",
    DRAFT_COMPANY: "li_draft_company",
    LAST_COMPANY_RESULT: "li_last_company_result",
  };

  // Contact-first storage: the operator metadata attached to the next
  // submission, and the archive a superseded campaign-era draft is moved to.
  const CONTACT_STORAGE = {
    OPERATOR_METADATA: "cc_operator_metadata",
    LEGACY_ARCHIVE: "cc_legacy_v1_archive",
    MIGRATION_NOTICE: "cc_migration_notice",
  };

  return {
    SCHEMA_VERSION,
    PROFILE_SCHEMA_VERSION,
    COMPANY_SCHEMA_VERSION,
    CONTACT_CAPTURE_SCHEMA_VERSION,
    SOURCE_IDENTIFIER,
    PROFILE_SOURCE_IDENTIFIER,
    COMPANY_SOURCE_IDENTIFIER,
    CONTACT_CAPTURE_SOURCE_IDENTIFIER,
    CAPTURE_MODES,
    LIMITS,
    STORAGE,
    PROFILE_STORAGE,
    CONTACT_STORAGE,
    DEFAULT_PREFERENCES,
    ALLOWED_BACKEND_ORIGIN_PATTERNS,
    CONTACT_CAPTURE_PATH,
    CONTACT_LABELS_PATH,
    CONTACT_LOOKUP_PATH,
    INTAKE_PATH,
    PROFILE_INTAKE_PATH,
    COMPANY_INTAKE_PATH,
    SURFACES,
    WARNINGS,
    CAPTURE_STATUS,
  };
});
