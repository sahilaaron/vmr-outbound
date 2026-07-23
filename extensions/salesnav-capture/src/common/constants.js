/**
 * Shared constants for the Sales Navigator capture extension.
 *
 * UMD-style module: works as a CommonJS module in Node (tests) and as a global
 * `self.SNCapture.constants` when loaded as a classic script in the content
 * script, service worker, or side panel. No bundler, no remote code.
 */
(function (root, factory) {
  const mod = factory();
  if (typeof module !== "undefined" && module.exports) module.exports = mod;
  const g = typeof self !== "undefined" ? self : root;
  g.SNCapture = Object.assign(g.SNCapture || {}, { constants: mod });
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  // Versioned contract identifier for the backend intake payload. Bump on any
  // breaking change to the record/payload shape (see docs/BACKEND_CONTRACT.md).
  const SCHEMA_VERSION = "salesnav-capture/1.0.0";

  // Identifies the client that produced the batch.
  const SOURCE_IDENTIFIER = "chrome-extension:salesnav-capture";

  // Safety limits (client-side; the backend enforces its own).
  const LIMITS = {
    // Maximum records retained in one draft batch. Prevents runaway captures.
    MAX_RECORDS_PER_BATCH: 500,
    // Reject a serialized payload larger than this before sending.
    MAX_PAYLOAD_BYTES: 5 * 1024 * 1024, // 5 MB
    // Longest a single result-page capture pass may scroll for (ms).
    CAPTURE_SCROLL_BUDGET_MS: 8000,
  };

  // Chrome storage keys (non-secret preferences + recoverable draft batch +
  // the last successful staging result, kept so the operator can reopen the
  // staged batch after the popup closes without recapturing).
  const STORAGE = {
    DRAFT_BATCH: "sn_draft_batch",
    PREFERENCES: "sn_preferences",
    LAST_RESULT: "sn_last_stage_result",
  };

  // Default, overridable operator preferences. No secrets, no remote URLs.
  const DEFAULT_PREFERENCES = {
    // Local VMR backend base URL. Loopback only by default.
    backendBaseUrl: "http://127.0.0.1:8000",
    // Output destination for the *production* default must require an explicit
    // operator action; nothing is ever sent automatically.
    lastCampaignId: "",
    // Where a "Send" goes during development: "mock" | "backend".
    sendTarget: "mock",
    // Mock/local receiver used only for testing the send flow.
    mockReceiverUrl: "http://127.0.0.1:8787/api/intake/sales-navigator/stage",
    maxRecordsPerBatch: 500,
  };

  // Origins the extension is allowed to talk to for handoff. Loopback + the
  // configured backend base URL only. LinkedIn is a *read* surface, never a
  // POST target.
  const ALLOWED_BACKEND_ORIGIN_PATTERNS = [
    /^http:\/\/127\.0\.0\.1(:\d+)?$/,
    /^http:\/\/localhost(:\d+)?$/,
    /^http:\/\/\[::1\](:\d+)?$/,
  ];

  // The backend route the contract targets. Final name reconciled to repo
  // conventions after PR #120 merges (see docs/BACKEND_CONTRACT.md).
  const INTAKE_PATH = "/api/intake/sales-navigator/stage";

  // Record-level warning codes (stable strings for UI + backend).
  const WARNINGS = {
    MISSING_FIELD: "missing_field",
    SELECTOR_FAILURE: "selector_failure",
    DUPLICATE_UNCERTAIN: "duplicate_uncertain_identity",
    DUPLICATE_COLLAPSED: "duplicate_collapsed",
    MALFORMED_URL: "malformed_url",
    NO_STABLE_IDENTITY: "no_stable_identity",
  };

  // Page-level capture status.
  const CAPTURE_STATUS = {
    OK: "ok",
    UNSUPPORTED_PAGE: "unsupported_page",
    STRUCTURE_UNRECOGNIZED: "structure_unrecognized",
    CHALLENGE_DETECTED: "challenge_detected",
    EMPTY: "empty",
  };

  return {
    SCHEMA_VERSION,
    SOURCE_IDENTIFIER,
    LIMITS,
    STORAGE,
    DEFAULT_PREFERENCES,
    ALLOWED_BACKEND_ORIGIN_PATTERNS,
    INTAKE_PATH,
    WARNINGS,
    CAPTURE_STATUS,
  };
});
