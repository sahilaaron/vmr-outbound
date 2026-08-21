/**
 * Service worker: extension state hub + backend communication.
 *
 * Responsibilities:
 *  - Own the recoverable reviewed drafts in chrome.storage.local.
 *  - Relay capture/detect requests to the active tab's content script and merge
 *    result rows (dedupe) into the reviewed batch.
 *  - Turn one explicit operator Save into a DURABLE PUSH JOB and deliver it as
 *    a sequence of bounded, independently idempotent contact-capture requests.
 *  - Persist optional Campaign filing context independently from Contact capture.
 *  - Produce a local CSV/JSON export of the reviewed capture on operator action.
 *  - Clear superseded campaign-bound local state explicitly.
 *
 * ONE SAVE IS NOT ONE REQUEST. The reviewed set may hold up to
 * `LIMITS.MAX_RECORDS_PER_BATCH` people; a request carries at most
 * `LIMITS.MAX_CONTACTS_PER_SUBMISSION` of them and at most
 * `PUSH.CHUNK_MAX_BYTES`. The push is planned once, written down, and resumed
 * from storage — so it survives the side panel closing, the Sales Navigator tab
 * going away, and the Manifest V3 service worker being suspended mid-flight.
 * See `common/push-job.js` for why every chunk's idempotency key is minted once.
 *
 * The local export is the operator's own copy of what they reviewed. It is
 * produced entirely in the browser, works with the backend unreachable, and
 * never removes or alters the reviewed capture — downloading is not saving and
 * not clearing.
 *
 * Campaign filing is optional and additive: acquisition always saves the person
 * first. Never posts to LinkedIn. Nothing is ever sent without an explicit
 * operator-triggered message.
 *
 * Authorization for hosted capture is the operator's own VMR Outbound account
 * link (see `common/account-link.js`): a short-lived access token, refreshed
 * from a rotating refresh token, attached only to a request going to a named
 * hosted deployment and never returned to the panel, written to a log line, or
 * stored beside the drafts. Nobody types a credential. No LinkedIn credential,
 * cookie or session is ever read or kept.
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
  "../common/chunking.js",
  "../common/push-job.js",
  "../common/migration.js",
  "../common/permissions.js",
  "../common/account-link.js",
  "../common/handoff.js"
);

const {
  constants,
  dedupe,
  schema,
  profileSchema,
  contactSchema,
  chunking,
  pushJob,
  migration,
  normalize,
  permissions,
  accountLink: accountLinkModule,
  handoff,
  surface,
} = self.SNCapture;
const {
  STORAGE,
  PROFILE_STORAGE,
  CONTACT_STORAGE,
  PUSH_STORAGE,
  PUSH,
  EXPORT,
  ACCOUNT_STORAGE,
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
  // Chunk payloads left by a push that was replaced, cancelled or interrupted
  // before this build existed. They are captured personal data with no reader.
  sweepOrphanChunks();
  // An unfinished push is the operator's work, not this worker instance's. Every
  // way the worker can come back is a way the push must come back with it.
  resumePush();
});
if (chrome.runtime.onStartup) {
  chrome.runtime.onStartup.addListener(() => {
    migrateLegacyState();
    sweepOrphanChunks();
    resumePush();
  });
}

// The ONLY reason `alarms` is in the manifest.
//
// A Manifest V3 service worker is suspended when it goes idle, and an in-memory
// `await` loop is not background execution — it is a promise that stops existing.
// Every other wake-up this extension has (the panel opening, a message arriving,
// the browser starting) depends on somebody doing something. A push whose next
// chunk is waiting out a backoff needs a wake-up that depends on nobody, and a
// periodic alarm is the narrowest mechanism Chrome offers for it. It fires only
// while a push is unfinished and is cleared the moment one settles.
if (chrome.alarms && chrome.alarms.onAlarm) {
  chrome.alarms.onAlarm.addListener((alarm) => {
    if (alarm && alarm.name === PUSH.RESUME_ALARM) resumePush();
  });
}

// ---- account link -----------------------------------------------------------
//
// Hosted capture is authorised by the operator's own VMR Outbound account. The
// client lives in `common/account-link.js`; the worker owns the one instance and
// is the only thing that ever sees a token. The panel is told whether the
// install is linked and to which account — never a token, in any response.

const accountLink = accountLinkModule.createAccountLink({
  chrome,
  crypto,
  fetch: (...args) => fetch(...args),
  backendBaseUrl: async () => (await getPrefs()).backendBaseUrl,
});

/** Link state for the panel: connected, and to whom. Never a token. */
async function accountState() {
  return accountLink.state();
}

/**
 * Whether the optional host permission for the backend is already granted.
 *
 * The token exchange is a cross-origin `fetch`, so it needs that permission even
 * though `launchWebAuthFlow` does not. Checking first means an operator who has
 * not approved the origin yet is told THAT, instead of being walked through a
 * sign-in window whose exchange then fails for an unrelated reason. The worker
 * never requests the permission — there is no user gesture here; the panel
 * requests it with the click that started the sign-in.
 */
async function accountLinkPermission() {
  const prefs = await getPrefs();
  const base = String(prefs.backendBaseUrl || "").replace(/\/$/, "");
  if (!permissions.isHostedUrl(base + "/")) return { ok: true, pattern: null };
  return hasHostPermission(base + constants.ACCOUNT_LINK_PATHS.TOKEN);
}

/** Open-the-panel path: report the link, connecting silently if it can. */
async function ensureAccountConnected() {
  const perm = await accountLinkPermission();
  if (!perm.ok) {
    // No point opening even a silent auth window: the exchange behind it cannot
    // run yet. The panel asks for the permission on the operator's next click.
    return {
      ok: true,
      account: await accountState(),
      reason: "permission_required",
      originPattern: perm.pattern,
    };
  }
  const result = await accountLink.ensureConnected();
  return { ok: true, account: result.account, reason: result.reason || null };
}

/** The single "Sign in to VMR Outbound" action. */
async function connectAccount() {
  const perm = await accountLinkPermission();
  if (!perm.ok) {
    return {
      ok: false,
      error: "permission_denied",
      originPattern: perm.pattern,
      account: await accountState(),
    };
  }
  const result = await accountLink.connect({ interactive: true });
  const account = await accountState();
  if (!result.ok) return { ok: false, error: result.error, account };
  return { ok: true, account };
}

async function disconnectAccount() {
  await accountLink.disconnect();
  return { ok: true, account: await accountState() };
}

// ---- development overrides ---------------------------------------------------
//
// EXACTLY ONE THING enables them: an object at `chrome.storage.local`
// key `vmr_dev_overrides` with `enabled === true`.
//
// Nothing writes that key. No panel control, no message handler, no install or
// startup step — deliberately, so it can only be created by hand from the
// extension's own devtools console on an unpacked build. An ordinary
// staging/production operator has no path to it at all, which is what makes the
// legacy `vmrx1` credential and the backend/mock-target fields below unreachable
// for them rather than merely hidden.

async function devOverrides() {
  try {
    const data = await chrome.storage.local.get(ACCOUNT_STORAGE.DEV_OVERRIDES);
    const raw = data && data[ACCOUNT_STORAGE.DEV_OVERRIDES];
    return raw && typeof raw === "object" && raw.enabled === true ? raw : null;
  } catch (_e) {
    return null;
  }
}

async function devModeEnabled() {
  return (await devOverrides()) !== null;
}

// ---- legacy `vmrx1` capture credential (development compatibility only) ------
//
// Superseded by the account link. Retained only for the local/development
// compatibility the backend still honours under APP_ENV=local, and reachable
// only behind the development gate above: the ordinary panel has no control for
// it, the worker refuses to store one without the gate, and hosted capture never
// depends on it. Held in `chrome.storage.session` — in-memory for the browser
// session, never written to disk, unreadable from a content script — and it has
// exactly one exit: the `Authorization` header of a request to a named hosted
// deployment when no account link exists.

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
 * Store a pasted legacy credential after a shape check.
 *
 * Refused outright unless the development gate is present: on an ordinary
 * install there is no such thing as a credential to paste, and accepting one
 * would keep alive exactly the shared-secret path the account link replaced.
 *
 * The shape check refuses an obviously-wrong paste — a truncated copy, the
 * configuration digest pasted instead of the credential — at the field, so the
 * developer learns immediately rather than through a 401 three screens later.
 * The backend remains the only authority on whether a well-formed credential is
 * a real one.
 */
async function setCredential(value) {
  if (!(await devModeEnabled())) return { ok: false, error: "dev_mode_required" };
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
 * Whether a legacy credential is held — never the credential itself.
 *
 * This is the only thing the panel is ever told about it, which is what keeps
 * the value out of the panel's DOM, its state, and anything it might render.
 */
async function credentialState() {
  return {
    ok: true,
    hasCredential: (await getCredential()) !== null,
    storageAvailable: credentialStore() !== null,
    devMode: await devModeEnabled(),
  };
}

/**
 * The legacy credential, but only where it is still legitimate: behind the
 * development gate, and only when this install has no account link to use
 * instead. Ordinary hosted capture can never reach this.
 */
async function legacyDevCredential() {
  if (!(await devModeEnabled())) return null;
  return getCredential();
}

/**
 * Headers for one backend request, or the refusal to make it.
 *
 * A loopback target is unchanged and carries nothing: local development has no
 * authenticated intake and adding one would be a behaviour change nobody asked
 * for. A named hosted deployment carries the account-linked access token, minted
 * from the operator's own VMR Outbound session and refreshed silently when it is
 * close to expiry.
 *
 * With no usable link the request is refused here rather than sent to collect a
 * 401 — the operator gets one "Sign in to VMR Outbound" action, not a backend
 * rejection they cannot act on. Nothing leaves the browser on that path.
 */
async function requestHeaders(url, extra) {
  const headers = Object.assign({}, extra || {});
  if (!permissions.isHostedUrl(url)) return { ok: true, headers };

  const token = await accountLink.ensureAccessToken();
  if (token.ok) {
    headers["Authorization"] = "Bearer " + token.accessToken;
    return { ok: true, headers };
  }

  const legacy = await legacyDevCredential();
  if (legacy) {
    headers["Authorization"] = "Bearer " + legacy;
    return { ok: true, headers };
  }

  // A transport or server-side failure while refreshing is not "you are signed
  // out" — saying so would send the operator through a sign-in that cannot help.
  if (token.error === "timeout" || token.error === "network_error") {
    return {
      ok: false,
      error: token.error,
      message: "Could not reach VMR Outbound to authorise this capture.",
    };
  }
  if (token.error === "token_endpoint_error") {
    return {
      ok: false,
      error: "account_link_unavailable",
      message: "VMR Outbound could not authorise this capture just now. Try again shortly.",
    };
  }
  // A development install with the gate on, no account link and no legacy
  // credential is a developer who has configured neither; naming the credential
  // is the actionable answer for them. An ordinary install has exactly one way
  // forward, and it is a sign-in.
  if (await devModeEnabled()) {
    return {
      ok: false,
      error: "credential_missing",
      message:
        "No VMR Outbound account link and no development capture credential. " +
        "Sign in, or set a credential in the development overrides.",
    };
  }
  return {
    ok: false,
    error: "account_link_required",
    message: "Sign in to VMR Outbound to capture into your account.",
  };
}

// ---- storage helpers ------------------------------------------------------

async function getPrefs() {
  const data = await chrome.storage.local.get(STORAGE.PREFERENCES);
  return Object.assign({}, DEFAULT_PREFERENCES, data[STORAGE.PREFERENCES] || {});
}
// Which preferences describe WHERE captures go. They are product configuration,
// not operator preferences: the hosted deployment is chosen by the product, and
// the ordinary panel has no control that can change any of them. A patch that
// carries one is accepted only behind the development gate, so the send target
// cannot be moved even by a panel that has been tampered with.
const DEV_ONLY_PREFERENCES = ["backendBaseUrl", "sendTarget", "mockReceiverUrl"];

async function setPrefs(patch) {
  const prefs = await getPrefs();
  const requested = Object.assign({}, patch || {});
  if (!(await devModeEnabled())) {
    for (const key of DEV_ONLY_PREFERENCES) delete requested[key];
  }
  const next = Object.assign({}, prefs, requested);
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
  // staged batch is only meaningful while its reviewed source exists. The same
  // is true of a settled push — it describes rows that no longer exist here.
  // An UNFINISHED push is not reached: the caller refuses the clear first.
  await clearLastResult();
  await clearPushJob();
  // The ledger describes capture ids that existed only in the set just cleared.
  // Keeping it would grow for ever and could never match anything again.
  await chrome.storage.local.remove(PUSH_STORAGE.LEDGER);
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
async function buildBatchSubmission(options) {
  // `persist: false` is the EXPORT path. Building a submission normally pins the
  // reviewed set's submission id so a retry re-sends the same one; an export is
  // a local copy that sends nothing, and pinning an id it will never use would
  // make a download look like the start of a save in the stored state.
  const persist = !options || options.persist !== false;
  const batch = await ensureBatch();
  const records = includedRecords(batch);
  const metadata = await getOperatorMetadata();
  const filing = await getFilingContext();
  const submissionId = batch.clientSubmissionId || contactSchema.newId();
  if (!batch.clientSubmissionId && persist) {
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
/**
 * Everything that can refuse a submission BEFORE it reaches the network.
 *
 * Split out from the send for one reason: the delivery ledger. A capture is
 * marked as having left the browser immediately before the request goes out,
 * because a worker killed mid-`fetch` must come back knowing the backend might
 * hold it. That marking must NOT happen for a refusal that never reached the
 * network — an unsigned-in install, a declined permission, a bad target — or a
 * push that was never transmitted would strand its contacts as unsendable.
 */
async function prepareSubmission(payload, explicitTarget) {
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

  return { ok: true, target, url, headers: auth.headers, body: serialized.json };
}

/** Send a prepared submission. From here on the backend may hold the captures. */
async function dispatchSubmission(prepared) {
  const { target, url, headers, body } = prepared;
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), CONTACT_CAPTURE_TIMEOUT_MS);
  try {
    const resp = await fetch(url, {
      method: "POST",
      headers,
      body,
      // The capture credential is the only thing that authorises this request.
      // Omitting ambient cookies keeps that true even if the operator is signed
      // in to the same hosted deployment in this browser.
      credentials: "omit",
      signal: controller.signal,
    });
    clearTimeout(timer);
    const text = await resp.text();
    // NOT `body`: that name belongs to the request payload destructured above,
    // and shadowing it here put the request body in a temporal dead zone.
    let responseBody = null;
    try {
      responseBody = text ? JSON.parse(text) : null;
    } catch (_e) {
      responseBody = { raw: text };
    }
    if (!resp.ok) {
      // The reviewed draft is preserved so a recoverable failure can be retried
      // with the SAME client_submission_id (idempotent).
      return { ok: false, error: "receiver_rejected", status: resp.status, body: responseBody };
    }
    const result = handoff.sanitizeContactSubmissionResult(responseBody, {
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

/** Prepare and send in one step. The profile workflow saves exactly one person. */
async function postSubmission(payload, explicitTarget) {
  const prepared = await prepareSubmission(payload, explicitTarget);
  if (!prepared.ok) return prepared;
  return dispatchSubmission(prepared);
}

// The one-shot listings save that used to live here is gone. Every listings
// save — ten rows or five thousand — is now a durable push (see below), so
// there is exactly one delivery path to reason about and a small capture cannot
// quietly behave differently from a large one. `saveProfileContact` keeps its
// direct send: one person is one contact, and a job for it would be ceremony.


// ---- durable background push ------------------------------------------------
//
// The lifecycle, in one place:
//
//   reviewed set  -> planned into bounded chunks (chunking.js)
//                 -> chunk payloads written to storage
//                 -> job record written to storage
//                 -> Save returns; the panel is free
//                 -> chunks POSTed one at a time, job rewritten after each
//                 -> accepted chunk's payload deleted
//                 -> settled: alarm cleared, outcome retained
//
// Everything after "Save returns" happens in this worker with nobody watching,
// and every step of it is recoverable from `chrome.storage.local` alone.

/** Structured, PII-FREE progress logging.
 *
 * Ids, counts, byte totals, statuses and error CODES only. No name, no profile
 * URL, no company, no label, no note, no snapshot, no token, and no response
 * body — a log line about a capture must never become a copy of it.
 */
function logPush(event, fields) {
  try {
    console.info(
      JSON.stringify(Object.assign({ event: "vmr_push_" + event }, fields || {}))
    );
  } catch (_e) {
    // Logging must never be the thing that breaks a push.
  }
}

async function getPushJob() {
  const data = await chrome.storage.local.get(PUSH_STORAGE.JOB);
  const raw = data[PUSH_STORAGE.JOB];
  return raw && raw.v === pushJob.JOB_VERSION ? raw : null;
}

async function setPushJob(job) {
  await chrome.storage.local.set({ [PUSH_STORAGE.JOB]: job });
  return job;
}

/** Remove the job AND every chunk payload it could still own. */
async function clearPushJob() {
  const job = await getPushJob();
  const keys = pushJob.allChunkKeys(job);
  if (keys.length) await chrome.storage.local.remove(keys);
  await chrome.storage.local.remove(PUSH_STORAGE.JOB);
  await clearResumeAlarm();
  await sweepOrphanChunks();
  return { ok: true };
}

async function readChunkContacts(job, index) {
  const chunk = job.chunks[index];
  if (!chunk) return null;
  const key = pushJob.chunkKey(chunk.clientSubmissionId);
  const data = await chrome.storage.local.get(key);
  const contacts = data[key];
  return Array.isArray(contacts) && contacts.length ? contacts : null;
}

// ---- the delivery ledger ------------------------------------------------------
//
// What has happened to each captured person, by `client_capture_id`. The module
// that reasons about it is `common/push-job.js`; this is only its storage.

async function getLedger() {
  const data = await chrome.storage.local.get(PUSH_STORAGE.LEDGER);
  return pushJob.readLedger(data[PUSH_STORAGE.LEDGER]);
}

async function setLedger(ledger) {
  await chrome.storage.local.set({ [PUSH_STORAGE.LEDGER]: ledger });
  return ledger;
}

async function markDelivery(captureIds, state) {
  if (!captureIds || !captureIds.length) return null;
  return setLedger(pushJob.markDelivery(await getLedger(), captureIds, state));
}

/**
 * Close the book on everything transmitted but never confirmed.
 *
 * Called when a job is cancelled or dismissed: nothing is going to retry those
 * people now, so they stop being "in doubt, awaiting a retry" and become
 * "transmitted, unconfirmed, not retried". They are still never re-planned — the
 * backend may hold them, and only their own frozen chunk could have proved it.
 */
async function finaliseLedger() {
  return setLedger(pushJob.finaliseInDoubt(await getLedger()));
}

/**
 * Reclaim chunk payloads that nothing can ever send.
 *
 * A chunk payload is captured personal data. Before this existed, replacing a
 * job simply overwrote the job pointer and left the previous job's chunk keys
 * behind — unreachable by any code path, unbounded across repeated pushes, and
 * holding the reviewed contacts of every abandoned attempt.
 *
 * Ownership is decided by the CURRENT job and nothing else: a key survives only
 * while it names a chunk that job still has to send. Accepted chunks have
 * already done their work, cancelled chunks will not be attempted again, and a
 * key belonging to no current job is reachable by nothing.
 *
 * Enumerating storage is the point — an index would only ever find keys it was
 * told about, and a key left by an interrupted write is exactly the kind this
 * has to find. `getKeys` avoids reading the values; where Chrome is too old for
 * it (the manifest allows 116) the fallback reads them and throws them away.
 */
async function sweepOrphanChunks() {
  const job = await getPushJob();
  const live = new Set(pushJob.liveChunkKeys(job));
  let keys;
  try {
    keys = chrome.storage.local.getKeys
      ? await chrome.storage.local.getKeys()
      : Object.keys(await chrome.storage.local.get(null));
  } catch (_e) {
    return { ok: true, removed: 0 };
  }
  const orphaned = keys.filter(
    (key) => key.startsWith(PUSH_STORAGE.CHUNK_PREFIX) && !live.has(key)
  );
  if (orphaned.length) {
    await chrome.storage.local.remove(orphaned);
    logPush("chunks_reclaimed", { count: orphaned.length, live: live.size });
  }
  return { ok: true, removed: orphaned.length };
}

async function ensureResumeAlarm() {
  if (!chrome.alarms) return;
  try {
    await chrome.alarms.create(PUSH.RESUME_ALARM, {
      periodInMinutes: PUSH.RESUME_PERIOD_MINUTES,
    });
  } catch (_e) {
    // Without the alarm a push still runs and still resumes whenever the panel
    // is opened or a message arrives. It just loses its unattended wake-up.
  }
}

async function clearResumeAlarm() {
  if (!chrome.alarms) return;
  try {
    await chrome.alarms.clear(PUSH.RESUME_ALARM);
  } catch (_e) {
    // Nothing to do: a stale alarm only causes a no-op resume.
  }
}

/** The panel's view of the push, or null when there has never been one. */
async function pushState() {
  const job = await getPushJob();
  return {
    ok: true,
    push: pushJob.jobView(job),
    pushActive: job ? !pushJob.isTerminal(job) : false,
  };
}

/**
 * Whether the reviewed batch may be changed right now.
 *
 * While a push is unfinished the reviewed set is the thing being delivered.
 * Capturing more rows, changing inclusion, or clearing it would either strand
 * the operator's work or produce a second push whose people the backend has
 * already accepted under the same capture ids. So those actions are refused
 * with a reason, not silently ignored, and nothing about the running push
 * changes.
 */
async function pushBlocking() {
  const job = await getPushJob();
  if (!job || pushJob.isTerminal(job)) return null;
  return {
    ok: false,
    error: "push_in_progress",
    message:
      "A save of this capture is still running. Wait for it to finish, or open " +
      "the panel to watch it.",
    push: pushJob.jobView(job),
  };
}

/**
 * Start one logical push.
 *
 * Does the smallest amount of work that makes the operation recoverable — plan,
 * write the chunks, write the job — and then returns. Delivery is deliberately
 * NOT awaited: tying it to this promise is what used to tie a five-thousand
 * person save to the lifetime of a side panel.
 */
async function startPush() {
  const blocking = await pushBlocking();
  if (blocking) return blocking;

  const existing = await getPushJob();
  const { batch, payload, metadata } = await buildBatchSubmission();

  const size = chunking.checkPushSize(payload.contacts.length, LIMITS.MAX_RECORDS_PER_BATCH);
  if (!size.ok) {
    if (size.code === "empty_batch") {
      return { ok: false, error: "empty_batch", message: "No included contacts to save." };
    }
    // The refusal names the real ceiling. Nothing is transmitted, and the
    // reviewed set is untouched, so the operator can exclude rows and save.
    return {
      ok: false,
      error: "push_limit_exceeded",
      limit: size.limit,
      count: size.count,
      message:
        `One save may contain up to ${size.limit} contacts. This capture has ` +
        `${size.count}. Exclude ${size.count - size.limit} or clear and capture again.`,
    };
  }

  // WHAT THIS SAVE MAY CONTAIN.
  //
  // A `client_capture_id` is unique across the backend's whole capture table.
  // Once a person has been transmitted, the backend may already own their id,
  // and offering it again under a NEW submission id is refused with
  // `client_capture_id_conflict` — permanently, on an operation the operator is
  // entitled to perform (exclude a row, capture more, save again).
  //
  // So a new Save plans ONLY people who have never left the browser. Everyone
  // else is either already saved, or belongs to a chunk that is carried forward
  // below and can only ever be replayed under its own original submission id.
  const ledger = await getLedger();
  const allIds = payload.contacts.map((c) => c.client_capture_id);
  const ledgerState = pushJob.ledgerCounts(ledger, allIds);
  const unsent = payload.contacts.filter((c) => pushJob.isPlannable(ledger, c.client_capture_id));

  // Work the previous job never finished. It keeps its submission ids and its
  // stored payloads: that is what makes carrying it forward safe rather than a
  // second attempt at the same people under new identities.
  const carried = pushJob.carryableChunks(existing);

  if (!unsent.length && !carried.length) {
    return {
      ok: false,
      error: "nothing_to_send",
      alreadySaved: ledgerState.accepted,
      notRetried: ledgerState.inDoubt + ledgerState.terminal,
      message: describeNothingToSend(ledgerState),
      push: existing ? pushJob.jobView(existing) : null,
    };
  }

  const envelopeBytes = contactSchema.envelopeBytes({
    clientSubmissionId: batch.clientSubmissionId,
    captureMode: CAPTURE_MODES.SALESNAV_PEOPLE_SEARCH,
    submittedAt: batch.createdAt,
    extensionVersion: EXTENSION_VERSION,
    metadata,
    campaignId: payload.campaign_id,
  });
  const plan = chunking.planChunks(unsent, {
    measure: contactSchema.captureBytes,
    envelopeBytes,
    maxContacts: Math.min(PUSH.CHUNK_MAX_CONTACTS, LIMITS.MAX_CONTACTS_PER_SUBMISSION),
    maxBytes: Math.min(PUSH.CHUNK_MAX_BYTES, LIMITS.MAX_PAYLOAD_BYTES),
    recordMaxBytes: Math.min(PUSH.RECORD_MAX_BYTES, LIMITS.MAX_PAYLOAD_BYTES),
  });
  if (!plan.chunks.length && !carried.length) {
    return {
      ok: false,
      error: "invalid_payload",
      message:
        "None of the reviewed contacts could be prepared for sending. Nothing was sent.",
      oversized: plan.oversized.length,
    };
  }

  const now = new Date().toISOString();
  const job = pushJob.createJob({
    jobId: contactSchema.newId(),
    logicalSubmissionId: batch.clientSubmissionId,
    createdAt: now,
    plan,
    carried,
    mintId: () => contactSchema.newId(),
    campaignId: payload.campaign_id,
    captureMode: CAPTURE_MODES.SALESNAV_PEOPLE_SEARCH,
    submittedAt: batch.createdAt,
    metadata,
    extensionVersion: EXTENSION_VERSION,
  });

  // The chunk payloads go down BEFORE the job does. A job that names a chunk
  // whose contacts were never written would resume into a hole; a chunk written
  // with no job pointing at it is inert and is reclaimed by the next sweep.
  // Carried chunks are not rewritten — their payloads are already on disk under
  // the same key, and that key is their submission id.
  const writes = {};
  for (const chunk of job.chunks) {
    if (chunk.carried) continue;
    const planned = plan.chunks[chunk.index - carried.length];
    writes[pushJob.chunkKey(chunk.clientSubmissionId)] = planned.indexes.map(
      (i) => unsent[i]
    );
  }
  if (Object.keys(writes).length) await chrome.storage.local.set(writes);
  await setPushJob(job);
  // A push in flight has no outcome yet, and an older one must not stand in
  // front of it.
  await clearLastResult();
  // The job that just went out of scope may have left chunk payloads behind.
  // They are captured personal data and nothing can send them now.
  await sweepOrphanChunks();
  await ensureResumeAlarm();
  logPush("planned", {
    job_id: job.jobId,
    contacts: job.totalContacts,
    planned_contacts: job.plannedContacts,
    chunks: job.totalChunks,
    carried_chunks: carried.length,
    already_saved: ledgerState.accepted,
    not_retried: ledgerState.inDoubt + ledgerState.terminal,
    envelope_bytes: envelopeBytes,
    oversized: plan.oversized.length,
  });

  // Deliberately not awaited. The operator's Save is finished the moment the
  // job is durable; the transfer is this worker's problem from here.
  drivePush();
  return {
    ok: true,
    push: pushJob.jobView(job),
    // What this Save deliberately left out, so the panel can say so rather than
    // let the operator wonder why 350 reviewed contacts became a 100-row save.
    alreadySaved: ledgerState.accepted,
    notRetried: ledgerState.inDoubt + ledgerState.terminal,
  };
}

/** Why a Save had nothing to do. Every branch is a different true sentence. */
function describeNothingToSend(state) {
  if (state.accepted && !state.inDoubt && !state.terminal) {
    return state.accepted === 1
      ? "This contact has already been saved."
      : `All ${state.accepted} of these contacts have already been saved.`;
  }
  if (!state.accepted && (state.inDoubt || state.terminal)) {
    return (
      `${state.inDoubt + state.terminal} contact(s) were already sent and never confirmed. ` +
      "Check VMR Outbound before capturing them again."
    );
  }
  return (
    `${state.accepted} contact(s) are saved and ${state.inDoubt + state.terminal} were sent ` +
    "without confirmation. There is nothing new here to send."
  );
}

// One delivery loop per worker instance. Two loops would race for the same
// chunk, and while the backend would deduplicate the result the attempt counter
// would not.
let pushDriveInFlight = false;

// Failures that are about the PUSH, not about the chunk that happened to hit
// them. A revoked account link or a declined host permission will refuse chunk
// 2 exactly as it refused chunk 1, so carrying on would burn every chunk's
// attempts against a condition only the operator can clear. The loop stops
// instead, leaving the remaining chunks pending and retryable, and the alarm
// picks the push up again once the operator has fixed it.
const PUSH_BLOCKING_ERRORS = new Set([
  "account_link_required",
  "account_link_unavailable",
  "account_link_revoked",
  "credential_missing",
  "credential_rejected",
  "extension_not_approved",
  "permission_denied",
  "origin_not_allowed",
  "rate_limited",
]);

/** Deliver whatever of the current job can be delivered right now. */
async function drivePush() {
  if (pushDriveInFlight) return { ok: true, busy: true };
  pushDriveInFlight = true;
  try {
    for (;;) {
      let job = await getPushJob();
      if (!job) return { ok: true, push: null };
      if (pushJob.isTerminal(job)) {
        await settlePush(job);
        return { ok: true, push: pushJob.jobView(job) };
      }

      const next = pushJob.nextChunk(job, Date.now());
      if (!next.chunk) {
        if (next.reason === "waiting") {
          // Everything left is in backoff. The alarm brings us back.
          await ensureResumeAlarm();
          return { ok: true, push: pushJob.jobView(job), waiting: true };
        }
        job = pushJob.settle(job, Date.now());
        await setPushJob(job);
        await settlePush(job);
        return { ok: true, push: pushJob.jobView(job) };
      }

      const index = next.chunk.index;
      const contacts = await readChunkContacts(job, index);
      if (!contacts) {
        // The payload is gone but the job still names it. That is not a network
        // problem and retrying cannot fix it, so it is recorded as what it is.
        pushJob.markFailed(job, index, { code: "chunk_payload_missing", retryable: false }, Date.now());
        await setPushJob(job);
        logPush("chunk_missing", { job_id: job.jobId, chunk: index });
        continue;
      }

      // Counted and written down BEFORE the request. A worker that dies inside
      // the request comes back having spent an attempt.
      pushJob.markAttempt(job, index, Date.now());
      await setPushJob(job);

      const chunk = job.chunks[index];
      const jobId = job.jobId;
      const captureIds = contacts.map((c) => c.client_capture_id);
      const chunkPayload = contactSchema.buildSubmission({
        clientSubmissionId: chunk.clientSubmissionId,
        captureMode: job.captureMode,
        submittedAt: job.submittedAt,
        extensionVersion: job.extensionVersion,
        metadata: job.metadata,
        campaignId: job.campaignId,
        contacts,
      });
      logPush("chunk_attempt", {
        job_id: job.jobId,
        chunk: index,
        chunks: job.totalChunks,
        records: contacts.length,
        bytes: chunk.bytes,
        attempt: chunk.attempts,
        carried: chunk.carried === true,
      });

      // Refusals that never reach the network are handled first, precisely so
      // that nothing is recorded as transmitted when nothing was.
      const prepared = await prepareSubmission(chunkPayload);
      let response;
      if (!prepared.ok) {
        response = prepared;
      } else {
        // The point of no return. From here the backend may hold these capture
        // ids whatever happens next — including this worker being killed inside
        // the request — so they stop being plannable BEFORE the request goes.
        await markDelivery(captureIds, PUSH.DELIVERY.IN_DOUBT);
        response = await dispatchSubmission(prepared);
      }

      // Re-read: storage is the authority, and this await is long enough for the
      // job to have been dismissed or replaced underneath us. Folding a response
      // into a job it does not belong to would be worse than losing it.
      job = await getPushJob();
      if (!job || job.jobId !== jobId || !job.chunks[index]) {
        logPush("chunk_orphaned", { job_id: jobId, chunk: index });
        return { ok: true, push: pushJob.jobView(job) };
      }

      if (response.ok) {
        // Proof, and it outranks everything: these people are saved.
        await markDelivery(captureIds, PUSH.DELIVERY.ACCEPTED);
        pushJob.markAccepted(job, index, response.result, Date.now());
        await setPushJob(job);
        // The copy has served its purpose. Deleting it here is what makes a
        // long push consume LESS storage as it goes rather than more.
        await chrome.storage.local.remove(pushJob.chunkKey(chunk.clientSubmissionId));
        logPush("chunk_accepted", {
          job_id: job.jobId,
          chunk: index,
          records: contacts.length,
          replayed: response.result && response.result.alreadyReceived === true,
        });
        // A response that arrived after the operator pressed Cancel is recorded
        // — it is true — but it does not restart a cancelled push.
        if (job.status === pushJob.STATUS.CANCELLED) {
          await settlePush(job);
          return { ok: true, push: pushJob.jobView(job), cancelled: true };
        }
        continue;
      }

      const detail = handoff.describeSendError(response);
      // The operator pressed Cancel while this request was in the air. Recording
      // a retryable failure now would put the chunk back into PENDING — which
      // is a live, unsweepable, unsendable chunk and a job that says cancelled
      // while behaving as though it were not.
      if (job.status === pushJob.STATUS.CANCELLED) {
        logPush("chunk_failed_after_cancel", {
          job_id: job.jobId,
          chunk: index,
          error: detail.code,
        });
        return { ok: true, push: pushJob.jobView(job), cancelled: true };
      }
      pushJob.markFailed(
        job,
        index,
        { code: detail.code, retryable: detail.canRetry !== false },
        Date.now()
      );
      await setPushJob(job);
      logPush("chunk_failed", {
        job_id: job.jobId,
        chunk: index,
        records: contacts.length,
        attempt: job.chunks[index].attempts,
        error: detail.code,
        status: response.status || null,
        retryable: job.chunks[index].status === pushJob.CHUNK_STATUS.PENDING,
      });
      await ensureResumeAlarm();
      if (PUSH_BLOCKING_ERRORS.has(detail.code)) {
        logPush("blocked", { job_id: job.jobId, error: detail.code });
        return { ok: true, push: pushJob.jobView(job), blocked: detail.code };
      }
      // Otherwise the loop continues: a chunk in backoff is skipped, a failed
      // chunk is finished with, and the chunks after it are still owed delivery.
    }
  } finally {
    pushDriveInFlight = false;
  }
}

// Whether this worker instance has already reclaimed orphaned chunk storage.
// A restarted worker sweeps again — that IS the recovery path — but the alarm
// firing once a minute through a long push does not re-enumerate storage.
let sweptThisSession = false;

async function sweepOnce() {
  if (sweptThisSession) return { ok: true, removed: 0, skipped: true };
  sweptThisSession = true;
  return sweepOrphanChunks();
}

/** Resume after suspension, restart, or a panel opening. */
async function resumePush() {
  // Recovery is exactly when a payload nothing owns comes to light: a job that
  // was replaced or interrupted before it could tidy up leaves keys that only a
  // sweep can find.
  await sweepOnce();
  const job = await getPushJob();
  if (!job) {
    await clearResumeAlarm();
    return { ok: true, push: null };
  }
  if (pushJob.isTerminal(job)) {
    await clearResumeAlarm();
    return { ok: true, push: pushJob.jobView(job) };
  }
  logPush("resume", {
    job_id: job.jobId,
    chunks: job.totalChunks,
    accepted: pushJob.contactsAccepted(job),
    contacts: job.totalContacts,
  });
  return drivePush();
}

/**
 * A settled push: stop the alarm, drop any chunk payload still on disk, and
 * retain the outcome so reopening the panel shows it.
 *
 * WHAT IS NOT DONE HERE: the reviewed capture is not cleared. Delivery finishing
 * is not the operator deciding they are done with the rows, and a batch that
 * vanishes on completion is a batch that cannot be re-examined against the
 * outcome. Clearing stays an explicit operator action, and it is refused while a
 * push is unfinished — which is the whole of the rule about when captured data
 * becomes safe to remove.
 */
async function settlePush(job) {
  await clearResumeAlarm();
  // Anything transmitted and never confirmed stops waiting for a retry that is
  // not coming, and is reported as what it is. It is still never re-planned.
  await finaliseLedger();
  // Every chunk this job will not send again is captured personal data with no
  // remaining reader.
  await sweepOrphanChunks();
  // Offer the labels again next time, but only once some of this push actually
  // landed — the old one-shot save remembered them on success and nothing about
  // chunking changes when a label counts as used.
  if (pushJob.contactsAccepted(job) > 0) {
    await rememberLabels((job.metadata && job.metadata.labels) || []);
  }
  await setLastResult(pushResultSummary(job), LISTINGS_CONTEXT);
  logPush("settled", {
    job_id: job.jobId,
    status: job.status,
    contacts: job.totalContacts,
    accepted: pushJob.contactsAccepted(job),
    failed_chunks: job.chunks.filter((c) => c.status === pushJob.CHUNK_STATUS.FAILED).length,
  });
  return job;
}

/**
 * The retained outcome of a whole push.
 *
 * `counts` are summed over every accepted chunk, so they describe the operation
 * the operator performed rather than the last request that happened to run.
 * `resultsSeen` and `resultsRetained` are kept apart on purpose: a 5,000-contact
 * push processes 5,000 people and stores a bounded number of detail rows, and
 * the panel must be able to say both.
 */
function pushResultSummary(job) {
  const view = pushJob.jobView(job);
  return {
    submissionId: null,
    clientSubmissionId: job.logicalSubmissionId,
    alreadyReceived: false,
    receivedAt: job.updatedAt,
    counts: view.counts,
    results: view.results,
    resultsSeen: view.resultsSeen,
    resultsRetained: view.resultsRetained,
    resultsTruncated: view.resultsTruncated,
    workbenchUrl: job.workbenchUrl,
    submittedAt: job.createdAt,
    push: {
      jobId: view.jobId,
      status: view.status,
      totalContacts: view.totalContacts,
      contactsAccepted: view.contactsAccepted,
      contactsFailed: view.contactsFailed,
      contactsCancelled: view.contactsCancelled,
      totalChunks: view.totalChunks,
      failedChunks: view.failedChunks,
      retryableChunks: view.retryableChunks,
      failures: view.failures,
      oversized: view.oversized,
    },
  };
}

/** Re-arm the failed chunks of a settled push. Same ids, so no duplicates. */
async function retryPush() {
  const job = await getPushJob();
  if (!job) return { ok: false, error: "no_push" };
  const { armed } = pushJob.retryFailed(job, Date.now());
  if (!armed) return { ok: false, error: "nothing_to_retry", push: pushJob.jobView(job) };
  await setPushJob(job);
  await ensureResumeAlarm();
  logPush("retry", { job_id: job.jobId, chunks: armed });
  drivePush();
  return { ok: true, push: pushJob.jobView(job) };
}

/**
 * Stop an unfinished push. The operator's escape hatch, and NOT a rollback.
 *
 * Without this an unrecoverable push was a dead end: a revoked account link
 * meant every resume refused, the job stayed unfinished for ever, and because an
 * unfinished push holds the reviewed set, capture, exclude, clear and dismiss
 * were all refused. The extension could be wedged by a condition the operator
 * had no control over and no way to acknowledge.
 *
 * What cancelling does NOT do is undo anything. Contacts the backend accepted
 * stay accepted — nothing here can reach across and un-commit them, and
 * pretending otherwise would be a lie about somebody's data. What it does is
 * stop offering the rest, release the reviewed set, and say plainly how the two
 * halves ended up.
 */
async function cancelPush() {
  const job = await getPushJob();
  if (!job) return { ok: false, error: "no_push" };
  if (pushJob.isTerminal(job)) {
    return { ok: false, error: "push_not_running", push: pushJob.jobView(job) };
  }
  const outcome = pushJob.cancel(job, new Date().toISOString());
  await setPushJob(job);
  await clearResumeAlarm();
  // Transmitted-but-unconfirmed people stop being "awaiting a retry". They are
  // still never re-planned: the backend may hold them.
  await finaliseLedger();
  // Every cancelled chunk's payload is now unreachable by any code path.
  await sweepOrphanChunks();
  await setLastResult(pushResultSummary(job), LISTINGS_CONTEXT);
  logPush("cancelled", {
    job_id: job.jobId,
    accepted: outcome.accepted,
    not_sent: outcome.notSent,
    transmitted_unconfirmed: outcome.transmitted,
  });
  return {
    ok: true,
    push: pushJob.jobView(job),
    accepted: outcome.accepted,
    notSent: outcome.notSent,
    transmitted: outcome.transmitted,
  };
}

/** Dismiss a SETTLED push. An unfinished one is never thrown away by accident. */
async function dismissPush() {
  const job = await getPushJob();
  if (job && !pushJob.isTerminal(job)) {
    return {
      ok: false,
      error: "push_in_progress",
      push: pushJob.jobView(job),
    };
  }
  await finaliseLedger();
  await clearPushJob();
  return { ok: true, push: null };
}

// ---- local export -----------------------------------------------------------
//
// The operator's own copy of the capture they just reviewed. Entirely local: it
// reads the reviewed batch, formats it here, and hands the text to the panel to
// save. No request is made, so it works with VMR Outbound unreachable, and it
// is only ever produced in response to an explicit operator action.
//
// Downloading changes nothing. The reviewed set is not marked, not consumed and
// not cleared, and the push that would save it is exactly as available
// afterwards as it was before.

function sanitizeFilename(name) {
  return (
    String(name)
      .replace(/[^a-zA-Z0-9._-]+/g, "_")
      .replace(/_{2,}/g, "_")
      .replace(/^[_.]+|[_.]+$/g, "")
      .slice(0, 120) || "capture"
  );
}

/**
 * Build the export text for the INCLUDED rows of the reviewed capture.
 *
 * Included, not everything: an excluded row is one the operator decided is not
 * part of this capture, and that decision means the same thing for the file as
 * it does for the save. This is the behaviour the export had before it was
 * removed, and it is preserved rather than reconsidered here.
 *
 *   csv   the flat review sheet — one row per contact, the historical column
 *         contract (src/common/schema.js CSV_COLUMNS)
 *   json  the exact contact-first submission body, pretty-printed, so what was
 *         exported and what would be sent are the same thing
 */
async function buildCapturedExport(format) {
  const wanted = EXPORT.FORMATS.includes(format) ? format : "csv";
  const { payload, records, batch } = await buildBatchSubmission({ persist: false });
  if (!records.length) {
    return { ok: false, error: "empty_batch", message: "No included contacts to export." };
  }
  const stamp = String(batch.createdAt || "").replace(/[:.]/g, "-");
  const base = sanitizeFilename(`${EXPORT.FILENAME_PREFIX}_${stamp}`);
  const text = wanted === "csv" ? schema.toCsv(records) : JSON.stringify(payload, null, 2);
  const mime = wanted === "csv" ? EXPORT.CSV_MIME : EXPORT.JSON_MIME;
  logPush("export", { format: wanted, records: records.length, bytes: text.length });
  return {
    ok: true,
    format: wanted,
    filename: `${base}.${wanted}`,
    mime,
    text,
    records: records.length,
  };
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
  if (result.error === "account_link_required") {
    // A hosted deployment that is running perfectly and simply is not linked to
    // this operator's account yet. Reporting that as "unreachable" would send
    // them to check a server that is fine.
    return { ok: false, state: "sign_in_required" };
  }
  if (result.error === "account_link_unavailable") {
    return { ok: false, state: "unreachable", error: result.error };
  }
  if (result.error === "credential_missing") {
    // Development path only: a developer-configured install with the gate on and
    // no legacy credential set.
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

// ---- message router -------------------------------------------------------

chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  (async () => {
    switch (msg && msg.type) {
      case "GET_STATE": {
        const batch = await ensureBatch();
        const prefs = await getPrefs();
        const retained = await getLastResult();
        const push = await pushState();
        // Opening the panel is one of the ways an interrupted push comes back.
        // Not awaited: the panel gets the state as it stands, and the delivery
        // it just restarted reports itself through the same state next time.
        resumePush();
        sendResponse({
          ok: true,
          prefs,
          batchView: buildBatchView(batch),
          lastResult: retained.result,
          lastResultContext: retained.context,
          metadata: await getOperatorMetadata(),
          filingContext: await getFilingContext(),
          credential: await credentialState(),
          account: await accountState(),
          push: push.push,
          pushActive: push.pushActive,
          dev: { enabled: await devModeEnabled() },
        });
        break;
      }
      case "DETECT_ACTIVE_PAGE":
        sendResponse(await detectActivePage());
        break;
      case "CAPTURE_ACTIVE_PAGE": {
        const blocked = await pushBlocking();
        sendResponse(blocked || (await captureActivePage()));
        break;
      }
      case "CANCEL_CAPTURE":
        sendResponse(await cancelActiveCapture());
        break;
      case "SET_PREFS":
        sendResponse({ ok: true, prefs: await setPrefs(msg.prefs) });
        break;
      case "TOGGLE_EXCLUDE": {
        const blocked = await pushBlocking();
        sendResponse(
          blocked || { ok: true, batchView: await toggleExclude(msg.stableKey, msg.index) }
        );
        break;
      }
      case "CLEAR_BATCH": {
        // The one action that could destroy work a running push still needs.
        const blocked = await pushBlocking();
        sendResponse(blocked || { ok: true, batchView: await clearBatch() });
        break;
      }
      case "PREVIEW_PAYLOAD": {
        const { payload } = await buildBatchSubmission();
        const validation = contactSchema.validateSubmission(payload);
        const serialized = contactSchema.serializePayload(payload);
        sendResponse({ ok: true, payload, validation, bytes: serialized.bytes });
        break;
      }
      // One operator Save. Returns as soon as the push is durable; delivery
      // continues in this worker and is reported through PUSH_STATE.
      case "SAVE_INCLUDED_CONTACTS":
        sendResponse(await startPush());
        break;
      case "PUSH_STATE":
        sendResponse(await pushState());
        break;
      case "RESUME_PUSH":
        sendResponse(await resumePush());
        break;
      case "RETRY_PUSH":
        sendResponse(await retryPush());
        break;
      case "CANCEL_PUSH":
        sendResponse(await cancelPush());
        break;
      case "DISMISS_PUSH":
        sendResponse(await dismissPush());
        break;
      // Local, backend-free, explicit. Never clears or alters the capture.
      case "EXPORT_CAPTURED_CONTACTS":
        sendResponse(await buildCapturedExport(msg.format));
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
      // The account link. The panel learns whether this install is linked and to
      // which account; a token never appears in any of these responses.
      case "GET_ACCOUNT_STATE":
        // `autoConnect` is the panel's cold open: connect silently if the
        // operator is already signed in and this install is already approved,
        // which is what makes "install, open, capture" need no click at all.
        sendResponse(
          msg.autoConnect === true
            ? await ensureAccountConnected()
            : { ok: true, account: await accountState() }
        );
        break;
      case "CONNECT_ACCOUNT":
        sendResponse(await connectAccount());
        break;
      case "DISCONNECT_ACCOUNT":
        sendResponse(await disconnectAccount());
        break;
      // The legacy credential is write-only from the panel's side: it can be
      // set, cleared, and asked about, but never read back — and it can only be
      // set at all behind the development gate.
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
          account: await accountState(),
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
      default:
        sendResponse({ ok: false, error: "unknown_message" });
    }
  })().catch((e) => sendResponse({ ok: false, error: "worker_exception", detail: String(e && e.message) }));
  return true; // async response
});
