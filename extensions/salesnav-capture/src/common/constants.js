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
  //
  // TWO DIFFERENT CEILINGS, and conflating them was the whole defect. One
  // reviewed capture set is what the OPERATOR works with; one submission is
  // what a single HTTP request carries. They used to be the same number, so the
  // only way to save more people was to send a bigger request — and a bigger
  // request is a bigger thing to lose, retry, and time out.
  //
  //   MAX_RECORDS_PER_BATCH        the logical push: what one Save may contain
  //   MAX_CONTACTS_PER_SUBMISSION  the wire contract: what one POST may contain
  //
  // The wire ceiling is unchanged and still equals `maxItems` in
  // docs/contact-capture.schema.json and the backend's own validator;
  // test/contact-schema-parity.test.js fails if the two drift.
  const LIMITS = {
    // Maximum people retained in one reviewed capture set — one operator Save.
    // Delivered as several bounded requests, never as one large one. Prevents
    // runaway captures; the operator still includes or excludes each row.
    MAX_RECORDS_PER_BATCH: 5000,
    // Maximum people in ONE contact-capture request. The committed contract's
    // `maxItems`; the transport never exceeds it whatever the push contains.
    MAX_CONTACTS_PER_SUBMISSION: 500,
    // Reject a serialized REQUEST larger than this before sending. This is a
    // per-request ceiling, not a per-push one.
    MAX_PAYLOAD_BYTES: 5 * 1024 * 1024, // 5 MB
    // Longest a single result-page capture pass may scroll for (ms).
    CAPTURE_SCROLL_BUDGET_MS: 20000,
    // Operator metadata bounds, mirroring contact-capture.schema.json.
    MAX_LABELS: 25,
    MAX_LABEL_LENGTH: 64,
    MAX_NOTE_LENGTH: 2000,
  };

  // ---- Chunked background push ------------------------------------------------
  //
  // One operator Save is a LOGICAL submission delivered as a sequence of bounded,
  // independently idempotent requests. The numbers below are chosen from the
  // measured contract rather than guessed:
  //
  //   a Sales Navigator contact serializes to ~2.9 KB (measured over the
  //   committed fixture shape, raw snapshot included);
  //   a person-profile contact is larger — an `about_text` alone may be 8 KB.
  //
  // So a 100-contact chunk is ~0.3 MB of Sales Navigator rows and stays under
  // the byte ceiling for profile-sized records too, because the byte ceiling is
  // enforced independently of the count. CHUNK_MAX_BYTES leaves 4x headroom
  // under the backend's 8 MB body limit (`contact_capture_intake_max_bytes`) and
  // sits below this extension's own 5 MB request gate, so a chunk that this
  // planner accepts can never be refused for size at either end.
  const PUSH = {
    // Records per chunk, before the byte ceiling is consulted.
    CHUNK_MAX_CONTACTS: 100,
    // Serialized bytes per chunk request, envelope included.
    CHUNK_MAX_BYTES: 2 * 1024 * 1024, // 2 MB
    // A single record larger than CHUNK_MAX_BYTES travels alone rather than
    // being dropped. Only a record that cannot fit even a request of its own is
    // refused — deterministically, at plan time, so it can never become a chunk
    // that retries for ever.
    RECORD_MAX_BYTES: 5 * 1024 * 1024, // 5 MB, == MAX_PAYLOAD_BYTES
    // Attempts per chunk before it is parked as failed-but-retryable-by-hand.
    MAX_ATTEMPTS: 5,
    // Backoff before attempt 2, 3, 4, 5 (ms). Bounded and fixed: a push that is
    // failing should get slower, not disappear.
    RETRY_BACKOFF_MS: [5000, 15000, 45000, 120000],
    // Detailed per-contact result entries retained for display. A push of 5,000
    // people is still 5,000 people processed — this bounds what is STORED for
    // the outcome card, and the job records how many were dropped so the panel
    // can say so instead of implying the rest never happened.
    MAX_RETAINED_RESULTS: 500,
    // Failed-chunk records retained. Same reasoning, much smaller numbers.
    MAX_RETAINED_FAILURES: 50,
    // The alarm that resumes a push after the service worker is suspended.
    RESUME_ALARM: "vmr_push_resume",
    // How often that alarm fires while a push is unfinished (minutes). Chrome
    // clamps periodic alarms to one minute; asking for less would be ignored.
    RESUME_PERIOD_MINUTES: 1,
  };

  // Chrome storage keys (non-secret preferences + recoverable draft batch +
  // the last successful submission result, kept so the operator can reopen the
  // saved contacts after the panel closes without recapturing).
  const STORAGE = {
    DRAFT_BATCH: "sn_draft_batch",
    PREFERENCES: "sn_preferences",
    LAST_RESULT: "sn_last_stage_result",
  };

  // The hosted VMR deployments this extension may send to, named exactly. There
  // is no pattern, no wildcard and no operator-typed hostname here on purpose:
  // a send target is where reviewed personal data goes, and "whatever the
  // operator pasted" is not a boundary. Adding a deployment is a deliberate
  // release that also declares the matching REQUIRED host permission in the
  // manifest — `test/config-parity.test.js` fails if the two drift, and fails
  // if a hosted origin is declared as optional instead.
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

  // Product-configured operator preferences.
  //
  // `backendBaseUrl` is NOT an operator field. The hosted deployment above is
  // the product default and the ordinary panel has no control that can change
  // it; only the development override (see DEV_OVERRIDES below) can, and that
  // override cannot be reached from any shipped UI. `sendTarget`/`mockReceiverUrl`
  // exist for the same development path — the ordinary default sends captures to
  // the product's hosted backend, authorised by the operator's own account link.
  const DEFAULT_PREFERENCES = {
    backendBaseUrl: HOSTED_BACKEND_ORIGINS[0],
    // "backend" | "mock". "mock" is reachable only under a development override.
    sendTarget: "backend",
    // Development-only receiver (tools/mock-receiver.js). Never used unless a
    // development override switches `sendTarget` to "mock".
    mockReceiverUrl: "http://127.0.0.1:8787/api/intake/contact-captures",
    maxRecordsPerBatch: 5000,
    // Labels the operator most recently applied, offered again next time. Plain
    // names only — the backend owns the canonical label registry.
    recentLabels: [],
  };

  // ---- Account link (PKCE authorization code, first-party) --------------------
  //
  // Hosted capture is authorised by the operator's own VMR Outbound account, not
  // by a shared secret anybody types. The extension runs a PKCE authorization-code
  // flow against the hosted app through `chrome.identity.launchWebAuthFlow`, and
  // holds two opaque, rotating, DB-backed tokens:
  //
  //   access token   `vmre1.<session id>.<secret>`  ~15 minutes, held in
  //                  `chrome.storage.session` (memory only, never on disk)
  //   refresh token  `vmrr1.<session id>.<secret>`  ~30 days, ROTATES on every
  //                  use, held in `chrome.storage.local` so a browser restart
  //                  restores access with no human action
  //
  // A refresh token is not a shared secret: it belongs to exactly one install,
  // is replaced on every use (a replay revokes the link server-side), and is
  // never shown to or typed by anybody.
  const ACCOUNT_LINK_PATHS = {
    AUTHORIZE: "/extension/authorize",
    TOKEN: "/extension/token",
    REVOKE: "/extension/revoke",
  };

  const ACCOUNT_LINK = {
    SCOPE: "capture",
    // Refresh rather than gamble when this little of the access token's life is
    // left, so a request never races its own expiry.
    MIN_ACCESS_REMAINING_MS: 60000,
    // Only used if the server omits `expires_in`; the server is authoritative.
    FALLBACK_ACCESS_TTL_SECONDS: 900,
    ACCESS_TOKEN_SCHEME: "vmre1",
    REFRESH_TOKEN_SCHEME: "vmrr1",
    // The extension's own redirect target, as `chrome.identity` mints it.
    REDIRECT_HOST_SUFFIX: ".chromiumapp.org",
  };

  const ACCOUNT_STORAGE = {
    // chrome.storage.local, non-secret: a stable per-install id so one VMR user
    // can link several browsers and revoke them individually.
    INSTALLATION_ID: "vmr_installation_id",
    // chrome.storage.local: { sessionId, refreshToken, accountEmail, scope,
    // linkedAt }. Persisted deliberately — this is what makes a browser restart
    // require nothing from the operator.
    ACCOUNT_LINK: "vmr_account_link",
    // chrome.storage.session: { accessToken, expiresAt }. Never written to disk.
    ACCESS_TOKEN: "vmr_access_token",
    // chrome.storage.local: the development override gate. Nothing in any
    // shipped UI writes this key, and no message handler sets it: it can only be
    // created by hand from the extension's own devtools console on an unpacked
    // build. An ordinary staging/production install can never reach it.
    DEV_OVERRIDES: "vmr_dev_overrides",
  };

  // ---- Legacy `vmrx1` capture credential (development compatibility only) -----
  //
  // Superseded by the account link above. The backend keeps parsing this scheme
  // only when APP_ENV=local, so it is retained here for local/development
  // compatibility and for `test/config-parity.test.js`, which proves the two
  // definitions of the scheme cannot drift. It is NOT part of the ordinary path:
  // no shipped panel control sets it, the worker refuses to store one unless the
  // development override is present, and hosted capture never depends on it.
  //
  // Presented as `Authorization: Bearer vmrx1.<key_id>.<secret>`.
  const CREDENTIAL_SCHEME = "vmrx1";
  // Shape check only, so an obviously-wrong paste is refused at the field rather
  // than becoming a mystery 401 three screens later. The backend is the only
  // authority on whether a credential is real.
  const CREDENTIAL_PATTERN = /^vmrx1\.[a-z0-9][a-z0-9._-]{0,62}\.[A-Za-z0-9_-]{32,}$/;

  // Where the legacy credential lives when a developer sets one:
  // `chrome.storage.session`, never `local`. In-memory for the browser session,
  // never written to disk, and unreadable from a content script.
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

  // Durable background push (chunked delivery of one logical Save).
  //
  // The job record is small and rewritten after every state transition, so it is
  // held apart from the chunk payloads it plans. Each chunk's contacts live
  // under their own key and are DELETED the moment the backend accepts that
  // chunk, so a running push consumes less storage as it progresses instead of
  // holding the whole submission until the end.
  const PUSH_STORAGE = {
    JOB: "cc_push_job",
    // `CHUNK_PREFIX + jobId + ":" + chunkIndex`.
    CHUNK_PREFIX: "cc_push_chunk:",
  };

  // ---- Local export (operator-controlled, backend-independent) ---------------
  //
  // Restored in this slice. It was removed with the campaign-era archive on the
  // reasoning that a reviewed contact is saved into VMR Outbound or not saved at
  // all — which is true of ACQUISITION and says nothing about the operator's own
  // copy of what they just reviewed. A 5,000-row capture is hours of work, and
  // the operator gets to keep a backup of it without asking a server first.
  //
  // Downloading is not clearing and not sending: the reviewed set is untouched
  // and remains pushable afterwards.
  const EXPORT = {
    FORMATS: ["csv", "json"],
    FILENAME_PREFIX: "vmr_captured_contacts",
    CSV_MIME: "text/csv",
    JSON_MIME: "application/json",
  };

  // Contact-first storage: the operator metadata attached to the next
  // submission, plus two keys nothing writes any more.
  //
  // `LEGACY_ARCHIVE` and `MIGRATION_NOTICE` are named here only so
  // `common/migration.js` can CLEAR them from installs that ran an earlier
  // version. They fed the CAMPAIGN-ERA archived-draft download, which is a
  // different thing from the reviewed-capture export restored above: that
  // archive holds drafts reviewed under a contract this extension no longer
  // speaks, so it is still cleared rather than resurrected. See that module.
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
    STORAGE,
    PROFILE_STORAGE,
    CONTACT_STORAGE,
    PUSH_STORAGE,
    PUSH,
    EXPORT,
    ACCOUNT_STORAGE,
    ACCOUNT_LINK,
    ACCOUNT_LINK_PATHS,
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
