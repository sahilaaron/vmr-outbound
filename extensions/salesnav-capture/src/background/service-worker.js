/**
 * Service worker: extension state hub + backend/mock communication + downloads.
 *
 * Responsibilities:
 *  - Own the recoverable draft batch in chrome.storage.local.
 *  - Relay capture/detect requests to the active Sales Navigator tab's content
 *    script and merge results (dedupe) into the batch.
 *  - Build the intake payload and POST it — ONLY on explicit operator action —
 *    to the configured mock receiver or local backend.
 *  - Produce JSON / CSV downloads.
 *
 * Never stores credentials/cookies/tokens. Never posts to LinkedIn. Nothing is
 * ever sent without an explicit SEND_BATCH message triggered by the operator.
 */
importScripts(
  "../common/constants.js",
  "../common/normalize.js",
  "../common/dedupe.js",
  "../common/schema.js",
  "../common/permissions.js"
);

const { constants, dedupe, schema, permissions } = self.SNCapture;
const { STORAGE, DEFAULT_PREFERENCES, LIMITS, CAPTURE_STATUS, ALLOWED_BACKEND_ORIGIN_PATTERNS, INTAKE_PATH } =
  constants;

const EXTENSION_VERSION = chrome.runtime.getManifest().version;
const SEND_TIMEOUT_MS = 15000;

// Open the side panel when the toolbar icon is clicked.
chrome.runtime.onInstalled.addListener(() => {
  if (chrome.sidePanel && chrome.sidePanel.setPanelBehavior) {
    chrome.sidePanel.setPanelBehavior({ openPanelOnActionClick: true }).catch(() => {});
  }
});

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

async function captureActivePage() {
  const r = await askContentScript({ type: "CS_CAPTURE" });
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

  const merged = dedupe.mergeBatch(batch.records, incoming);
  batch.records = merged.records;
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
  await setBatch(batch);
  return buildBatchView(batch);
}

async function clearBatch() {
  await chrome.storage.local.remove(STORAGE.DRAFT_BATCH);
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

async function buildCurrentPayload() {
  const batch = await ensureBatch();
  const prefs = await getPrefs();
  const records = includedRecords(batch);
  const payload = schema.buildPayload({
    records,
    clientBatchId: batch.clientBatchId,
    campaignId: prefs.lastCampaignId || null,
    capturedAt: batch.createdAt,
    currentSearchUrl: batch.lastSearchUrl,
    extractionMeta: {
      extension_version: EXTENSION_VERSION,
      pages_captured: batch.pagesCaptured.length,
      record_count: records.length,
      capture_statuses: batch.statuses.map((s) => s.status),
      warnings_summary: warningsSummary(records),
    },
  });
  return { batch, prefs, payload, records };
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

async function sendBatch(explicitTarget) {
  const { payload, records } = await buildCurrentPayload();

  if (records.length === 0) {
    return { ok: false, error: "empty_batch", message: "No included records to send." };
  }
  const validation = schema.validatePayload(payload);
  if (!validation.valid) {
    return { ok: false, error: "invalid_payload", messages: validation.errors };
  }
  const serialized = schema.serializePayload(payload);
  if (!serialized.withinLimit) {
    return {
      ok: false,
      error: "payload_too_large",
      message: `Payload ${serialized.bytes} bytes exceeds ${LIMITS.MAX_PAYLOAD_BYTES}. Reduce the batch.`,
    };
  }

  const prefs = await getPrefs();
  const target = explicitTarget || prefs.sendTarget || "mock";
  let url;
  if (target === "mock") {
    url = prefs.mockReceiverUrl;
  } else {
    const base = (prefs.backendBaseUrl || "").replace(/\/$/, "");
    url = base + INTAKE_PATH;
  }

  if (!isAllowedBackendOrigin(url)) {
    return {
      ok: false,
      error: "origin_not_allowed",
      message: `Refusing to send to ${url}. Only loopback origins are permitted.`,
    };
  }

  const perm = await hasHostPermission(url);
  if (!perm.ok) {
    return {
      ok: false,
      error: "permission_denied",
      originPattern: perm.pattern,
      message: `Loopback access not granted for ${perm.pattern || url}. Approve the permission prompt, then send again.`,
    };
  }

  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), SEND_TIMEOUT_MS);
  try {
    const resp = await fetch(url, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-Client-Batch-Id": payload.client_batch_id,
        "Idempotency-Key": payload.client_batch_id,
      },
      body: serialized.json,
      signal: controller.signal,
    });
    clearTimeout(timer);
    const text = await resp.text();
    let body = null;
    try { body = text ? JSON.parse(text) : null; } catch (_e) { body = { raw: text }; }
    if (!resp.ok) {
      return { ok: false, error: "receiver_rejected", status: resp.status, body };
    }
    return { ok: true, status: resp.status, body, target, url };
  } catch (e) {
    clearTimeout(timer);
    if (e && e.name === "AbortError") {
      return { ok: false, error: "timeout", message: `No response within ${SEND_TIMEOUT_MS}ms.` };
    }
    return { ok: false, error: "network_error", message: String(e && e.message) };
  }
}

// ---- campaigns ------------------------------------------------------------

async function fetchCampaigns() {
  const prefs = await getPrefs();
  const base = (prefs.backendBaseUrl || "").replace(/\/$/, "");
  const url = base + "/api/campaigns?fields=id,name,status";
  if (!isAllowedBackendOrigin(url)) {
    return { ok: false, error: "origin_not_allowed" };
  }
  const perm = await hasHostPermission(url);
  if (!perm.ok) {
    return { ok: false, error: "permission_denied", originPattern: perm.pattern };
  }
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), SEND_TIMEOUT_MS);
  try {
    const resp = await fetch(url, { signal: controller.signal });
    clearTimeout(timer);
    if (!resp.ok) return { ok: false, error: "http_" + resp.status };
    const body = await resp.json();
    // Accept only minimal fields.
    const campaigns = (Array.isArray(body) ? body : body.campaigns || []).map((c) => ({
      id: String(c.id),
      name: String(c.name || ""),
      status: String(c.status || ""),
    }));
    return { ok: true, campaigns };
  } catch (e) {
    clearTimeout(timer);
    return { ok: false, error: e && e.name === "AbortError" ? "timeout" : "network_error" };
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

async function exportBatch(format) {
  const { payload, batch } = await buildCurrentPayload();
  const stamp = batch.createdAt.replace(/[:.]/g, "-");
  const base = sanitizeFilename(`salesnav_capture_${stamp}`);
  let mime, text, ext;
  if (format === "csv") {
    mime = "text/csv";
    text = schema.toCsv(payload.records);
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
  return { ok: true, filename, records: payload.records.length };
}

// ---- message router -------------------------------------------------------

chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  (async () => {
    switch (msg && msg.type) {
      case "GET_STATE": {
        const batch = await ensureBatch();
        const prefs = await getPrefs();
        sendResponse({ ok: true, prefs, batchView: buildBatchView(batch) });
        break;
      }
      case "DETECT_ACTIVE_PAGE":
        sendResponse(await detectActivePage());
        break;
      case "CAPTURE_ACTIVE_PAGE":
        sendResponse(await captureActivePage());
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
        const { payload } = await buildCurrentPayload();
        const validation = schema.validatePayload(payload);
        const serialized = schema.serializePayload(payload);
        sendResponse({ ok: true, payload, validation, bytes: serialized.bytes });
        break;
      }
      case "SEND_BATCH":
        sendResponse(await sendBatch(msg.target));
        break;
      case "FETCH_CAMPAIGNS":
        sendResponse(await fetchCampaigns());
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
