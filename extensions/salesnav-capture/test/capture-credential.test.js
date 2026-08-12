"use strict";
/**
 * The hosted capture credential, exercised through the REAL service worker.
 *
 * Three properties are worth a test here, and they are the three that would be
 * silently wrong if this were only reviewed by eye:
 *
 *  1. The credential is attached to a hosted request and to nothing else. A
 *     loopback send must stay byte-identical to what it was, because local
 *     development has no authenticated intake and quietly starting to send an
 *     `Authorization` header there would be a behaviour change nobody asked for.
 *  2. The credential never leaves the worker except in that header. The panel
 *     is told whether one is held and never what it is, so there is nothing for
 *     the DOM, a screenshot or a stored draft to carry.
 *  3. It lives in `chrome.storage.session`, not `local`. The harness models the
 *     two areas separately precisely so this can be asserted rather than
 *     assumed.
 */
const { test } = require("node:test");
const assert = require("node:assert/strict");
const { createWorker } = require("./worker-harness.js");
const constants = require("../src/common/constants.js");
const handoff = require("../src/common/handoff.js");

const HOSTED_BASE = constants.HOSTED_BACKEND_ORIGINS[0];
const LOOPBACK_BASE = "http://127.0.0.1:8000";
const CREDENTIAL = "vmrx1.beta-laptop.3fVQx8Zk2nLp7Rw6TyUiOaSdFgHjKlZxCvBnM4qWeRt";

/** One reviewed profile draft, enough for the worker to build a submission. */
function profileDraft() {
  return {
    clientCaptureId: "11111111-1111-4111-8111-111111111111",
    clientSubmissionId: null,
    createdAt: "2026-01-01T00:00:00.000Z",
    excludedSections: [],
    pageTitle: "Morgan Vale | LinkedIn",
    extraction: {
      status: "ok",
      capturedAt: "2026-01-01T00:00:00.000Z",
      profile: {
        linkedin_profile_url: "https://www.linkedin.com/in/morgan-vale",
        full_name: "Morgan Vale",
        first_name: "Morgan",
        last_name: "Vale",
        headline: "Operations Manager",
        location: "Bristol, England, United Kingdom",
        about: null,
        connections_label: null,
        followers_label: null,
      },
      experiences: [],
      missingSections: [],
      pageWarnings: [],
    },
  };
}

/** A worker whose fetch records every call and answers with `response`. */
function workerWith(options) {
  const calls = [];
  const o = options || {};
  const worker = createWorker({
    sessionStorage: o.sessionStorage,
    storage: Object.assign(
      {
        [constants.STORAGE.PREFERENCES]: {
          sendTarget: "backend",
          backendBaseUrl: o.base || LOOPBACK_BASE,
        },
        [constants.PROFILE_STORAGE.DRAFT_PROFILE]: profileDraft(),
      },
      o.storage
    ),
    fetch: (url, init) => {
      calls.push({ url, init });
      const answer = o.response || {
        status: 201,
        body: { submission_id: "s-1", counts: {}, results: [] },
      };
      return Promise.resolve({
        ok: answer.status < 400,
        status: answer.status,
        text: () => Promise.resolve(JSON.stringify(answer.body || {})),
        json: () => Promise.resolve(answer.body || {}),
      });
    },
  });
  return { worker, calls };
}

function authHeader(call) {
  const headers = (call && call.init && call.init.headers) || {};
  return headers.Authorization || headers.authorization || null;
}

// --- 1. Which requests carry the credential ----------------------------------

test("a hosted capture carries the credential as a bearer header", async () => {
  const { worker, calls } = workerWith({
    base: HOSTED_BASE,
    sessionStorage: { [constants.CREDENTIAL_STORAGE.CAPTURE_CREDENTIAL]: CREDENTIAL },
  });
  const r = await worker.dispatch({ type: "SAVE_CONTACT", target: "backend" });
  assert.equal(r.ok, true, JSON.stringify(r));
  assert.equal(calls.length, 1);
  assert.ok(calls[0].url.startsWith(HOSTED_BASE + constants.CONTACT_CAPTURE_PATH));
  assert.equal(authHeader(calls[0]), "Bearer " + CREDENTIAL);
  // Ambient cookies are explicitly not sent: the credential is the only thing
  // that authorises this request, and an operator signed in to the same hosted
  // deployment must not change that.
  assert.equal(calls[0].init.credentials, "omit");
});

test("a loopback capture sends no credential, even when one is held", async () => {
  const { worker, calls } = workerWith({
    base: LOOPBACK_BASE,
    sessionStorage: { [constants.CREDENTIAL_STORAGE.CAPTURE_CREDENTIAL]: CREDENTIAL },
  });
  const r = await worker.dispatch({ type: "SAVE_CONTACT", target: "backend" });
  assert.equal(r.ok, true, JSON.stringify(r));
  assert.equal(authHeader(calls[0]), null);
});

test("the three contract reads carry the credential against hosted", async () => {
  for (const message of [
    { type: "FETCH_LABELS" },
    { type: "FETCH_CAMPAIGNS" },
    { type: "PROFILE_MATCH_STATE" },
  ]) {
    const { worker, calls } = workerWith({
      base: HOSTED_BASE,
      sessionStorage: { [constants.CREDENTIAL_STORAGE.CAPTURE_CREDENTIAL]: CREDENTIAL },
      response: { status: 200, body: { labels: [], campaigns: [], match: "none" } },
    });
    await worker.dispatch(message);
    assert.equal(calls.length, 1, message.type);
    assert.equal(authHeader(calls[0]), "Bearer " + CREDENTIAL, message.type);
  }
});

// --- 2. What happens when there is no credential ------------------------------

test("a hosted capture with no credential refuses before sending anything", async () => {
  const { worker, calls } = workerWith({ base: HOSTED_BASE });
  const r = await worker.dispatch({ type: "SAVE_CONTACT", target: "backend" });
  assert.equal(r.ok, false);
  assert.equal(r.error, "credential_missing");
  // The point of refusing here rather than sending: nothing left the browser.
  assert.equal(calls.length, 0);
});

test("a missing credential is an actionable message, not a bare failure", () => {
  const described = handoff.describeSendError({ ok: false, error: "credential_missing" });
  assert.equal(described.code, "credential_missing");
  assert.match(described.headline, /credential/i);
  assert.match(described.detail, /Settings/);
  assert.equal(described.canRetry, true);
});

test("a hosted probe reports a needed credential rather than an unreachable server", async () => {
  const { worker } = workerWith({ base: HOSTED_BASE });
  const r = await worker.dispatch({ type: "PROBE_BACKEND" });
  assert.equal(r.state, "credential_required");
});

test("a browser without session storage refuses to hold a credential", async () => {
  const { worker } = workerWith({ base: HOSTED_BASE, sessionStorage: null });
  const set = await worker.dispatch({ type: "SET_CAPTURE_CREDENTIAL", credential: CREDENTIAL });
  assert.equal(set.ok, false);
  assert.equal(set.error, "credential_storage_unavailable");
  const state = await worker.dispatch({ type: "GET_CREDENTIAL_STATE" });
  assert.equal(state.storageAvailable, false);
  assert.equal(state.hasCredential, false);
});

// --- 3. Storage, and what the panel may learn ---------------------------------

test("a credential is stored in session storage and never in local storage", async () => {
  const { worker } = workerWith({ base: HOSTED_BASE });
  const set = await worker.dispatch({ type: "SET_CAPTURE_CREDENTIAL", credential: CREDENTIAL });
  assert.equal(set.ok, true);
  assert.equal(
    worker.sessionStore[constants.CREDENTIAL_STORAGE.CAPTURE_CREDENTIAL],
    CREDENTIAL
  );
  assert.equal(
    JSON.stringify(worker.store).includes(CREDENTIAL),
    false,
    "the credential must never reach the area that persists to disk"
  );
});

test("clearing removes the credential", async () => {
  const { worker } = workerWith({
    base: HOSTED_BASE,
    sessionStorage: { [constants.CREDENTIAL_STORAGE.CAPTURE_CREDENTIAL]: CREDENTIAL },
  });
  const cleared = await worker.dispatch({ type: "CLEAR_CAPTURE_CREDENTIAL" });
  assert.equal(cleared.hasCredential, false);
  assert.equal(JSON.stringify(worker.sessionStore).includes(CREDENTIAL), false);
});

test("the worker reports whether a credential is held, never what it is", async () => {
  const { worker } = workerWith({
    base: HOSTED_BASE,
    sessionStorage: { [constants.CREDENTIAL_STORAGE.CAPTURE_CREDENTIAL]: CREDENTIAL },
  });
  for (const message of [
    { type: "GET_CREDENTIAL_STATE" },
    { type: "GET_STATE" },
    { type: "PROFILE_GET_STATE" },
  ]) {
    const r = await worker.dispatch(message);
    assert.equal(
      JSON.stringify(r).includes(CREDENTIAL),
      false,
      `${message.type} leaked the credential`
    );
  }
  const state = await worker.dispatch({ type: "GET_CREDENTIAL_STATE" });
  assert.equal(state.hasCredential, true);
  assert.equal((await worker.dispatch({ type: "GET_STATE" })).credential.hasCredential, true);
});

test("an exported batch carries no credential", async () => {
  const { worker } = workerWith({
    base: HOSTED_BASE,
    sessionStorage: { [constants.CREDENTIAL_STORAGE.CAPTURE_CREDENTIAL]: CREDENTIAL },
  });
  const preview = await worker.dispatch({ type: "PREVIEW_PAYLOAD" });
  assert.equal(JSON.stringify(preview).includes(CREDENTIAL), false);
});

// --- 4. Shape checking a pasted value ----------------------------------------

for (const bad of [
  "",
  "   ",
  "not-a-credential",
  "vmrx1.beta-laptop",
  "vmrx1.beta-laptop.short",
  "vmrx0.beta-laptop.3fVQx8Zk2nLp7Rw6TyUiOaSdFgHjKlZxCvBnM4qWeRt",
  "beta-laptop:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
]) {
  test(`a paste of ${JSON.stringify(bad).slice(0, 40)} is refused at the field`, async () => {
    const { worker } = workerWith({ base: HOSTED_BASE });
    const r = await worker.dispatch({ type: "SET_CAPTURE_CREDENTIAL", credential: bad });
    assert.equal(r.ok, false);
    assert.equal(r.error, "credential_malformed");
    assert.equal(JSON.stringify(worker.sessionStore), "{}");
  });
}

test("the configuration digest is not mistaken for a credential", () => {
  // The mint script prints two lines. Pasting the wrong one must fail loudly
  // rather than being stored and producing a 401 on the next save.
  const digestEntry =
    "beta-laptop:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef";
  assert.equal(constants.CREDENTIAL_PATTERN.test(digestEntry), false);
  assert.equal(constants.CREDENTIAL_PATTERN.test(CREDENTIAL), true);
});

// --- 5. What the backend's refusals look like to the operator ------------------

test("a hosted 401 blames the credential, not the local backend", async () => {
  const { worker } = workerWith({
    base: HOSTED_BASE,
    sessionStorage: { [constants.CREDENTIAL_STORAGE.CAPTURE_CREDENTIAL]: CREDENTIAL },
    response: { status: 401, body: { error: "unauthorized", status: 401 } },
  });
  const r = await worker.dispatch({ type: "SAVE_CONTACT", target: "backend" });
  assert.equal(r.ok, false);
  const described = handoff.describeSendError(r);
  assert.equal(described.code, "credential_rejected");
  assert.match(described.detail, /revoked/i);
  // The pre-existing wording for a bare `unauthorized` body talks about local
  // access and would be actively misleading here.
  assert.doesNotMatch(described.headline, /local access/i);
  assert.equal(described.canRetry, false);
});

test("a hosted 403 says the install is not approved", async () => {
  const described = handoff.describeSendError({
    error: "receiver_rejected",
    status: 403,
    body: { error: "unauthorized", status: 403 },
  });
  assert.equal(described.code, "extension_not_approved");
  assert.match(described.detail, /extension ID/i);
});

test("a network failure against hosted still reads as a transport problem", () => {
  const described = handoff.describeSendError({ error: "network_error" });
  assert.equal(described.code, "network_error");
  assert.equal(described.canRetry, true);
});

test("no classified send error echoes the credential", () => {
  const responses = [
    { ok: false, error: "credential_missing" },
    { ok: false, error: "receiver_rejected", status: 401, body: { error: "unauthorized" } },
    { ok: false, error: "receiver_rejected", status: 403, body: { error: "unauthorized" } },
    { ok: false, error: "network_error", message: "failed" },
  ];
  for (const response of responses) {
    const described = handoff.describeSendError(response);
    assert.equal(JSON.stringify(described).includes(CREDENTIAL), false);
  }
});

// --- 6. Company evidence stays local ------------------------------------------

test("a hosted company send is refused with a truthful reason", async () => {
  const { worker, calls } = workerWith({
    base: HOSTED_BASE,
    sessionStorage: { [constants.CREDENTIAL_STORAGE.CAPTURE_CREDENTIAL]: CREDENTIAL },
    storage: {
      [constants.PROFILE_STORAGE.DRAFT_COMPANY]: {
        clientCaptureId: "22222222-2222-4222-8222-222222222222",
        createdAt: "2026-01-01T00:00:00.000Z",
        extraction: {
          status: "ok",
          capturedAt: "2026-01-01T00:00:00.000Z",
          company: { company_linkedin_url: "https://www.linkedin.com/company/meridian" },
          missingSections: [],
          pageWarnings: [],
        },
      },
    },
  });
  const r = await worker.dispatch({ type: "COMPANY_SEND" });
  assert.equal(r.ok, false);
  assert.equal(r.error, "company_capture_local_only");
  assert.equal(calls.length, 0);
});
