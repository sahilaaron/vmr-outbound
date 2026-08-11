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
  // The CONTACT-FIRST contract is what the normal workflow uses. A submission
  // always saves permanent Contacts; Campaign selection is an optional filing
  // shortcut, never an acquisition prerequisite.
  const CONTACT_CAPTURE_SCHEMA_VERSION = "linkedin-contact-capture/2.1.0";
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
    CAPTURE_SCROLL_BUDGET_MS: 20000,
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

  // Default, overridable operator preferences. No secrets or remote URLs.
  // Optional Campaign filing is stored separately from acquisition preferences.
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

  // The hosted VMR deployments this extension may send to, named exactly. There
  // is no pattern, no wildcard and no operator-typed hostname here on purpose:
  // a send target is where reviewed personal data goes, and "whatever the
  // operator pasted" is not a boundary. Adding a deployment is a deliberate
  // release that also declares the matching optional host permission in the
  // manifest — `test/config-parity.test.js` fails if the two drift.
  //
  // HTTPS only. A hosted capture carries a bearer credential, and a credential
  // over plaintext is a credential given away.
  const HOSTED_BACKEND_ORIGINS = ["https://srv1885453.hstgr.cloud"];

  // Origins the extension is allowed to talk to for handoff: loopback for local
  // development, plus the named hosted deployments above. LinkedIn is a *read*
  // surface, never a POST target.
  //
  // The two loopback hosts are exactly the ones `optional_host_permissions`
  // declares in the manifest, and they are what the local-development contract
  // documents (docs/DEVELOPMENT.md runs uvicorn on `--host 127.0.0.1`). An
  // `http://[::1]` entry used to sit here as well: it passed this check, then
  // produced the match pattern `http://[::1]/*`, which the manifest does not
  // declare — so the permission could never be granted and every send failed
  // `permission_denied`. A target that validates but can never work is worse
  // than one that is refused immediately, so the IPv6 spelling is not accepted.
  const ALLOWED_BACKEND_ORIGIN_PATTERNS = [
    /^http:\/\/127\.0\.0\.1(:\d+)?$/,
    /^http:\/\/localhost(:\d+)?$/,
    /^https:\/\/srv1885453\.hstgr\.cloud$/,
  ];

  // ---- Hosted capture credential (Beta) --------------------------------------
  //
  // A hosted VMR deployment is on the Internet, so the intake it exposes is
  // authenticated. The credential is a VMR-application bearer secret issued per
  // install: it is NOT the operator's hosted sign-in cookie, NOT a Google token,
  // and NOT a Gmail grant. It authorises one thing — submitting captures on the
  // enumerated intake contract — and it is revocable server-side by key id.
  //
  // Presented as `Authorization: Bearer vmrx1.<key_id>.<secret>`.
  const CREDENTIAL_SCHEME = "vmrx1";
  // Shape check only, so an obviously-wrong paste is refused at the field rather
  // than becoming a mystery 401 three screens later. The backend is the only
  // authority on whether a credential is real.
  const CREDENTIAL_PATTERN = /^vmrx1\.[a-z0-9][a-z0-9._-]{0,62}\.[A-Za-z0-9_-]{32,}$/;

  // Where the credential lives: `chrome.storage.session`, not `local`.
  //
  // `session` is in-memory for the browser session, is never written to disk,
  // and defaults to TRUSTED_CONTEXTS — so no content script running on a
  // LinkedIn page can read it even if that page is hostile. The cost is real and
  // deliberate: the operator re-enters the credential after a Chrome restart.
  // For an internal Beta credential that is the right trade, and the settings
  // screen says so plainly rather than letting it look like a bug.
  const CREDENTIAL_STORAGE = {
    CAPTURE_CREDENTIAL: "vmr_capture_credential",
  };

  // Backend routes. The contact-capture route is the one the normal workflow
  // uses; the rest are the legacy campaign-era intakes.
  const CONTACT_CAPTURE_PATH = "/api/intake/contact-captures";
  const CONTACT_LABELS_PATH = "/api/contact-labels";
  const CAMPAIGNS_PATH = "/api/campaigns";
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

  // Why a visible Sales Navigator row was not offered as capturable (DAT-018).
  // A skipped row is reported truthfully and never repaired by inference.
  const SKIP_REASONS = {
    MISSING_COMPANY_NAME: "missing_company_name",
  };

  // Operator-controlled scrolling over the ALREADY OPEN results page (DAT-018).
  // Smooth and incremental so rows have time to render and the operator can
  // watch what is happening. Bounded and cancellable by construction: there is
  // no pagination, no navigation, and no unattended traversal.
  const SCROLL = {
    // Fraction of the viewport advanced per increment. Well under one screen so
    // content is never skipped past.
    STEP_RATIO: 0.35,
    MIN_STEP_PX: 120,
    // Pause after each increment to let lazy rows render and layout settle.
    SETTLE_MS: 450,
    // Extra pause when the row count grew, since a batch just mounted.
    GROWTH_SETTLE_MS: 700,
    // Consecutive increments with no new rows before the pass stops.
    STABLE_CHECKS: 3,
    // Hard ceiling on increments, independent of the time budget.
    MAX_STEPS: 120,
    // Each pause is drawn from a range around its base rather than being a
    // fixed value plus a small addition. Two reasons, both about the page:
    //
    //  - Render latency genuinely varies. A fixed wait is either too short for
    //    the slow case or wasteful for the fast one, and most increments do not
    //    need a full settle window at all — hence a floor well below the base.
    //  - A constant interval can lock in phase with the page's own render
    //    cadence, which makes row counts read mid-mount.
    //
    // It is NOT detection avoidance and NOT human-mimicking: the range is
    // small, fixed, documented, and driven by an injected random source so
    // tests stay deterministic. The pass remains operator-initiated, bounded
    // and cancellable; nothing here changes what is read or how much.
    PAUSE_MIN_FACTOR: 0.45,
    PAUSE_MAX_FACTOR: 1.25,
  };

  // Record-level warning codes (stable strings for UI + backend).
  const WARNINGS = {
    MISSING_FIELD: "missing_field",
    SELECTOR_FAILURE: "selector_failure",
    DUPLICATE_UNCERTAIN: "duplicate_uncertain_identity",
    DUPLICATE_COLLAPSED: "duplicate_collapsed",
    MALFORMED_URL: "malformed_url",
    NO_STABLE_IDENTITY: "no_stable_identity",
    // A value the adapter computed from another observed value rather than read
    // off the page. Always paired with the field and the source field, so a
    // derivation can never be mistaken for an observation (DAT-018).
    DERIVED_VALUE: "derived_value",
    // Profile/company capture warning codes (DAT-012).
    MISSING_SECTION: "missing_section",
    UNPARSED_TIMELINE: "unparsed_timeline",
    UNRECOGNIZED_LAYOUT: "unrecognized_layout",
    UNPARSED_VALUE: "unparsed_value",
    // The page rendered an explicit placeholder where a value would go (for
    // example LinkedIn's literal "--" for an empty headline). The field stays
    // null: a placeholder is not content, and storing it would look like one.
    PLACEHOLDER_VALUE: "placeholder_value",
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
    FILING_CONTEXT: "cc_filing_context",
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
    SCROLL,
    SKIP_REASONS,
    STORAGE,
    PROFILE_STORAGE,
    CONTACT_STORAGE,
    CREDENTIAL_STORAGE,
    CREDENTIAL_SCHEME,
    CREDENTIAL_PATTERN,
    DEFAULT_PREFERENCES,
    ALLOWED_BACKEND_ORIGIN_PATTERNS,
    HOSTED_BACKEND_ORIGINS,
    CONTACT_CAPTURE_PATH,
    CONTACT_LABELS_PATH,
    CAMPAIGNS_PATH,
    CONTACT_LOOKUP_PATH,
    INTAKE_PATH,
    PROFILE_INTAKE_PATH,
    COMPANY_INTAKE_PATH,
    SURFACES,
    WARNINGS,
    CAPTURE_STATUS,
  };
});
