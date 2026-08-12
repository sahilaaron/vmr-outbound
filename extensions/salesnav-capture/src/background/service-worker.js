/**
 * Service worker: extension state hub + backend communication + downloads.
 *
 * Responsibilities:
 *  - Own the recoverable reviewed drafts in chrome.storage.local.
 *  - Relay capture/detect requests to the active tab's content script and merge
 *    result rows (dedupe) into the reviewed batch.
 *  - Build the CONTACT-FIRST submission and POST it — ONLY on explicit operator
 *    action — to the local backend or the dev mock receiver.
 *  - Produce JSON / CSV downloads as an offline fallback.
 *  - Persist optional Campaign filing context independently from Contact capture.
 *  - Migrate superseded campaign-bound local state explicitly.
 *
 * Campaign filing is optional and additive: acquisition always saves the person
 * first. Never posts to LinkedIn. Nothing is ever sent without an explicit
 * operator-triggered message.
 *
 * One credential, and only one: the VMR capture credential a hosted deployment
 * requires (see `common/constants.js`). It is held in `chrome.storage.session`,
 * attached only to a request going to a named hosted deployment, and never
 * returned to the panel, written to a log line, or stored beside the drafts. No
 * LinkedIn credential, cookie or session is ever read or kept.
 */
importScripts(
  "../common/constants.js",
  "../common/normalize.js",
  "../common/extraction.js",
  "../common/surface.js",
  "../common/dedupe.js",
  "../common/schema.js",
  "../common/profile-schema.js",
  "../common/contact-schema.js",
  "../common/migration.js",
  "../common/permissions.js",
  "../common/handoff.js"
);

const {
  constants,
  dedupe,
  schema,
  profileSchema,
  contactSchema,
  migration,
  normalize,
  permissions,
  handoff,
  surface,
} = self.SNCapture;
const {
  STORAGE,
  PROFILE_STORAGE,
  CONTACT_STORAGE,
  CREDENTIAL_STORAGE,
  CREDENTIAL_PATTERN,
  DEFAULT_PREFERENCES,
  LIMITS,
  CAPTURE_STATUS,
  CAPTURE_MODES,
  SURFACES,
  ALLOWED_BACKEND_ORIGIN_PATTERNS,
  CONTACT_CAPTURE_PATH,
  CONTACT_LABELS_PATH,
  CAMPAIGNS_PATH,
  CONTACT_LOOKUP_PATH,
  COMPANY_INTAKE_PATH,
} = constants;

const EXTENSION_VERSION = chrome.runtime.getManifest().version;

// Client-side abort budgets. Each one is set from the budget the *backend*
// already commits to for that route, so the client stops waiting after the
// server has had its chance to answer — never before.
//
//   - Reads (labels, campaigns, save-vs-refresh lookup) and the company intake
//     POST keep 15 s. The company route declares no server-side wall-clock
//     budget at all (app/core/config.py has max_bytes for it but no
//     `linkedin_company_intake_timeout_seconds`), and the reads are small
//     advisory queries, so there is no contract that would justify waiting
//     longer. Unchanged.
//
//   - The contact-capture POST gets 75 s. `contact_capture_intake_timeout_seconds`
//     defaults to 60 s (app/core/config.py) and the service enforces it
//     cooperatively plus via PostgreSQL `statement_timeout`, rolling the whole
//     submission back and returning 504 on breach. A 15 s client abort was
//     shorter than the server's own bounded path, so a slow-but-successful
//     submission was reported to the operator as a timeout while the server
//     went on to commit it. Waiting past the server's budget means the operator
//     sees the server's real verdict — 201, or a truthful 504 — instead of a
//     guess. The 15 s of headroom covers request/response transfer either side
//     of that budget.
//
// Retrying is safe at any of these values: the reviewed draft and its
// `client_submission_id` are preserved across a failure, and the backend
// replays an identical resubmission rather than duplicating it.
const SEND_TIMEOUT_MS = 15000;
const CONTACT_CAPTURE_TIMEOUT_MS = 75000;

async function migrateLegacyState() {
  try {
    await migration.runMigration(chrome.storage.local, {
      migratedAt: new Date().toISOString(),
    });
  } catch (_e) {
    // Migration is best-effort: a failure must never block the panel opening.
  }
}

// Open the side panel when the toolbar icon is clicked, and retire any
// campaign-era local state on install/update and on browser start.
chrome.runtime.onInstalled.addListener(() => {
  if (chrome.sidePanel && chrome.sidePanel.setPanelBehavior) {
    chrome.sidePanel.setPanelBehavior({ openPanelOnActionClick: true }).catch(() => {});
  }
  migrateLegacyState();
});
if (chrome.runtime.onStartup) chrome.runtime.onStartup.addListener(migrateLegacyState);

// ---- hosted capture credential ---------------------------------------------
//
// Held in `chrome.storage.session`: in-memory for the browser session, never
// written to disk, and unreadable from a content script. Everything below is
// written so the value has exactly one exit: the `Authorization` header of a
// request to a named hosted deployment. It is never returned to the panel,
// never included in a response object, and never passed to `console`.

/** Session storage, or null on a browser too old to have it. */
function credentialStore() {
  return (chrome.storage && chrome.storage.session) || null;
}

async function getCredential() {
  const store = credentialStore();
  if (!store) return null;
  try {
    const data = await store.get(CREDENTIAL_STORAGE.CAPTURE_CREDENTIAL);
    const value = data && data[CREDENTIAL_STORAGE.CAPTURE_CREDENTIAL];
    return typeof value === "string" && value ? value : null;
  } catch (_e) {
    return null;
  }
}

/**
 * Store a pasted credential after a shape check.
 *
 * The shape check refuses an obviously-wrong paste — a truncated copy, the
 * configuration digest pasted instead of the credential — at the field, so the
 * operator learns immediately rather than through a 401 three screens later.
 * The backend remains the only authority on whether a well-formed credential is
 * a real one.
 */
async function setCredential(value) {
  const store = credentialStore();
  if (!store) return { ok: false, error: "credential_storage_unavailable" };
  const candidate = typeof value === "string" ? value.trim() : "";
  if (!CREDENTIAL_PATTERN.test(candidate)) {
    return { ok: false, error: "credential_malformed" };
  }
  await store.set({ [CREDENTIAL_STORAGE.CAPTURE_CREDENTIAL]: candidate });
  return { ok: true, hasCredential: true };
}

async function clearCredential() {
  const store = credentialStore();
  if (store) await store.remove(CREDENTIAL_STORAGE.CAPTURE_CREDENTIAL);
  return { ok: true, hasCredential: false };
}

/**
 * Whether a credential is held — never the credential itself.
 *
 * This is the only thing the panel is ever told about it, which is what keeps
 * the value out of the panel's DOM, its state, and anything it might render.
 */
async function credentialState() {
  return {
    ok: true,
    hasCredential: (await getCredential()) !== null,
    storageAvailable: credentialStore() !== null,
  };
}

/**
 * Headers for one backend request, or the refusal to make it.
 *
 * A loopback target is unchanged and carries no credential: local development
 * has no authenticated intake and adding one would be a behaviour change nobody
 * asked for. A named hosted deployment must carry the credential, and a missing
 * one is refused here rather than sent anyway to collect a 401 — the operator
 * gets "set the credential", not "the backend rejected you".
 */
async function requestHeaders(url, extra) {
  const headers = Object.assign({}, extra || {});
  if (!permissions.isHostedUrl(url)) return { ok: true, headers };
  const credential = await getCredential();
  if (!credential) {
    return {
      ok: false,
      error: "credential_missing",
      message: "Hosted VMR needs a capture credential. Add it in Settings, then retry.",
    };
  }
  headers["Authorization"] = "Bearer " + credential;
  return { ok: true, headers };
}

// ---- storage helpers ------------------------------------------------------

async function getPrefs() {
  const data = await chrome.storage.local.get(STORAGE.PREFERENCES);
  return Object.assign({}, DEFAULT_PREFERENCES, data[STORAGE.PREFERENCES] || {});
}
async function setPrefs(patch) {
  const prefs = await getPrefs();
  const next = Object.assign({}, prefs, patch || {});
  await chrome.storage.local.set({ [STORAGE.PREFERENCES]: next });
  return next;
}
async function getBatch() {
  const data = await chrome.storage.local.get(STORAGE.DRAFT_BATCH);
  return data[STORAGE.DRAFT_BATCH] || null;
}
async function setBatch(batch) {
  await chrome.storage.local.set({ [STORAGE.DRAFT_BATCH]: batch });
  return batch;
}
// ---- retained results (UI-016) ---------------------------------------------
//
// A retained result is the small, safe summary of a successful submission, kept
// so the operator can reopen it — and its returned workbench link — after the
// panel closes, without recapturing or resaving.
//
// The result alone cannot say WHICH page it describes, so a panel restoring it
// had no way to tell "this is still your result" from "this belongs to the
// person you were looking at ten minutes ago". Each retained result is therefore
// stored beside the capture context it came from:
//
//   { kind: "listings" | "profile" | "company", url: <normalized URL | null> }
//
// A record written before this existed has no context. It is returned with
// `context: null`, and the panel declines to place it rather than guess.

const RETAINED_RESULT_VERSION = 1;

/** The stored shape: the result the operator sees, and the page it belongs to. */
function retainedRecord(result, context) {
  return { v: RETAINED_RESULT_VERSION, result, context: context || null };
}

/** Read a retained record, tolerating one written before UI-016. */
function readRetainedRecord(raw) {
  if (!raw) return { result: null, context: null };
  if (raw.v === RETAINED_RESULT_VERSION) {
    return { result: raw.result || null, context: raw.context || null };
  }
  return { result: raw, context: null };
}

/** The capture context of one LinkedIn page. A URL that will not normalize
 *  yields a null url, which reads downstream as "cannot be placed". */
function pageContext(kind, url) {
  const n = normalize.normalizeLinkedInUrl(url);
  return { kind, url: n.valid ? n.url : null };
}

// A results batch is captured across several pages of one search, so it belongs
// to the listings workflow rather than to any single URL.
const LISTINGS_CONTEXT = { kind: "listings", url: null };

async function getLastResult() {
  const data = await chrome.storage.local.get(STORAGE.LAST_RESULT);
  return readRetainedRecord(data[STORAGE.LAST_RESULT]);
}
async function setLastResult(result, context) {
  await chrome.storage.local.set({ [STORAGE.LAST_RESULT]: retainedRecord(result, context) });
  return result;
}
async function clearLastResult() {
  await chrome.storage.local.remove(STORAGE.LAST_RESULT);
}
async function ensureBatch() {
  let batch = await getBatch();
  if (!batch) {
    batch = {
      clientBatchId: schema.newBatchId(),
      createdAt: new Date().toISOString(),
      records: [],
      pagesCaptured: [],
      statuses: [],
      lastSearchUrl: null,
    };
    await setBatch(batch);
  }
  return batch;
}

// ---- active tab / content-script bridge -----------------------------------

async function findActiveSalesTab() {
  const tabs = await chrome.tabs.query({ active: true, lastFocusedWindow: true });
  let tab = tabs && tabs[0];
  if (!tab || !/^https:\/\/www\.linkedin\.com\/sales\//.test(tab.url || "")) {
    // Fall back to any active linkedin/sales tab across normal windows.
    const all = await chrome.tabs.query({ url: "https://www.linkedin.com/sales/*" });
    tab = all.find((t) => t.active) || all[0] || tab;
  }
  return tab || null;
}

async function askContentScript(message) {
  const tab = await findActiveSalesTab();
  if (!tab) {
    return { ok: false, error: "no_sales_tab", message: "Open a Sales Navigator page in the active tab." };
  }
  if (!/^https:\/\/www\.linkedin\.com\/sales\//.test(tab.url || "")) {
    return { ok: false, error: "unsupported_tab", url: tab.url };
  }
  try {
    const resp = await chrome.tabs.sendMessage(tab.id, message);
    return { ok: true, tab, resp };
  } catch (e) {
    // Content script not present (e.g. page loaded before install). Inject it.
    try {
      await chrome.scripting.executeScript({
        target: { tabId: tab.id },
        files: [
          "src/common/constants.js",
          "src/common/normalize.js",
          "src/common/extraction.js",
          // content-script.js requires self.SNCapture.scroller (DAT-018 D). It
          // must be injected here as well as in the manifest, or a Sales
          // Navigator page opened BEFORE install/reload bails out at the
          // shared-module guard and capture silently stops working.
          "src/common/scroller.js",
          "src/content/content-script.js",
        ],
      });
      const resp = await chrome.tabs.sendMessage(tab.id, message);
      return { ok: true, tab, resp };
    } catch (e2) {
      return { ok: false, error: "content_script_unavailable", detail: String(e2 && e2.message) };
    }
  }
}

// ---- capture flow ---------------------------------------------------------

async function detectActivePage() {
  const r = await askContentScript({ type: "CS_DETECT" });
  if (!r.ok) return { ok: false, error: r.error, message: r.message, url: r.url };
  return { ok: true, page: r.resp };
}

// Whether a CS_CAPTURE is currently in flight. Cancellation is only meaningful
// while one is: with no capture running there is nothing to stop, and the panel
// is told so rather than being allowed to believe it cancelled something.
let captureInFlight = false;

/**
 * Stop the scrolling pass the operator started (DAT-018 D).
 *
 * Deliberately does NOT go through askContentScript: that injects the content
 * script when it is missing, and injecting a fresh script in order to cancel a
 * pass that cannot exist in it is pointless work. If the script is not there,
 * no pass is running.
 *
 * Cancelling is an operator action, not a failure: the in-flight CS_CAPTURE
 * still resolves, still returns the rows that were already on the page, and
 * still leaves the batch and the reviewed draft intact. Nothing is submitted.
 */
async function cancelActiveCapture() {
  if (!captureInFlight) {
    return { ok: true, cancelled: false, reason: "no_active_capture" };
  }
  const tab = await findActiveSalesTab();
  if (!tab) return { ok: true, cancelled: false, reason: "no_sales_tab" };
  try {
    const resp = await chrome.tabs.sendMessage(tab.id, { type: "CS_CANCEL_SCROLL" });
    return {
      ok: true,
      cancelled: !!(resp && resp.cancelled),
      reason: (resp && resp.reason) || null,
      passId: (resp && resp.passId) || null,
    };
  } catch (_e) {
    // No content script in the tab, so no pass is running in it.
    return { ok: true, cancelled: false, reason: "content_script_unavailable" };
  }
}

async function captureActivePage() {
  captureInFlight = true;
  let r;
  try {
    r = await askContentScript({ type: "CS_CAPTURE" });
  } finally {
    captureInFlight = false;
  }
  if (!r.ok) return { ok: false, error: r.error, message: r.message, url: r.url };
  const result = r.resp; // extractPage() output

  const batch = await ensureBatch();
  batch.lastSearchUrl = result.sourceSearchUrl || batch.lastSearchUrl;
  batch.statuses.push({ status: result.status, page: result.sourcePageNumber, at: result.capturedAt });

  // Only OK captures contribute records. Non-OK statuses are surfaced but never
  // treated as a successful empty capture.
  if (result.status !== CAPTURE_STATUS.OK) {
    await setBatch(batch);
    return {
      ok: true,
      captureStatus: result.status,
      pageWarnings: result.pageWarnings,
      added: 0,
      collapsed: 0,
      uncertain: 0,
      overLimit: false,
      // Carried on the non-OK path too: a pass the operator cancelled before
      // any row loaded is a cancellation, not an empty results page, and the
      // panel must be able to tell the two apart.
      scroll: result.scroll || null,
      batchView: buildBatchView(batch),
    };
  }

  // Enforce the max-records cap.
  const remaining = Math.max(0, effectiveMax(await getPrefs()) - batch.records.length);
  let incoming = result.records;
  let overLimit = false;
  if (incoming.length > remaining) {
    incoming = incoming.slice(0, remaining);
    overLimit = true;
  }

  // Mint one stable capture id per row now, so a retry of the same reviewed
  // content re-sends the SAME ids and the backend replays it idempotently.
  for (const rec of incoming) {
    if (!rec._captureId) rec._captureId = contactSchema.newId();
  }

  const merged = dedupe.mergeBatch(batch.records, incoming);
  batch.records = merged.records;
  // The reviewed content changed, so the previous submission id no longer
  // describes it. A new id is minted at send time.
  batch.clientSubmissionId = null;
  // UI-016: for the same reason the retained result no longer describes this
  // batch. Newly captured rows are unsent work, and an older result must never
  // stand in front of them — the same rule `profileCapture` already applies to
  // a recaptured profile.
  await clearLastResult();
  if (result.sourcePageNumber != null && !batch.pagesCaptured.includes(result.sourcePageNumber)) {
    batch.pagesCaptured.push(result.sourcePageNumber);
  }
  await setBatch(batch);

  return {
    ok: true,
    captureStatus: result.status,
    pageWarnings: result.pageWarnings,
    added: merged.added,
    collapsed: merged.collapsed,
    uncertain: merged.uncertain,
    overLimit,
    // DAT-018 B: rows the page showed but that carry no Company Name. They are
    // reported truthfully and never entered the batch, so they cannot be sent.
    skipped: result.skipped || [],
    skippedCount: result.skippedCount || 0,
    visibleCount: result.visibleCount != null ? result.visibleCount : null,
    scroll: result.scroll || null,
    batchView: buildBatchView(batch),
  };
}

function effectiveMax(prefs) {
  const p = Number(prefs && prefs.maxRecordsPerBatch);
  if (Number.isFinite(p) && p > 0) return Math.min(p, LIMITS.MAX_RECORDS_PER_BATCH);
  return LIMITS.MAX_RECORDS_PER_BATCH;
}

function buildBatchView(batch) {
  return {
    clientBatchId: batch.clientBatchId,
    createdAt: batch.createdAt,
    lastSearchUrl: batch.lastSearchUrl,
    pagesCaptured: batch.pagesCaptured.slice().sort((a, b) => a - b),
    statuses: batch.statuses.slice(-10),
    summary: dedupe.summarize(batch.records),
    records: batch.records,
  };
}

// ---- exclude / clear ------------------------------------------------------

async function toggleExclude(stableKey, index) {
  const batch = await ensureBatch();
  let rec = null;
  if (stableKey) rec = batch.records.find((r) => r._stableKey === stableKey);
  if (!rec && Number.isInteger(index)) rec = batch.records[index];
  if (rec) rec._excluded = !rec._excluded;
  // Including or excluding a row changes what would be submitted.
  batch.clientSubmissionId = null;
  await setBatch(batch);
  return buildBatchView(batch);
}

async function clearBatch() {
  await chrome.storage.local.remove(STORAGE.DRAFT_BATCH);
  // Clearing the reviewed batch also discards the last staging result: the
  // staged batch is only meaningful while its reviewed source exists.
  await clearLastResult();
  const fresh = await ensureBatch();
  return buildBatchView(fresh);
}

// ---- payload build + send -------------------------------------------------

function includedRecords(batch) {
  return batch.records.filter((r) => !r._excluded);
}

function warningsSummary(records) {
  const counts = {};
  for (const r of records) {
    for (const w of r.warnings || []) counts[w.code] = (counts[w.code] || 0) + 1;
  }
  return counts;
}

// ---- operator metadata (labels + note for the next submission) -------------
//
// Plain label NAMES and one short note. The backend owns the canonical label
// registry; the extension only ever requests a name.

async function getOperatorMetadata() {
  const data = await chrome.storage.local.get(CONTACT_STORAGE.OPERATOR_METADATA);
  const stored = data[CONTACT_STORAGE.OPERATOR_METADATA] || {};
  return contactSchema.operatorMetadata(stored);
}

async function setOperatorMetadata(patch) {
  const current = await getOperatorMetadata();
  const next = contactSchema.operatorMetadata({
    labels: patch && patch.labels !== undefined ? patch.labels : current.labels,
    note: patch && patch.note !== undefined ? patch.note : current.note,
  });
  await chrome.storage.local.set({ [CONTACT_STORAGE.OPERATOR_METADATA]: next });
  return next;
}

async function clearOperatorMetadata() {
  await chrome.storage.local.remove(CONTACT_STORAGE.OPERATOR_METADATA);
  return contactSchema.operatorMetadata(null);
}

// Campaign choice is a durable filing preference, not part of the Contact
// draft. Clearing labels/notes or recapturing a page therefore does not erase it.
function cleanCampaignId(value) {
  const text = typeof value === "string" ? value.trim().toLowerCase() : "";
  return /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/.test(
    text
  )
    ? text
    : null;
}

async function getFilingContext() {
  const data = await chrome.storage.local.get(CONTACT_STORAGE.FILING_CONTEXT);
  const stored = data[CONTACT_STORAGE.FILING_CONTEXT] || {};
  return { campaignId: cleanCampaignId(stored.campaignId) };
}

async function setFilingContext(value) {
  const next = { campaignId: cleanCampaignId(value && value.campaignId) };
  await chrome.storage.local.set({ [CONTACT_STORAGE.FILING_CONTEXT]: next });
  return next;
}

/** Remember the labels just used so the operator can reapply them next time. */
async function rememberLabels(labels) {
  if (!labels || !labels.length) return;
  const prefs = await getPrefs();
  const merged = contactSchema.sanitizeLabels([...labels, ...(prefs.recentLabels || [])]);
  await setPrefs({ recentLabels: merged.slice(0, LIMITS.MAX_LABELS) });
}

// ---- contact-first submission ----------------------------------------------

/**
 * Build the contact-first submission for the reviewed results batch. Only the
 * rows the operator left included are ever sent; excluded rows never leave the
 * browser.
 */
async function buildBatchSubmission() {
  const batch = await ensureBatch();
  const records = includedRecords(batch);
  const metadata = await getOperatorMetadata();
  const filing = await getFilingContext();
  const submissionId = batch.clientSubmissionId || contactSchema.newId();
  if (!batch.clientSubmissionId) {
    batch.clientSubmissionId = submissionId;
    await setBatch(batch);
  }
  const contacts = records.map((rec) =>
    contactSchema.buildResultRowCapture({
      record: rec,
      clientCaptureId: rec._captureId,
      capturedAt: batch.createdAt,
      sourceSearchUrl: batch.lastSearchUrl,
      adapterVersion: "salesnav-people-results-adapter/1",
      metadata: null,
    })
  );
  const payload = contactSchema.buildSubmission({
    clientSubmissionId: submissionId,
    captureMode: CAPTURE_MODES.SALESNAV_PEOPLE_SEARCH,
    submittedAt: batch.createdAt,
    extensionVersion: EXTENSION_VERSION,
    metadata,
    campaignId: filing.campaignId,
    contacts,
  });
  return { batch, payload, records, metadata };
}

function isAllowedBackendOrigin(urlStr) {
  try {
    const u = new URL(urlStr);
    return ALLOWED_BACKEND_ORIGIN_PATTERNS.some((re) => re.test(u.origin));
  } catch (_e) {
    return false;
  }
}

/**
 * Whether the loopback host permission for `url` has already been granted.
 * The worker never *requests* (no user gesture here) — the side panel requests
 * before sending. This is a defensive gate so a send fails clearly if the
 * optional permission was declined or revoked.
 */
async function hasHostPermission(url) {
  const pattern = permissions.originPatternForUrl(url);
  if (!pattern) return { ok: false, pattern: null };
  try {
    const granted = await chrome.permissions.contains({ origins: [pattern] });
    return { ok: granted, pattern };
  } catch (_e) {
    return { ok: false, pattern };
  }
}

/** Resolve the POST target for the contact-first submission. */
async function contactCaptureUrl(explicitTarget) {
  const prefs = await getPrefs();
  const target = explicitTarget || prefs.sendTarget || "mock";
  if (target === "mock") return { target, url: prefs.mockReceiverUrl };
  const base = (prefs.backendBaseUrl || "").replace(/\/$/, "");
  return { target, url: base + CONTACT_CAPTURE_PATH };
}

/**
 * POST one reviewed contact-first submission. Shared by both workflows so the
 * validation, loopback, permission, timeout, and idempotency behaviour cannot
 * drift between saving one profile and saving a page of results.
 */
async function postSubmission(payload, explicitTarget) {
  if (!payload.contacts.length) {
    return { ok: false, error: "empty_batch", message: "No included contacts to save." };
  }
  const validation = contactSchema.validateSubmission(payload);
  if (!validation.valid) {
    return { ok: false, error: "invalid_payload", messages: validation.errors };
  }
  const serialized = contactSchema.serializePayload(payload);
  if (!serialized.withinLimit) {
    return {
      ok: false,
      error: "payload_too_large",
      message: `Payload ${serialized.bytes} bytes exceeds ${LIMITS.MAX_PAYLOAD_BYTES}. Save fewer contacts.`,
    };
  }

  const { target, url } = await contactCaptureUrl(explicitTarget);
  if (!isAllowedBackendOrigin(url)) {
    return {
      ok: false,
      error: "origin_not_allowed",
      message: `Refusing to send to ${url}. Only loopback and approved VMR origins are permitted.`,
    };
  }
  const perm = await hasHostPermission(url);
  if (!perm.ok) {
    return {
      ok: false,
      error: "permission_denied",
      originPattern: perm.pattern,
      message: `Access not granted for ${perm.pattern || url}. Approve the permission prompt, then save again.`,
    };
  }
  const auth = await requestHeaders(url, {
    "Content-Type": "application/json",
    "Idempotency-Key": payload.client_submission_id,
  });
  if (!auth.ok) return { ok: false, error: auth.error, message: auth.message };

  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), CONTACT_CAPTURE_TIMEOUT_MS);
  try {
    const resp = await fetch(url, {
      method: "POST",
      headers: auth.headers,
      body: serialized.json,
      // The capture credential is the only thing that authorises this request.
      // Omitting ambient cookies keeps that true even if the operator is signed
      // in to the same hosted deployment in this browser.
      credentials: "omit",
      signal: controller.signal,
    });
    clearTimeout(timer);
    const text = await resp.text();
    let body = null;
    try { body = text ? JSON.parse(text) : null; } catch (_e) { body = { raw: text }; }
    if (!resp.ok) {
      // The reviewed draft is preserved so a recoverable failure can be retried
      // with the SAME client_submission_id (idempotent).
      return { ok: false, error: "receiver_rejected", status: resp.status, body };
    }
    const result = handoff.sanitizeContactSubmissionResult(body, {
      submittedAt: new Date().toISOString(),
    });
    return { ok: true, status: resp.status, target, url, result };
  } catch (e) {
    clearTimeout(timer);
    if (e && e.name === "AbortError") {
      return {
        ok: false,
        error: "timeout",
        message: `No response within ${CONTACT_CAPTURE_TIMEOUT_MS}ms.`,
      };
    }
    return { ok: false, error: "network_error", message: String(e && e.message) };
  }
}

/** Save the included rows of the reviewed results batch as contacts. */
async function saveIncludedContacts(explicitTarget) {
  const { payload, metadata } = await buildBatchSubmission();
  const response = await postSubmission(payload, explicitTarget);
  if (response.ok) {
    await setLastResult(response.result, LISTINGS_CONTEXT);
    await rememberLabels(metadata.labels);
    // The panel paints this outcome now and may restore it later. Both paths
    // are handed the same context, so a live outcome and a restored one are
    // placed by identical rules.
    response.resultContext = LISTINGS_CONTEXT;
  }
  return response;
}

// ---- label registry + save-vs-refresh lookup --------------------------------

/** Fetch existing labels so the operator can reuse one. Read-only. */
async function fetchLabels() {
  const prefs = await getPrefs();
  const base = (prefs.backendBaseUrl || "").replace(/\/$/, "");
  const url = base + CONTACT_LABELS_PATH;
  if (!isAllowedBackendOrigin(url)) return { ok: false, error: "origin_not_allowed" };
  const perm = await hasHostPermission(url);
  if (!perm.ok) return { ok: false, error: "permission_denied", originPattern: perm.pattern };
  const auth = await requestHeaders(url);
  if (!auth.ok) return { ok: false, error: auth.error };
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), SEND_TIMEOUT_MS);
  try {
    const resp = await fetch(url, {
      headers: auth.headers,
      credentials: "omit",
      signal: controller.signal,
    });
    clearTimeout(timer);
    if (!resp.ok) return { ok: false, error: "http_" + resp.status };
    const body = await resp.json();
    const labels = (Array.isArray(body) ? body : body.labels || [])
      .map((l) => String(l && l.name ? l.name : ""))
      .filter(Boolean);
    return { ok: true, labels: contactSchema.sanitizeLabels(labels) };
  } catch (e) {
    clearTimeout(timer);
    return { ok: false, error: e && e.name === "AbortError" ? "timeout" : "network_error" };
  }
}

/**
 * Ask the backend whether it is reachable, right now.
 *
 * The panel's connection badge used to be written only as a side effect of a
 * save, which made it a record of the last save rather than a statement about the
 * backend. A failed save latched it to "Not connected" and nothing could clear it
 * — so the badge stayed wrong after the backend came back, on the one screen
 * where "is it reachable?" is the question actually being asked.
 *
 * This reuses the label endpoint rather than adding a health route: it is
 * read-only, loopback-only, already permitted for this origin, and already proven
 * by the labels fetch, so a probe cannot succeed where a real request would fail.
 * Distinguishing the failure kinds matters — a denied optional permission is not
 * an unreachable backend, and telling an operator to start a server that is
 * already running wastes their time.
 */
async function probeBackend() {
  const result = await fetchLabels();
  if (result.ok) return { ok: true, state: "connected" };
  if (result.error === "permission_denied") {
    return { ok: false, state: "not_allowed", originPattern: result.originPattern };
  }
  if (result.error === "origin_not_allowed") {
    return { ok: false, state: "not_allowed" };
  }
  if (result.error === "credential_missing") {
    // A hosted deployment that is running perfectly and simply has not been
    // given a credential yet. Reporting that as "unreachable" would send the
    // operator to check a server that is fine.
    return { ok: false, state: "credential_required" };
  }
  // Anything else — a timeout, a refused connection, an HTTP error — means the
  // backend did not usefully answer.
  return { ok: false, state: "unreachable", error: result.error };
}

/** Fetch active/draft Campaigns for the optional filing selector. */
async function fetchCampaigns() {
  const prefs = await getPrefs();
  const base = (prefs.backendBaseUrl || "").replace(/\/$/, "");
  const url = base + CAMPAIGNS_PATH;
  if (!isAllowedBackendOrigin(url)) return { ok: false, error: "origin_not_allowed" };
  const perm = await hasHostPermission(url);
  if (!perm.ok) return { ok: false, error: "permission_denied", originPattern: perm.pattern };
  const auth = await requestHeaders(url);
  if (!auth.ok) return { ok: false, error: auth.error };
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), SEND_TIMEOUT_MS);
  try {
    const resp = await fetch(url, {
      headers: auth.headers,
      credentials: "omit",
      signal: controller.signal,
    });
    clearTimeout(timer);
    if (!resp.ok) return { ok: false, error: "http_" + resp.status };
    const body = await resp.json();
    const rows = Array.isArray(body) ? body : body.campaigns || [];
    const campaigns = rows
      .map((row) => ({
        id: cleanCampaignId(row && row.id),
        name: typeof (row && row.name) === "string" ? row.name.trim().slice(0, 255) : "",
        status: typeof (row && row.status) === "string" ? row.status : null,
      }))
      .filter((row) => row.id && row.name);
    return { ok: true, campaigns };
  } catch (e) {
    clearTimeout(timer);
    return { ok: false, error: e && e.name === "AbortError" ? "timeout" : "network_error" };
  }
}

/**
 * Ask whether an exact normalized profile URL already has a contact, so the
 * panel can label its primary action Save or Refresh. The backend returns
 * existence only — no contact field is ever fetched into the browser. A failure
 * is not an error the operator must act on: the panel simply says "Save".
 */
async function lookupContact(profileUrl) {
  if (!profileUrl) return { ok: true, match: "unknown" };
  const prefs = await getPrefs();
  const base = (prefs.backendBaseUrl || "").replace(/\/$/, "");
  const url = base + CONTACT_LOOKUP_PATH + "?linkedin_profile_url=" + encodeURIComponent(profileUrl);
  if (!isAllowedBackendOrigin(url)) return { ok: false, error: "origin_not_allowed" };
  const perm = await hasHostPermission(url);
  if (!perm.ok) return { ok: false, error: "permission_denied", originPattern: perm.pattern };
  const auth = await requestHeaders(url);
  if (!auth.ok) return { ok: false, error: auth.error };
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), SEND_TIMEOUT_MS);
  try {
    const resp = await fetch(url, {
      headers: auth.headers,
      credentials: "omit",
      signal: controller.signal,
    });
    clearTimeout(timer);
    if (!resp.ok) return { ok: false, error: "http_" + resp.status };
    const body = await resp.json();
    const match = typeof body.match === "string" ? body.match : "unknown";
    return { ok: true, match, contactCount: Number(body.contact_count) || 0 };
  } catch (e) {
    clearTimeout(timer);
    return { ok: false, error: e && e.name === "AbortError" ? "timeout" : "network_error" };
  }
}

// ---- person-profile capture mode (DAT-012C) --------------------------------
//
// A profile draft is ONE reviewed capture (not a batch). It lives in its own
// storage keys so the results-page workflow is never affected. Nothing is sent
// without an explicit SAVE_CONTACT from the operator.

async function getProfileDraft() {
  const data = await chrome.storage.local.get(PROFILE_STORAGE.DRAFT_PROFILE);
  return data[PROFILE_STORAGE.DRAFT_PROFILE] || null;
}
async function setProfileDraft(draft) {
  await chrome.storage.local.set({ [PROFILE_STORAGE.DRAFT_PROFILE]: draft });
  return draft;
}
async function getLastProfileResult() {
  const data = await chrome.storage.local.get(PROFILE_STORAGE.LAST_PROFILE_RESULT);
  return readRetainedRecord(data[PROFILE_STORAGE.LAST_PROFILE_RESULT]);
}
async function setLastProfileResult(result, context) {
  await chrome.storage.local.set({
    [PROFILE_STORAGE.LAST_PROFILE_RESULT]: retainedRecord(result, context),
  });
  return result;
}

/** The person a reviewed profile draft describes, as a capture context. */
function profileDraftContext(draft) {
  const url =
    draft && draft.extraction && draft.extraction.profile
      ? draft.extraction.profile.linkedin_profile_url
      : null;
  return pageContext("profile", url);
}

async function findActiveTab() {
  const tabs = await chrome.tabs.query({ active: true, lastFocusedWindow: true });
  return (tabs && tabs[0]) || null;
}

/** Classify the ACTIVE tab into a side-panel mode (URL-level; DOM checks refine). */
async function detectActiveSurface() {
  const tab = await findActiveTab();
  if (!tab || !tab.url) return { ok: true, surface: SURFACES.UNSUPPORTED, url: null };
  const detected = surface.detectSurface(tab.url, null);
  return { ok: true, surface: detected.surface, reason: detected.reason, url: tab.url };
}

const PROFILE_CS_FILES = [
  "src/common/constants.js",
  "src/common/normalize.js",
  "src/common/extraction.js",
  "src/common/surface.js",
  "src/common/profile-extraction.js",
  "src/content/profile-content-script.js",
];

async function askProfileContentScript(message) {
  const tab = await findActiveTab();
  if (!tab || !/^https:\/\/www\.linkedin\.com\/in\//.test(tab.url || "")) {
    return {
      ok: false,
      error: "no_profile_tab",
      message: "Open a LinkedIn profile (linkedin.com/in/…) in the active tab.",
    };
  }
  try {
    const resp = await chrome.tabs.sendMessage(tab.id, message);
    return { ok: true, tab, resp };
  } catch (e) {
    try {
      await chrome.scripting.executeScript({ target: { tabId: tab.id }, files: PROFILE_CS_FILES });
      const resp = await chrome.tabs.sendMessage(tab.id, message);
      return { ok: true, tab, resp };
    } catch (e2) {
      return { ok: false, error: "content_script_unavailable", detail: String(e2 && e2.message) };
    }
  }
}

async function profileDetect() {
  const r = await askProfileContentScript({ type: "PS_DETECT" });
  if (!r.ok) return { ok: false, error: r.error, message: r.message };
  return { ok: true, page: r.resp };
}

/** Build a small, reviewable summary of a draft for the side panel. */
function buildProfileDraftView(draft) {
  if (!draft) return null;
  const ex = draft.extraction;
  const current = (ex.experiences || []).filter((e) => e.is_current === true);
  return {
    clientCaptureId: draft.clientCaptureId,
    capturedAt: ex.capturedAt,
    status: ex.status,
    profile: ex.profile,
    experiences: ex.experiences || [],
    experienceCount: (ex.experiences || []).length,
    currentRoles: current.map((e) => ({
      job_title: e.job_title,
      company_name: e.company_name,
    })),
    missingSections: ex.missingSections || [],
    pageWarnings: ex.pageWarnings || [],
    excludedSections: draft.excludedSections || [],
  };
}

/**
 * Read the person profile in the active tab into the reviewed draft.
 *
 * `live` is true for the side panel's automatic preview and false (the default)
 * for a read the operator asked for. Both produce the same draft; they differ
 * only in what they are allowed to discard. Neither sends anything.
 */
async function profileCapture(live) {
  const r = await askProfileContentScript({ type: "PS_CAPTURE" });
  if (!r.ok) return { ok: false, error: r.error, message: r.message };
  const result = r.resp;

  // Only ok/partial results become a reviewable draft. Challenge, unavailable,
  // unsupported, and unrecognized-structure results are surfaced and NOT stored.
  if (result.status !== CAPTURE_STATUS.OK && result.status !== CAPTURE_STATUS.PARTIAL) {
    return {
      ok: true,
      captureStatus: result.status,
      pageWarnings: result.pageWarnings || [],
      draftView: buildProfileDraftView(await getProfileDraft()),
    };
  }

  // A NEW capture replaces the draft and mints a fresh client_capture_id (a
  // changed reviewed content must never reuse the previous idempotency key).
  const draft = {
    clientCaptureId: contactSchema.newId(),
    clientSubmissionId: null,
    createdAt: new Date().toISOString(),
    excludedSections: [],
    pageTitle: r.tab && r.tab.title ? r.tab.title : null,
    extraction: result,
  };
  await setProfileDraft(draft);
  // A read the operator asked for is new unsent work, and the result of the
  // previous read no longer describes it. The panel's own live preview is not
  // that: it runs by itself whenever the panel opens, so treating it as a
  // recapture would discard the saved outcome the operator came back for
  // (UI-016).
  if (!live) await chrome.storage.local.remove(PROFILE_STORAGE.LAST_PROFILE_RESULT);
  return {
    ok: true,
    captureStatus: result.status,
    pageWarnings: result.pageWarnings || [],
    draftView: buildProfileDraftView(draft),
  };
}

async function profileToggleSection(section) {
  const draft = await getProfileDraft();
  if (!draft) return { ok: false, error: "no_draft" };
  const set = new Set(draft.excludedSections || []);
  if (set.has(section)) set.delete(section);
  else set.add(section);
  draft.excludedSections = Array.from(set);
  draft.clientSubmissionId = null;
  await setProfileDraft(draft);
  return { ok: true, draftView: buildProfileDraftView(draft) };
}

async function profileClear() {
  await chrome.storage.local.remove(PROFILE_STORAGE.DRAFT_PROFILE);
  await chrome.storage.local.remove(PROFILE_STORAGE.LAST_PROFILE_RESULT);
  return { ok: true, draftView: null };
}

/**
 * Build the contact-first submission for the one reviewed profile draft. The
 * draft carries the person; the submission carries operator metadata and the
 * independent optional Campaign filing choice.
 */
async function buildProfileSubmission() {
  const draft = await getProfileDraft();
  if (!draft) return { draft: null, payload: null, metadata: null };
  const metadata = await getOperatorMetadata();
  const filing = await getFilingContext();
  const submissionId = draft.clientSubmissionId || contactSchema.newId();
  if (!draft.clientSubmissionId) {
    draft.clientSubmissionId = submissionId;
    await setProfileDraft(draft);
  }
  const capture = contactSchema.buildProfileCapture({
    extraction: draft.extraction,
    clientCaptureId: draft.clientCaptureId,
    excludedSections: draft.excludedSections || [],
    pageTitle: draft.pageTitle || null,
    metadata: null,
  });
  const payload = contactSchema.buildSubmission({
    clientSubmissionId: submissionId,
    captureMode: CAPTURE_MODES.LINKEDIN_PROFILE,
    submittedAt: draft.createdAt,
    extensionVersion: EXTENSION_VERSION,
    metadata,
    campaignId: filing.campaignId,
    contacts: [capture],
  });
  return { draft, payload, metadata };
}

/** Save (or refresh) the reviewed profile as a permanent contact capture. */
async function saveProfileContact(explicitTarget) {
  const { draft, payload, metadata } = await buildProfileSubmission();
  if (!draft) {
    return { ok: false, error: "empty_batch", message: "No reviewed capture to save." };
  }
  const response = await postSubmission(payload, explicitTarget);
  if (response.ok) {
    const context = profileDraftContext(draft);
    await setLastProfileResult(response.result, context);
    await rememberLabels(metadata.labels);
    response.resultContext = context;
  }
  return response;
}

/**
 * Whether the captured person already exists, so the panel can offer "Refresh
 * Contact" instead of "Save Contact". Purely advisory: an unavailable backend
 * simply leaves the action as Save.
 */
async function profileMatchState() {
  const draft = await getProfileDraft();
  const url = draft && draft.extraction && draft.extraction.profile
    ? draft.extraction.profile.linkedin_profile_url
    : null;
  if (!url) return { ok: true, match: "unknown" };
  return lookupContact(url);
}

// ---- company capture mode (DAT-012G) ---------------------------------------
//
// Mirrors the person-profile mode with its own storage keys. The operator
// opens the company page manually; the extension NEVER navigates there from a
// person profile. Nothing is sent without an explicit COMPANY_SEND.

async function getCompanyDraft() {
  const data = await chrome.storage.local.get(PROFILE_STORAGE.DRAFT_COMPANY);
  return data[PROFILE_STORAGE.DRAFT_COMPANY] || null;
}
async function setCompanyDraft(draft) {
  await chrome.storage.local.set({ [PROFILE_STORAGE.DRAFT_COMPANY]: draft });
  return draft;
}
async function getLastCompanyResult() {
  const data = await chrome.storage.local.get(PROFILE_STORAGE.LAST_COMPANY_RESULT);
  return readRetainedRecord(data[PROFILE_STORAGE.LAST_COMPANY_RESULT]);
}

/** The company a reviewed company draft describes, as a capture context. */
function companyDraftContext(draft) {
  const url =
    draft && draft.extraction && draft.extraction.company
      ? draft.extraction.company.company_linkedin_url
      : null;
  return pageContext("company", url);
}

const COMPANY_CS_FILES = [
  "src/common/constants.js",
  "src/common/normalize.js",
  "src/common/extraction.js",
  "src/common/surface.js",
  "src/common/company-extraction.js",
  "src/content/company-content-script.js",
];

async function askCompanyContentScript(message) {
  const tab = await findActiveTab();
  if (!tab || !/^https:\/\/www\.linkedin\.com\/company\//.test(tab.url || "")) {
    return {
      ok: false,
      error: "no_company_tab",
      message: "Open a LinkedIn company page (linkedin.com/company/…) in the active tab.",
    };
  }
  try {
    const resp = await chrome.tabs.sendMessage(tab.id, message);
    return { ok: true, tab, resp };
  } catch (e) {
    try {
      await chrome.scripting.executeScript({ target: { tabId: tab.id }, files: COMPANY_CS_FILES });
      const resp = await chrome.tabs.sendMessage(tab.id, message);
      return { ok: true, tab, resp };
    } catch (e2) {
      return { ok: false, error: "content_script_unavailable", detail: String(e2 && e2.message) };
    }
  }
}

function buildCompanyDraftView(draft) {
  if (!draft) return null;
  const ex = draft.extraction;
  return {
    clientCaptureId: draft.clientCaptureId,
    capturedAt: ex.capturedAt,
    status: ex.status,
    company: ex.company,
    missingSections: ex.missingSections || [],
    pageWarnings: ex.pageWarnings || [],
  };
}

async function companyCapture() {
  const r = await askCompanyContentScript({ type: "CO_CAPTURE" });
  if (!r.ok) return { ok: false, error: r.error, message: r.message };
  const result = r.resp;
  if (result.status !== CAPTURE_STATUS.OK && result.status !== CAPTURE_STATUS.PARTIAL) {
    return {
      ok: true,
      captureStatus: result.status,
      pageWarnings: result.pageWarnings || [],
      draftView: buildCompanyDraftView(await getCompanyDraft()),
    };
  }
  const draft = {
    clientCaptureId: profileSchema.newCaptureId(),
    createdAt: new Date().toISOString(),
    extraction: result,
  };
  await setCompanyDraft(draft);
  await chrome.storage.local.remove(PROFILE_STORAGE.LAST_COMPANY_RESULT);
  return {
    ok: true,
    captureStatus: result.status,
    pageWarnings: result.pageWarnings || [],
    draftView: buildCompanyDraftView(draft),
  };
}

async function companyClear() {
  await chrome.storage.local.remove(PROFILE_STORAGE.DRAFT_COMPANY);
  await chrome.storage.local.remove(PROFILE_STORAGE.LAST_COMPANY_RESULT);
  return { ok: true, draftView: null };
}

async function companySend() {
  const draft = await getCompanyDraft();
  if (!draft) return { ok: false, error: "empty_batch", message: "No reviewed capture to send." };
  const prefs = await getPrefs();

  // The target is checked before the payload is built, because an unsupported
  // target is not a fixable problem with the capture: telling the operator
  // their draft failed validation when the real answer is "this backend does
  // not accept company evidence" sends them to correct the wrong thing.
  const companyBase = (prefs.backendBaseUrl || "").replace(/\/$/, "");
  const companyUrl = companyBase + COMPANY_INTAKE_PATH;
  if (permissions.isHostedUrl(companyUrl)) {
    // Company evidence is a separate surface from contact capture and is not in
    // the hosted intake contract the capture credential authorises, so a hosted
    // company send would be refused by the backend. Saying so here is more
    // useful than relaying a 401 the operator cannot act on.
    return {
      ok: false,
      error: "company_capture_local_only",
      message:
        "Company evidence capture is available against a local VMR backend only. " +
        "Contact capture works against hosted VMR.",
    };
  }

  const payload = profileSchema.buildCompanyPayload({
    extraction: draft.extraction,
    clientCaptureId: draft.clientCaptureId,
    // Company evidence is firmographic context for a company record. It has
    // never belonged to a campaign and now cannot: the field stays null.
    campaignId: null,
    extensionVersion: EXTENSION_VERSION,
  });
  const validation = profileSchema.validateCompanyPayload(payload);
  if (!validation.valid) {
    return { ok: false, error: "invalid_payload", messages: validation.errors };
  }
  const serialized = profileSchema.serializePayload(payload);
  if (!serialized.withinLimit) return { ok: false, error: "payload_too_large" };

  if (!isAllowedBackendOrigin(companyUrl)) return { ok: false, error: "origin_not_allowed" };
  const perm = await hasHostPermission(companyUrl);
  if (!perm.ok) return { ok: false, error: "permission_denied", originPattern: perm.pattern };

  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), SEND_TIMEOUT_MS);
  try {
    const resp = await fetch(companyUrl, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "Idempotency-Key": payload.client_capture_id,
      },
      body: serialized.json,
      credentials: "omit",
      signal: controller.signal,
    });
    clearTimeout(timer);
    const text = await resp.text();
    let body = null;
    try { body = text ? JSON.parse(text) : null; } catch (_e) { body = { raw: text }; }
    if (!resp.ok) return { ok: false, error: "receiver_rejected", status: resp.status, body };
    const result = handoff.sanitizeProfileStageResult(body, {
      stagedAt: new Date().toISOString(),
    });
    const context = companyDraftContext(draft);
    await chrome.storage.local.set({
      [PROFILE_STORAGE.LAST_COMPANY_RESULT]: retainedRecord(result, context),
    });
    return { ok: true, status: resp.status, result, resultContext: context };
  } catch (e) {
    clearTimeout(timer);
    if (e && e.name === "AbortError") {
      return { ok: false, error: "timeout", message: `No response within ${SEND_TIMEOUT_MS}ms.` };
    }
    return { ok: false, error: "network_error", message: String(e && e.message) };
  }
}

// ---- downloads ------------------------------------------------------------

function sanitizeFilename(name) {
  return String(name)
    .replace(/[^a-zA-Z0-9._-]+/g, "_")
    .replace(/_{2,}/g, "_")
    .replace(/^[_.]+|[_.]+$/g, "")
    .slice(0, 120) || "batch";
}

function dataUrl(mime, text) {
  return `data:${mime};charset=utf-8,` + encodeURIComponent(text);
}

/**
 * Offline fallback: download the reviewed submission the operator would have
 * saved. JSON is the exact contact-first body (so it can be inspected or
 * replayed by hand); CSV is a flat review sheet of the included rows.
 */
async function exportBatch(format) {
  const { payload, batch, records } = await buildBatchSubmission();
  const stamp = batch.createdAt.replace(/[:.]/g, "-");
  const base = sanitizeFilename(`contact_capture_${stamp}`);
  let mime, text, ext;
  if (format === "csv") {
    mime = "text/csv";
    text = schema.toCsv(records);
    ext = "csv";
  } else {
    mime = "application/json";
    text = JSON.stringify(payload, null, 2);
    ext = "json";
  }
  const filename = `${base}.${ext}`;
  await chrome.downloads.download({
    url: dataUrl(mime, text),
    filename,
    saveAs: true,
  });
  return { ok: true, filename, records: payload.contacts.length };
}

/** Download the archived campaign-era drafts so nothing is lost on migration. */
async function exportLegacyArchive() {
  const data = await chrome.storage.local.get(CONTACT_STORAGE.LEGACY_ARCHIVE);
  const archive = data[CONTACT_STORAGE.LEGACY_ARCHIVE];
  if (!archive) return { ok: false, error: "no_legacy_archive" };
  const filename = sanitizeFilename("vmr_legacy_capture_drafts") + ".json";
  await chrome.downloads.download({
    url: dataUrl("application/json", JSON.stringify(archive, null, 2)),
    filename,
    saveAs: true,
  });
  return { ok: true, filename };
}

async function getMigrationNotice() {
  const data = await chrome.storage.local.get([
    CONTACT_STORAGE.MIGRATION_NOTICE,
    CONTACT_STORAGE.LEGACY_ARCHIVE,
  ]);
  return {
    notice: data[CONTACT_STORAGE.MIGRATION_NOTICE] || null,
    hasArchive: !!data[CONTACT_STORAGE.LEGACY_ARCHIVE],
  };
}

/**
 * Discard the campaign-era archive (DAT-018 C).
 *
 * The archive card is shown only while an archive exists, so hiding it without
 * clearing the archive would make it reappear on the next panel load. Discard
 * therefore removes the archive itself, which is what the button says it does.
 * This is destructive and irreversible, so it is an explicit operator action
 * and the panel offers Download first.
 */
async function discardLegacyArchive() {
  await chrome.storage.local.remove([
    CONTACT_STORAGE.MIGRATION_NOTICE,
    CONTACT_STORAGE.LEGACY_ARCHIVE,
  ]);
  return { ok: true };
}

async function dismissMigrationNotice() {
  await chrome.storage.local.remove(CONTACT_STORAGE.MIGRATION_NOTICE);
  return { ok: true };
}

// ---- message router -------------------------------------------------------

chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  (async () => {
    switch (msg && msg.type) {
      case "GET_STATE": {
        const batch = await ensureBatch();
        const prefs = await getPrefs();
        const retained = await getLastResult();
        sendResponse({
          ok: true,
          prefs,
          batchView: buildBatchView(batch),
          lastResult: retained.result,
          lastResultContext: retained.context,
          metadata: await getOperatorMetadata(),
          filingContext: await getFilingContext(),
          migration: await getMigrationNotice(),
          credential: await credentialState(),
        });
        break;
      }
      case "DETECT_ACTIVE_PAGE":
        sendResponse(await detectActivePage());
        break;
      case "CAPTURE_ACTIVE_PAGE":
        sendResponse(await captureActivePage());
        break;
      case "CANCEL_CAPTURE":
        sendResponse(await cancelActiveCapture());
        break;
      case "SET_PREFS":
        sendResponse({ ok: true, prefs: await setPrefs(msg.prefs) });
        break;
      case "TOGGLE_EXCLUDE":
        sendResponse({ ok: true, batchView: await toggleExclude(msg.stableKey, msg.index) });
        break;
      case "CLEAR_BATCH":
        sendResponse({ ok: true, batchView: await clearBatch() });
        break;
      case "PREVIEW_PAYLOAD": {
        const { payload } = await buildBatchSubmission();
        const validation = contactSchema.validateSubmission(payload);
        const serialized = contactSchema.serializePayload(payload);
        sendResponse({ ok: true, payload, validation, bytes: serialized.bytes });
        break;
      }
      case "SAVE_INCLUDED_CONTACTS":
        sendResponse(await saveIncludedContacts(msg.target));
        break;
      case "GET_OPERATOR_METADATA":
        sendResponse({ ok: true, metadata: await getOperatorMetadata() });
        break;
      case "SET_OPERATOR_METADATA":
        sendResponse({ ok: true, metadata: await setOperatorMetadata(msg.metadata) });
        break;
      case "CLEAR_OPERATOR_METADATA":
        sendResponse({ ok: true, metadata: await clearOperatorMetadata() });
        break;
      case "GET_FILING_CONTEXT":
        sendResponse({ ok: true, filingContext: await getFilingContext() });
        break;
      case "SET_FILING_CONTEXT":
        sendResponse({
          ok: true,
          filingContext: await setFilingContext(msg.filingContext),
        });
        break;
      // The credential is write-only from the panel's side: it can be set,
      // cleared, and asked about, but never read back.
      case "GET_CREDENTIAL_STATE":
        sendResponse(await credentialState());
        break;
      case "SET_CAPTURE_CREDENTIAL":
        sendResponse(await setCredential(msg.credential));
        break;
      case "CLEAR_CAPTURE_CREDENTIAL":
        sendResponse(await clearCredential());
        break;
      case "FETCH_LABELS":
        sendResponse(await fetchLabels());
        break;
      case "PROBE_BACKEND":
        sendResponse(await probeBackend());
        break;
      case "FETCH_CAMPAIGNS":
        sendResponse(await fetchCampaigns());
        break;
      case "EXPORT_LEGACY_ARCHIVE":
        sendResponse(await exportLegacyArchive());
        break;
      case "DISMISS_MIGRATION_NOTICE":
        sendResponse(await dismissMigrationNotice());
        break;
      case "DISCARD_LEGACY_ARCHIVE":
        sendResponse(await discardLegacyArchive());
        break;
      case "DETECT_SURFACE":
        sendResponse(await detectActiveSurface());
        break;
      case "PROFILE_GET_STATE": {
        const draft = await getProfileDraft();
        const retained = await getLastProfileResult();
        sendResponse({
          ok: true,
          prefs: await getPrefs(),
          draftView: buildProfileDraftView(draft),
          lastResult: retained.result,
          lastResultContext: retained.context,
          metadata: await getOperatorMetadata(),
          filingContext: await getFilingContext(),
          migration: await getMigrationNotice(),
        });
        break;
      }
      case "PROFILE_MATCH_STATE":
        sendResponse(await profileMatchState());
        break;
      case "PROFILE_DETECT":
        sendResponse(await profileDetect());
        break;
      case "PROFILE_CAPTURE":
        sendResponse(await profileCapture(msg.live === true));
        break;
      case "PROFILE_TOGGLE_SECTION":
        sendResponse(await profileToggleSection(msg.section));
        break;
      case "PROFILE_CLEAR":
        sendResponse(await profileClear());
        break;
      case "SAVE_CONTACT":
        sendResponse(await saveProfileContact(msg.target));
        break;
      case "COMPANY_GET_STATE": {
        const retained = await getLastCompanyResult();
        sendResponse({
          ok: true,
          prefs: await getPrefs(),
          draftView: buildCompanyDraftView(await getCompanyDraft()),
          lastResult: retained.result,
          lastResultContext: retained.context,
        });
        break;
      }
      case "COMPANY_CAPTURE":
        sendResponse(await companyCapture());
        break;
      case "COMPANY_CLEAR":
        sendResponse(await companyClear());
        break;
      case "COMPANY_SEND":
        sendResponse(await companySend());
        break;
      case "EXPORT_BATCH":
        sendResponse(await exportBatch(msg.format));
        break;
      default:
        sendResponse({ ok: false, error: "unknown_message" });
    }
  })().catch((e) => sendResponse({ ok: false, error: "worker_exception", detail: String(e && e.message) }));
  return true; // async response
});
