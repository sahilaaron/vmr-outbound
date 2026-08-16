"use strict";
/**
 * Account linking — the whole reason this change exists.
 *
 * An operator used to have to paste a backend URL and a `vmrx1.<key id>.<secret>`
 * shared credential into the extension, and to do it AGAIN after every Chrome
 * restart, because the credential was session-only. That is the product blocker
 * these tests hold closed:
 *
 *   1. the ordinary panel offers nothing to type — no backend URL, no
 *      credential, no key id, no mock receiver, and no `vmrx1` anywhere;
 *   2. a browser restart re-authorizes from the persisted refresh token alone,
 *      with no window, no click and no typing;
 *   3. what IS persisted is a rotating, install-bound refresh token — never a
 *      permanent plaintext shared secret;
 *   4. the developer overrides that remain cannot be reached by an ordinary
 *      staging/production operator.
 *
 * Driven through the REAL service worker (test/worker-harness.js) and the REAL
 * side panel (test/panel-harness.js): the browser edges are stubbed, nothing
 * about the flow is re-implemented here.
 */
const { test } = require("node:test");
const assert = require("node:assert/strict");
const nodeCrypto = require("node:crypto");

const { createWorker, linkedAccount } = require("./worker-harness.js");
const { createPanel } = require("./panel-harness.js");
const constants = require("../src/common/constants.js");
const accountLinkModule = require("../src/common/account-link.js");

const HOSTED_BASE = constants.HOSTED_BACKEND_ORIGINS[0];
const LOOPBACK_BASE = "http://127.0.0.1:8000";
const TOKEN_URL = HOSTED_BASE + constants.ACCOUNT_LINK_PATHS.TOKEN;
const AUTHORIZE_URL = HOSTED_BASE + constants.ACCOUNT_LINK_PATHS.AUTHORIZE;
const REVOKE_URL = HOSTED_BASE + constants.ACCOUNT_LINK_PATHS.REVOKE;
const CAPTURE_URL = HOSTED_BASE + constants.CONTACT_CAPTURE_PATH;

const ACCESS_1 = "vmre1.0123456789abcdef0123456789abcdef.AccessOneAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA";
const ACCESS_2 = "vmre1.0123456789abcdef0123456789abcdef.AccessTwoBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB";
// The refresh token an install already holds, and the two the server rotates to.
const REFRESH_0 = "vmrr1.0123456789abcdef0123456789abcdef.RefreshZeroAAAAAAAAAAAAAAAAAAAAAAAAAAAAA";
const REFRESH_1 = "vmrr1.0123456789abcdef0123456789abcdef.RefreshOneAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA";
const REFRESH_2 = "vmrr1.0123456789abcdef0123456789abcdef.RefreshTwoBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB";

/** The gate that unlocks the developer overrides. Nothing writes this key. */
const DEV_GATE = { [constants.ACCOUNT_STORAGE.DEV_OVERRIDES]: { enabled: true } };

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

function jsonResponse(status, body) {
  return {
    ok: status < 400,
    status,
    text: () => Promise.resolve(JSON.stringify(body || {})),
    json: () => Promise.resolve(body || {}),
  };
}

/**
 * The hosted app, as far as the extension can tell: a token endpoint that
 * issues and ROTATES, a revoke endpoint, and the capture intake.
 *
 * `tokens` is the queue of pairs it will hand out, so a test can prove the
 * second refresh presents the token the first one returned.
 */
function hostedServer(options) {
  const o = options || {};
  const calls = [];
  const issued = o.tokens || [
    { access_token: ACCESS_1, refresh_token: REFRESH_1 },
    { access_token: ACCESS_2, refresh_token: REFRESH_2 },
  ];
  let next = 0;

  function fetchImpl(url, init) {
    const body = init && init.body ? JSON.parse(init.body) : null;
    calls.push({ url, init, body });
    if (url === TOKEN_URL) {
      if (o.tokenStatus && o.tokenStatus >= 400) {
        // `tokenError` lets a test choose which of the endpoint's three refusal
        // names comes back, because the extension classifies on it (#280).
        return Promise.resolve(
          jsonResponse(o.tokenStatus, { error: o.tokenError || "invalid_grant" })
        );
      }
      const pair = issued[Math.min(next, issued.length - 1)];
      next += 1;
      return Promise.resolve(
        jsonResponse(200, {
          access_token: pair.access_token,
          token_type: "Bearer",
          expires_in: 900,
          refresh_token: pair.refresh_token,
          scope: "capture",
          account: { email: o.accountEmail || "operator@example.com" },
        })
      );
    }
    if (url === REVOKE_URL) return Promise.resolve(jsonResponse(204, {}));
    return Promise.resolve(
      jsonResponse(o.captureStatus || 201, {
        submission_id: "01366e2e-0000-4000-8000-000000000001",
        client_submission_id: "77ae7ae0-0000-4000-8000-000000000001",
        already_received: false,
        counts: { submitted: 1, created: 1 },
        results: [{ outcome: "created" }],
      })
    );
  }

  return { fetchImpl, calls };
}

/** The hosted app answering an authorize request by redirecting back with a code. */
function redirectWithCode(details, code) {
  const url = new URL(details.url);
  const redirect = new URL(url.searchParams.get("redirect_uri"));
  redirect.searchParams.set("code", code || "auth-code-1");
  redirect.searchParams.set("state", url.searchParams.get("state"));
  return redirect.toString();
}

function hostedWorker(options) {
  const o = options || {};
  const server = o.server || hostedServer();
  const worker = createWorker({
    hostPermission: o.hostPermission,
    storage: Object.assign(
      {
        [constants.STORAGE.PREFERENCES]: {
          sendTarget: "backend",
          backendBaseUrl: o.base || HOSTED_BASE,
        },
        [constants.PROFILE_STORAGE.DRAFT_PROFILE]: profileDraft(),
      },
      o.storage
    ),
    sessionStorage: o.sessionStorage,
    fetch: server.fetchImpl,
    onAuthFlow: o.onAuthFlow,
  });
  return { worker, server };
}

function authHeader(call) {
  const headers = (call && call.init && call.init.headers) || {};
  return headers.Authorization || headers.authorization || null;
}

const tokenCalls = (server) => server.calls.filter((c) => c.url === TOKEN_URL);
const captureCalls = (server) => server.calls.filter((c) => c.url.startsWith(CAPTURE_URL));

// --- 1. Linking: PKCE, one exchange, nothing typed ---------------------------

test("signing in links the install with a correct PKCE exchange", async () => {
  const { worker, server } = hostedWorker({
    onAuthFlow: (details) => redirectWithCode(details),
  });

  const r = await worker.dispatch({ type: "CONNECT_ACCOUNT" });
  assert.equal(r.ok, true, JSON.stringify(r));
  assert.equal(r.account.connected, true);
  assert.equal(r.account.accountEmail, "operator@example.com");

  // The authorize request carries everything the server binds the grant to.
  assert.equal(worker.authFlows.length, 1);
  assert.equal(worker.authFlows[0].interactive, true);
  const authUrl = new URL(worker.authFlows[0].url);
  assert.equal(authUrl.origin + authUrl.pathname, AUTHORIZE_URL);
  assert.equal(authUrl.searchParams.get("extension_id"), "test-extension");
  assert.equal(authUrl.searchParams.get("code_challenge_method"), "S256");
  assert.equal(
    authUrl.searchParams.get("redirect_uri"),
    "https://test-extension.chromiumapp.org/"
  );
  const challenge = authUrl.searchParams.get("code_challenge");
  assert.match(challenge, /^[A-Za-z0-9_-]{43}$/, "the challenge must be 43-char base64url");
  const installationId = authUrl.searchParams.get("installation_id");
  assert.ok(installationId, "the grant is bound to this installation");
  assert.equal(
    worker.store[constants.ACCOUNT_STORAGE.INSTALLATION_ID],
    installationId,
    "the installation id is minted once and kept"
  );

  // The exchange proves possession of the verifier behind that challenge.
  const exchanges = tokenCalls(server);
  assert.equal(exchanges.length, 1);
  assert.equal(exchanges[0].body.grant_type, "authorization_code");
  assert.equal(exchanges[0].body.code, "auth-code-1");
  assert.equal(exchanges[0].body.extension_id, "test-extension");
  assert.equal(exchanges[0].body.installation_id, installationId);
  assert.equal(
    nodeCrypto.createHash("sha256").update(exchanges[0].body.code_verifier).digest("base64url"),
    challenge,
    "code_challenge must be base64url(sha256(code_verifier))"
  );
  // No ambient cookie is what makes this work — the code and the extension
  // origin are.
  assert.equal(exchanges[0].init.credentials, "omit");
});

test("a redirect whose state is not this flow's is never exchanged", async () => {
  const { worker, server } = hostedWorker({
    onAuthFlow: (details) => {
      const redirect = new URL(new URL(details.url).searchParams.get("redirect_uri"));
      redirect.searchParams.set("code", "attacker-code");
      redirect.searchParams.set("state", "not-the-state-we-minted");
      return redirect.toString();
    },
  });
  const r = await worker.dispatch({ type: "CONNECT_ACCOUNT" });
  assert.equal(r.ok, false);
  assert.equal(r.error, "state_mismatch");
  assert.equal(tokenCalls(server).length, 0, "a foreign code must not be exchanged");
  assert.equal(worker.store[constants.ACCOUNT_STORAGE.ACCOUNT_LINK], undefined);
});

test("a cold open connects silently when the operator is already signed in", async () => {
  const { worker } = hostedWorker({ onAuthFlow: (details) => redirectWithCode(details) });
  const r = await worker.dispatch({ type: "GET_ACCOUNT_STATE", autoConnect: true });
  assert.equal(r.account.connected, true);
  assert.equal(r.account.accountEmail, "operator@example.com");
  // The silent path is what makes "install, open the panel, capture" need no
  // click at all: no interactive window was ever opened.
  assert.equal(worker.authFlows.length, 1);
  assert.equal(worker.authFlows[0].interactive, false);
});

test("with nobody signed in, the panel is told to sign in and nothing is sent", async () => {
  // No onAuthFlow: the browser cannot complete a non-interactive flow, which is
  // exactly what a signed-out operator's browser does.
  const { worker, server } = hostedWorker({});
  const state = await worker.dispatch({ type: "GET_ACCOUNT_STATE", autoConnect: true });
  assert.equal(state.account.connected, false);
  assert.equal(state.account.accountEmail, null);

  const save = await worker.dispatch({ type: "SAVE_CONTACT", target: "backend" });
  assert.equal(save.ok, false);
  assert.equal(save.error, "account_link_required");
  assert.equal(captureCalls(server).length, 0, "nothing may leave the browser unauthorized");
});

test("an unapproved origin is reported as such, not as a failed sign-in", async () => {
  // The token exchange behind the sign-in window is a cross-origin fetch, so it
  // needs the optional host permission. Opening an auth window the exchange
  // cannot follow would teach the operator that signing in is broken.
  const { worker } = hostedWorker({
    hostPermission: false,
    onAuthFlow: (details) => redirectWithCode(details),
  });
  const auto = await worker.dispatch({ type: "GET_ACCOUNT_STATE", autoConnect: true });
  assert.equal(auto.account.connected, false);
  assert.equal(auto.reason, "permission_required");

  const r = await worker.dispatch({ type: "CONNECT_ACCOUNT" });
  assert.equal(r.ok, false);
  assert.equal(r.error, "permission_denied");
  assert.equal(worker.authFlows.length, 0, "no window is opened for a flow that cannot finish");
});

test("a signed-out panel open does not launch a burst of hidden auth windows", async () => {
  // Opening the panel asks several questions at once — link state, backend
  // probe, labels, campaigns — and each one reaches the same authorization
  // gate. Without a cooldown that is one hidden `launchWebAuthFlow` per
  // question, every time the panel is opened.
  const { worker } = hostedWorker({});
  await worker.dispatch({ type: "GET_ACCOUNT_STATE", autoConnect: true });
  await worker.dispatch({ type: "PROBE_BACKEND" });
  await worker.dispatch({ type: "FETCH_LABELS" });
  await worker.dispatch({ type: "FETCH_CAMPAIGNS" });
  assert.equal(worker.authFlows.length, 1, "one silent attempt per cooldown window, not four");
});

// --- 2. Restart: the persisted refresh token alone is enough -----------------

test("a browser restart re-authorizes from the refresh token with zero prompts", async () => {
  // The state Chrome leaves behind: chrome.storage.local survived, the
  // in-memory access token did not.
  const linked = linkedAccount({ expiresInMs: 0, refreshToken: REFRESH_0 });
  const { worker, server } = hostedWorker({
    storage: linked.local,
    sessionStorage: linked.session,
  });

  const r = await worker.dispatch({ type: "SAVE_CONTACT", target: "backend" });
  assert.equal(r.ok, true, JSON.stringify(r));

  // Nobody was asked for anything: no sign-in window, no typing, no credential.
  assert.equal(worker.authFlows.length, 0, "a restart must not prompt");

  const refreshes = tokenCalls(server);
  assert.equal(refreshes.length, 1);
  assert.equal(refreshes[0].body.grant_type, "refresh_token");
  assert.equal(refreshes[0].body.refresh_token, REFRESH_0);

  const capture = captureCalls(server);
  assert.equal(capture.length, 1);
  assert.equal(authHeader(capture[0]), "Bearer " + ACCESS_1);
  assert.equal(capture[0].init.credentials, "omit");
});

test("the rotated refresh token is persisted, so the next refresh uses it", async () => {
  const linked = linkedAccount({ expiresInMs: 0, refreshToken: REFRESH_0 });
  const { worker, server } = hostedWorker({
    storage: linked.local,
    sessionStorage: linked.session,
  });

  await worker.dispatch({ type: "SAVE_CONTACT", target: "backend" });
  assert.equal(
    worker.store[constants.ACCOUNT_STORAGE.ACCOUNT_LINK].refreshToken,
    REFRESH_1,
    "the server rotates on every use; failing to keep the new token strands the install"
  );

  // Wind the stored access token past its usable life and save again: the
  // second refresh must present the token the first one returned.
  worker.sessionStore[constants.ACCOUNT_STORAGE.ACCESS_TOKEN] = {
    accessToken: ACCESS_1,
    expiresAt: Date.now() - 1000,
  };
  await worker.dispatch({ type: "SAVE_CONTACT", target: "backend" });
  const refreshes = tokenCalls(server);
  assert.equal(refreshes.length, 2);
  assert.equal(refreshes[1].body.refresh_token, REFRESH_1);
  assert.equal(worker.authFlows.length, 0, "rotation must never need a human");
});

test("an access token close to expiry is refreshed before it is used", async () => {
  // 30 seconds left: usable by the clock, not usable by the time the request
  // lands. The client refreshes rather than racing it.
  const linked = linkedAccount({ expiresInMs: 30000, refreshToken: REFRESH_0 });
  const { worker, server } = hostedWorker({
    storage: linked.local,
    sessionStorage: linked.session,
  });
  await worker.dispatch({ type: "SAVE_CONTACT", target: "backend" });
  assert.equal(tokenCalls(server).length, 1);
  assert.equal(authHeader(captureCalls(server)[0]), "Bearer " + ACCESS_1);
});

test("a live access token is reused rather than refreshed on every request", async () => {
  const linked = linkedAccount({ refreshToken: REFRESH_1, accessToken: ACCESS_1 });
  const { worker, server } = hostedWorker({
    storage: linked.local,
    sessionStorage: linked.session,
  });
  await worker.dispatch({ type: "SAVE_CONTACT", target: "backend" });
  assert.equal(tokenCalls(server).length, 0);
  assert.equal(authHeader(captureCalls(server)[0]), "Bearer " + ACCESS_1);
});

test("a dead refresh token drops the link and asks for one sign-in", async () => {
  // Revoked server-side, expired, or already rotated: the grant is dead, and
  // retrying it forever would be worse than asking once.
  const server = hostedServer({ tokenStatus: 400 });
  const linked = linkedAccount({ expiresInMs: 0, refreshToken: REFRESH_0 });
  const { worker } = hostedWorker({
    server,
    storage: linked.local,
    sessionStorage: linked.session,
  });

  const r = await worker.dispatch({ type: "SAVE_CONTACT", target: "backend" });
  assert.equal(r.ok, false);
  assert.equal(r.error, "account_link_required");
  assert.equal(captureCalls(server).length, 0);
  assert.equal(
    worker.store[constants.ACCOUNT_STORAGE.ACCOUNT_LINK],
    undefined,
    "a dead link must not be kept and retried forever"
  );
  const state = await worker.dispatch({ type: "GET_ACCOUNT_STATE" });
  assert.equal(state.account.connected, false);
});

// --- 3. What is persisted, and what the panel may learn ----------------------

test("no vmrx1-style plaintext shared secret is persisted by the new flow", async () => {
  const { worker } = hostedWorker({ onAuthFlow: (details) => redirectWithCode(details) });
  await worker.dispatch({ type: "CONNECT_ACCOUNT" });

  const persisted = JSON.stringify(worker.store);
  assert.equal(persisted.includes("vmrx1"), false, "no legacy shared credential is stored");
  assert.equal(
    persisted.includes(ACCESS_1),
    false,
    "the access token is memory-only and must never reach disk"
  );
  assert.equal(
    worker.sessionStore[constants.ACCOUNT_STORAGE.ACCESS_TOKEN].accessToken,
    ACCESS_1
  );

  // What IS on disk is one rotating, install-bound refresh token — not a shared
  // secret: it belongs to this install, the server replaces it on every use, and
  // nobody ever sees or types it.
  const link = worker.store[constants.ACCOUNT_STORAGE.ACCOUNT_LINK];
  assert.equal(link.refreshToken, REFRESH_1);
  assert.ok(link.refreshToken.startsWith(constants.ACCOUNT_LINK.REFRESH_TOKEN_SCHEME + "."));
  assert.equal(link.accountEmail, "operator@example.com");
  assert.equal(link.scope, "capture");
});

test("no worker response ever carries a token", async () => {
  const linked = linkedAccount({ refreshToken: REFRESH_1, accessToken: ACCESS_1 });
  const { worker } = hostedWorker({
    storage: linked.local,
    sessionStorage: linked.session,
  });
  for (const message of [
    { type: "GET_STATE" },
    { type: "PROFILE_GET_STATE" },
    { type: "GET_ACCOUNT_STATE" },
    { type: "GET_ACCOUNT_STATE", autoConnect: true },
    { type: "PREVIEW_PAYLOAD" },
  ]) {
    const r = await worker.dispatch(message);
    const text = JSON.stringify(r);
    assert.equal(text.includes(ACCESS_1), false, `${message.type} leaked the access token`);
    assert.equal(text.includes(REFRESH_1), false, `${message.type} leaked the refresh token`);
  }
  // What the panel IS told: connected, and to whom.
  const state = await worker.dispatch({ type: "GET_STATE" });
  assert.equal(state.account.connected, true);
  assert.equal(state.account.accountEmail, "operator@example.com");
});

test("disconnecting revokes server-side and forgets the link locally", async () => {
  const linked = linkedAccount({ refreshToken: REFRESH_1, accessToken: ACCESS_1 });
  const { worker, server } = hostedWorker({
    storage: linked.local,
    sessionStorage: linked.session,
  });

  const r = await worker.dispatch({ type: "DISCONNECT_ACCOUNT" });
  assert.equal(r.ok, true);
  assert.equal(r.account.connected, false);

  const revokes = server.calls.filter((c) => c.url === REVOKE_URL);
  assert.equal(revokes.length, 1);
  assert.equal(authHeader(revokes[0]), "Bearer " + ACCESS_1);
  assert.equal(worker.store[constants.ACCOUNT_STORAGE.ACCOUNT_LINK], undefined);
  assert.equal(worker.sessionStore[constants.ACCOUNT_STORAGE.ACCESS_TOKEN], undefined);
});

// --- 4. What must NOT have changed -------------------------------------------

test("a loopback capture still carries no authorization at all", async () => {
  const linked = linkedAccount({ refreshToken: REFRESH_1, accessToken: ACCESS_1 });
  const server = hostedServer();
  const { worker } = hostedWorker({
    base: LOOPBACK_BASE,
    server,
    storage: linked.local,
    sessionStorage: linked.session,
  });
  const r = await worker.dispatch({ type: "SAVE_CONTACT", target: "backend" });
  assert.equal(r.ok, true, JSON.stringify(r));
  assert.equal(server.calls.length, 1);
  assert.equal(authHeader(server.calls[0]), null, "local development has no authenticated intake");
  assert.equal(tokenCalls(server).length, 0, "and needs no token minted for it");
});

test("company evidence still refuses to reach a hosted backend", async () => {
  const linked = linkedAccount({ refreshToken: REFRESH_1, accessToken: ACCESS_1 });
  const server = hostedServer();
  const { worker } = hostedWorker({
    server,
    storage: Object.assign({}, linked.local, {
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
    }),
    sessionStorage: linked.session,
  });
  const r = await worker.dispatch({ type: "COMPANY_SEND" });
  assert.equal(r.ok, false);
  // An account link is NOT permission to send company evidence to hosted VMR:
  // that surface is not in the capture contract, and the refusal is local.
  assert.equal(r.error, "company_capture_local_only");
  assert.equal(server.calls.length, 0);
});

// --- 5. The developer overrides are unreachable ------------------------------
//
// Exactly one thing unlocks them: an object at chrome.storage.local key
// `vmr_dev_overrides` with `enabled: true`. Nothing in the extension writes it —
// no panel control, no message handler, not install or startup — so it can only
// be created by hand from the extension's own devtools console on an unpacked
// build. These tests assert that gate from both sides.

test("an ordinary install cannot move the send target, even by message", async () => {
  const { worker } = hostedWorker({});
  const r = await worker.dispatch({
    type: "SET_PREFS",
    prefs: {
      backendBaseUrl: "http://127.0.0.1:8000",
      sendTarget: "mock",
      mockReceiverUrl: "http://127.0.0.1:8787/api/intake/contact-captures",
      maxRecordsPerBatch: 250,
    },
  });
  assert.equal(r.ok, true);
  // The product's deployment is where captures go, full stop...
  assert.equal(r.prefs.backendBaseUrl, HOSTED_BASE);
  assert.equal(r.prefs.sendTarget, "backend");
  // ...while an ordinary preference is still an ordinary preference.
  assert.equal(r.prefs.maxRecordsPerBatch, 250);
  assert.equal(worker.store[constants.STORAGE.PREFERENCES].backendBaseUrl, HOSTED_BASE);
});

test("an ordinary install cannot store a legacy capture credential", async () => {
  const { worker } = hostedWorker({});
  const r = await worker.dispatch({
    type: "SET_CAPTURE_CREDENTIAL",
    credential: "vmrx1.beta-laptop.3fVQx8Zk2nLp7Rw6TyUiOaSdFgHjKlZxCvBnM4qWeRt",
  });
  assert.equal(r.ok, false);
  assert.equal(r.error, "dev_mode_required");
  assert.equal(JSON.stringify(worker.sessionStore).includes("vmrx1"), false);
  const state = await worker.dispatch({ type: "GET_STATE" });
  assert.equal(state.dev.enabled, false);
});

test("behind the gate, a developer can point the extension at a local backend", async () => {
  const { worker } = hostedWorker({ storage: DEV_GATE });
  const r = await worker.dispatch({
    type: "SET_PREFS",
    prefs: { backendBaseUrl: LOOPBACK_BASE, sendTarget: "backend" },
  });
  assert.equal(r.prefs.backendBaseUrl, LOOPBACK_BASE);
  const state = await worker.dispatch({ type: "GET_STATE" });
  assert.equal(state.dev.enabled, true);
});

// --- 6. The panel an ordinary operator sees ----------------------------------

const ORDINARY_PREFS = {
  backendBaseUrl: HOSTED_BASE,
  sendTarget: "backend",
  mockReceiverUrl: "http://127.0.0.1:8787/api/intake/contact-captures",
  maxRecordsPerBatch: 500,
  recentLabels: [],
};

async function ordinaryPanel(overrides) {
  const p = await createPanel({
    responses: Object.assign(
      {
        GET_STATE: {
          ok: true,
          prefs: ORDINARY_PREFS,
          metadata: { labels: [], note: null },
          batchView: null,
          account: { connected: true, accountEmail: "operator@example.com" },
          dev: { enabled: false },
        },
        PROFILE_GET_STATE: { ok: true, prefs: ORDINARY_PREFS, draftView: null },
        COMPANY_GET_STATE: { ok: true, draftView: null },
        GET_ACCOUNT_STATE: {
          ok: true,
          account: { connected: true, accountEmail: "operator@example.com" },
        },
        FETCH_LABELS: { ok: true, labels: [] },
        PROBE_BACKEND: { ok: true, state: "connected" },
        DETECT_SURFACE: {
          ok: true,
          surface: constants.SURFACES.UNSUPPORTED,
          url: "https://example.com/",
        },
      },
      overrides
    ),
  });
  await p.flush();
  return p;
}

test("the ordinary panel has no backend, credential, key-id or mock-receiver control", async () => {
  const p = await ordinaryPanel();
  await p.click("settings-toggle");
  await p.flush();

  // Every control the operator used to have to fill in is gone, by id...
  for (const id of [
    "backend-url",
    "mock-url",
    "capture-credential",
    "credential-state",
    "credential-save",
    "credential-clear",
    "credential-feedback",
    "send-target",
    "extension-id",
  ]) {
    assert.equal(p.$(id), null, `the ordinary panel still has #${id}`);
  }

  // ...and by what is actually rendered. This is the grep the requirement asks
  // for: run over the whole panel, in the state an ordinary operator opens it in.
  const html = p.document.body.innerHTML;
  for (const forbidden of [
    /vmrx1/i,
    /capture credential/i,
    /backend base url/i,
    /mock receiver/i,
    /key[ -]?id/i,
    /api secret/i,
    /auth token/i,
    /where captures go/i,
  ]) {
    assert.ok(!forbidden.test(html), `the ordinary panel still renders ${forbidden}`);
  }
  // No backend address of any kind is on screen — not the hosted one it uses,
  // not a loopback one it does not.
  assert.ok(!html.includes(HOSTED_BASE), "the panel renders the backend URL");
  assert.ok(!html.includes("127.0.0.1"), "the panel renders a loopback URL");
  // And nothing is typeable: the only inputs left are capture options.
  const typed = Array.from(p.document.querySelectorAll("#settings input, #settings select"))
    .map((n) => n.id)
    .sort();
  assert.deepEqual(typed, ["max-records"]);
});

test("the ordinary panel states the connection and offers only sign-out", async () => {
  const p = await ordinaryPanel();
  await p.click("settings-toggle");
  await p.flush();
  assert.match(p.viewText(), /Connected to VMR Outbound/);
  assert.match(p.viewText(), /operator@example\.com/);
  assert.match(p.viewText(), /Disconnect/);
  assert.equal(p.$("account-connect").hidden, true);
});

test("a save that needs a sign-in offers the sign-in, then saves", async () => {
  // The recovery path an operator actually hits: their link expired while the
  // panel was open. Nothing was sent, the reviewed set is still there, and the
  // one action offered is the one that fixes it.
  let connected = false;
  const p = await ordinaryPanel({
    GET_STATE: {
      ok: true,
      prefs: ORDINARY_PREFS,
      metadata: { labels: [], note: null },
      batchView: {
        records: [
          {
            rawFullName: "Dana Whitfield",
            companyName: "Northwind Logistics",
            warnings: [],
            _excluded: false,
            _stableKey: "k1",
          },
        ],
        pagesCaptured: ["1"],
        summary: {
          total: 1,
          included: 1,
          excluded: 0,
          withMissingFields: 0,
          selectorFailures: 0,
          uncertainIdentity: 0,
        },
      },
      account: { connected: false, accountEmail: null },
      dev: { enabled: false },
    },
    GET_ACCOUNT_STATE: { ok: true, account: { connected: false, accountEmail: null } },
    DETECT_SURFACE: {
      ok: true,
      surface: constants.SURFACES.SALESNAV_PEOPLE_RESULTS,
      url: "https://www.linkedin.com/sales/search/people",
    },
    DETECT_ACTIVE_PAGE: {
      ok: true,
      page: {
        supported: true,
        url: "https://www.linkedin.com/sales/search/people",
        visibleCount: 1,
      },
    },
    CONNECT_ACCOUNT: () => {
      connected = true;
      return { ok: true, account: { connected: true, accountEmail: "operator@example.com" } };
    },
    SAVE_INCLUDED_CONTACTS: () =>
      connected
        ? { ok: true, result: { counts: { created: 1 }, results: [{ outcome: "created" }] } }
        : { ok: false, error: "account_link_required" },
  });

  await p.click("listings-review-btn");
  await p.click("save-btn");
  assert.match(p.viewText(), /Connect to VMR Outbound/);
  assert.match(p.viewText(), /Nothing was sent/);
  assert.equal(p.$("outcome-primary").textContent.trim(), "Sign in to VMR Outbound");
  assert.equal(p.connection(), "Sign in needed");

  await p.click("outcome-primary");
  await p.flush();
  assert.equal(p.sent.filter((m) => m.type === "CONNECT_ACCOUNT").length, 1);
  assert.match(p.viewText(), /Prospect saved/);
});

/** A signed-out panel pointed at whichever backend the caller names. */
async function signedOutPanelOn(prefs, permission) {
  return createPanel({
    responses: {
      GET_STATE: {
        ok: true,
        prefs,
        metadata: { labels: [], note: null },
        batchView: null,
        account: { connected: false, accountEmail: null },
        dev: { enabled: false },
      },
      PROFILE_GET_STATE: { ok: true, prefs, draftView: null },
      COMPANY_GET_STATE: { ok: true, draftView: null },
      GET_ACCOUNT_STATE: { ok: true, account: { connected: false, accountEmail: null } },
      FETCH_LABELS: { ok: true, labels: [] },
      DETECT_SURFACE: {
        ok: true,
        surface: constants.SURFACES.UNSUPPORTED,
        url: "https://example.com/",
      },
      CONNECT_ACCOUNT: { ok: false, error: "sign_in_cancelled" },
    },
    permission,
  });
}

test("the hosted sign-in asks for no runtime host permission and starts anyway (#280)", async () => {
  // The hosted origin moved from `optional_host_permissions` to
  // `host_permissions`. It is held from install, so the click must go straight
  // to the sign-in window — with a browser that would refuse a runtime grant.
  //
  // This is the no-op the UAT hit: as an optional permission, a dismissed
  // dialog left the operator with a message and no auth window, which reads
  // exactly like a button that does nothing.
  const p = await signedOutPanelOn(ORDINARY_PREFS, { granted: false, grantOnRequest: false });
  await p.flush();
  const before = p.permissionCalls.length;
  await p.click("signin-btn");

  assert.equal(
    p.sent.filter((m) => m.type === "CONNECT_ACCOUNT").length,
    1,
    "the sign-in must start: the host permission is already held"
  );
  assert.deepEqual(
    p.permissionCalls.slice(before).filter((c) => c.call === "request"),
    [],
    "no runtime permission may be requested for the fixed hosted deployment"
  );
});

test("the sign-in says it is opening a window before anything appears (#280)", async () => {
  // "First click produces an obvious transition, not an apparent no-op."
  const p = await signedOutPanelOn(ORDINARY_PREFS, { granted: false, grantOnRequest: false });
  await p.flush();
  let observed = "";
  await new Promise((resolve) => {
    p.responses.CONNECT_ACCOUNT = () => {
      observed = p.$("signin-message").textContent;
      resolve();
      return { ok: false, error: "sign_in_cancelled" };
    };
    p.click("signin-btn");
  });
  assert.match(observed, /Opening the VMR Outbound sign-in window/);
});

test("a development install on loopback still answers the optional-permission prompt", async () => {
  // The runtime request was not deleted, only narrowed to the origins that are
  // still optional. A developer pointed at 127.0.0.1 keeps the old behaviour.
  const localPrefs = Object.assign({}, ORDINARY_PREFS, {
    backendBaseUrl: "http://127.0.0.1:8000",
  });
  const p = await signedOutPanelOn(localPrefs, { granted: false, grantOnRequest: false });
  await p.flush();
  await p.click("signin-btn");
  assert.equal(
    p.sent.filter((m) => m.type === "CONNECT_ACCOUNT").length,
    0,
    "a local sign-in must not start when its token exchange cannot run"
  );
  assert.ok(
    p.permissionCalls.some(
      (c) => c.call === "request" && c.origins.includes("http://127.0.0.1/*")
    ),
    "the loopback origin must still be requested at runtime"
  );
  assert.match(p.$("signin-message").textContent, /Allow VM Prospector to reach/);
  assert.match(p.$("signin-message").textContent, /Nothing has been sent/);
});

test("the developer overrides are not merely hidden — they are not there", async () => {
  const p = await ordinaryPanel();
  await p.click("settings-toggle");
  await p.flush();
  const host = p.$("dev-settings");
  assert.ok(host, "the container exists so the development build has somewhere to build into");
  assert.equal(host.hidden, true);
  assert.equal(host.childNodes.length, 0, "an ordinary panel builds no development controls");
  assert.equal(host.textContent, "");
});

test("with the gate on, a development build gets its overrides back", async () => {
  const p = await ordinaryPanel({
    GET_STATE: {
      ok: true,
      prefs: ORDINARY_PREFS,
      metadata: { labels: [], note: null },
      batchView: null,
      account: { connected: true, accountEmail: "operator@example.com" },
      dev: { enabled: true },
    },
    GET_CREDENTIAL_STATE: { ok: true, hasCredential: false, storageAvailable: true },
  });
  await p.click("settings-toggle");
  await p.flush();
  const host = p.$("dev-settings");
  assert.equal(host.hidden, false);
  assert.match(host.textContent, /Development overrides/);
  assert.match(host.textContent, /Backend base URL/);
  assert.match(host.textContent, /Legacy capture credential/);
});

// --- 7. The module's own edges -----------------------------------------------

test("a hostile or truncated redirect yields no authorization, and never throws", () => {
  for (const bad of [
    null,
    "",
    "not a url",
    "https://test-extension.chromiumapp.org/",
    "https://test-extension.chromiumapp.org/?error=access_denied",
  ]) {
    const parsed = accountLinkModule.parseRedirect(bad);
    assert.equal(parsed.code, null, JSON.stringify(bad));
  }
  const ok = accountLinkModule.parseRedirect(
    "https://test-extension.chromiumapp.org/?code=abc&state=xyz"
  );
  assert.equal(ok.code, "abc");
  assert.equal(ok.state, "xyz");
});

test("a malformed token is parsed to nothing rather than raising", () => {
  assert.equal(accountLinkModule.sessionIdOf(null), null);
  assert.equal(accountLinkModule.sessionIdOf("vmre1"), null);
  assert.equal(accountLinkModule.sessionIdOf("vmre1.session.secret"), "session");
});

test("base64url encoding matches the encoding the server verifies against", () => {
  // The challenge is compared byte-for-byte against the server's own
  // base64url(sha256(verifier)); an alphabet or padding difference here would
  // fail every exchange.
  for (const sample of ["", "a", "ab", "abc", "the quick brown fox", "ÿþý"]) {
    const bytes = Buffer.from(sample, "utf8");
    assert.equal(
      accountLinkModule.base64Url(new Uint8Array(bytes)),
      bytes.toString("base64url"),
      sample
    );
  }
});

// --- 8. Safe failure categories (#280) ---------------------------------------
//
// Every interactive failure used to collapse into one message — "Sign-in did
// not complete. The window was closed, or VMR Outbound declined this install."
// It named two unrelated causes and was wrong about both whenever the real
// cause was a third thing, which is what the hosted UAT hit. These hold the
// replacement categories closed, and hold the line on what they may contain.

const handoff = require("../src/common/handoff.js");

/** The category one interactive connect attempt produces. */
async function connectFailure(options) {
  const { worker } = hostedWorker(options);
  const r = await worker.dispatch({ type: "CONNECT_ACCOUNT" });
  assert.equal(r.ok, false, "this helper is for failures only");
  return r.error;
}

test("a closed or cancelled auth window is reported as cancelled, not as a refusal", async () => {
  const error = await connectFailure({
    onAuthFlow: () => {
      throw new Error("The user did not approve access.");
    },
  });
  assert.equal(error, "sign_in_cancelled");
  const described = handoff.describeSendError({ error });
  assert.match(described.headline, /cancelled/i);
  assert.equal(described.canRetry, true);
});

test("a declined consent page is reported as declined", async () => {
  const error = await connectFailure({
    onAuthFlow: (details) => {
      const redirect = new URL(new URL(details.url).searchParams.get("redirect_uri"));
      redirect.searchParams.set("error", "access_denied");
      return redirect.toString();
    },
  });
  assert.equal(error, "sign_in_declined");
  assert.match(handoff.describeSendError({ error }).headline, /declined/i);
});

test("a window that returns without an authorization is reported as incomplete", async () => {
  // The shape the `next=` defect produced: the window came back, but never
  // carrying a code.
  const error = await connectFailure({
    onAuthFlow: (details) => new URL(details.url).searchParams.get("redirect_uri"),
  });
  assert.equal(error, "sign_in_incomplete");
  assert.match(handoff.describeSendError({ error }).headline, /did not finish/i);
});

test("a dead authorization code is reported as expired, not as a refused install", async () => {
  const error = await connectFailure({
    server: hostedServer({ tokenStatus: 400, tokenError: "invalid_grant" }),
    onAuthFlow: (details) => redirectWithCode(details),
  });
  assert.equal(error, "authorization_expired");
  assert.equal(handoff.describeSendError({ error }).canRetry, true);
});

test("an install this deployment does not approve is told so, and not to retry", async () => {
  for (const [status, named] of [
    [401, "unauthorized"],
    [400, "invalid_request"],
  ]) {
    const error = await connectFailure({
      server: hostedServer({ tokenStatus: status, tokenError: named }),
      onAuthFlow: (details) => redirectWithCode(details),
    });
    assert.equal(error, "extension_not_authorized", `${status} ${named}`);
    const described = handoff.describeSendError({ error });
    assert.match(described.headline, /not approved/i);
    assert.equal(described.canRetry, false, "retrying an unapproved install cannot help");
  }
});

test("a server error during the exchange is not read as a dead grant", async () => {
  const error = await connectFailure({
    server: hostedServer({ tokenStatus: 503, tokenError: "unauthorized" }),
    onAuthFlow: (details) => redirectWithCode(details),
  });
  assert.equal(error, "token_endpoint_error");
});

/** The account-link client over a plain in-memory store, for direct calls. */
function directLink(options) {
  const o = options || {};
  const local = Object.assign({}, o.local);
  const session = {};
  const area = (bag) => ({
    get: async (key) => (key in bag ? { [key]: bag[key] } : {}),
    set: async (values) => Object.assign(bag, values),
    remove: async (key) => {
      for (const k of [].concat(key)) delete bag[k];
    },
  });
  const link = accountLinkModule.createAccountLink({
    chrome: {
      storage: { local: area(local), session: area(session) },
      runtime: { id: "test-extension" },
      identity: { getRedirectURL: () => "https://test-extension.chromiumapp.org/" },
    },
    crypto: nodeCrypto.webcrypto,
    fetch: o.fetch,
    backendBaseUrl: async () => HOSTED_BASE,
  });
  return { link, local, session };
}

test("a revoked or disabled link is named as such, and the local link is dropped", async () => {
  // `refresh()` is internal to the worker's own token handling, so it is
  // exercised directly here rather than through a message that does not exist.
  const server = hostedServer({ tokenStatus: 400, tokenError: "invalid_grant" });
  const { link, local } = directLink({
    fetch: server.fetchImpl,
    local: {
      [constants.ACCOUNT_STORAGE.INSTALLATION_ID]: "11111111-2222-4333-8444-555555555555",
      [constants.ACCOUNT_STORAGE.ACCOUNT_LINK]: {
        sessionId: "0123456789abcdef0123456789abcdef",
        refreshToken: REFRESH_0,
        accountEmail: "operator@example.com",
        scope: "capture",
      },
    },
  });

  const r = await link.refresh();
  assert.equal(r.ok, false);
  assert.equal(r.error, "account_link_revoked");
  assert.equal(
    local[constants.ACCOUNT_STORAGE.ACCOUNT_LINK],
    undefined,
    "a dead grant must not leave a refresh token behind"
  );
  const described = handoff.describeSendError({ error: r.error });
  assert.match(described.headline, /no longer connected/i);
});

test("a server error on refresh keeps the link instead of signing the operator out", async () => {
  // The other half of the rule: only a *decided* refusal drops a link. A 5xx
  // means the server is unwell, not that this install lost its authorization.
  const server = hostedServer({ tokenStatus: 503, tokenError: "unauthorized" });
  const { link, local } = directLink({
    fetch: server.fetchImpl,
    local: {
      [constants.ACCOUNT_STORAGE.INSTALLATION_ID]: "11111111-2222-4333-8444-555555555555",
      [constants.ACCOUNT_STORAGE.ACCOUNT_LINK]: {
        sessionId: "0123456789abcdef0123456789abcdef",
        refreshToken: REFRESH_0,
        accountEmail: "operator@example.com",
        scope: "capture",
      },
    },
  });

  const r = await link.refresh();
  assert.equal(r.ok, false);
  assert.equal(r.error, "token_endpoint_error");
  assert.ok(
    local[constants.ACCOUNT_STORAGE.ACCOUNT_LINK],
    "a server-side failure must not throw away a working link"
  );
});

test("a SILENT attempt still reports only 'sign in required', whatever failed", async () => {
  // Categories are for the action an operator took. A background attempt that
  // could not complete without UI is the normal answer for "not linked yet"
  // and must not surface as an error at all.
  const { worker } = hostedWorker({});
  const r = await worker.dispatch({ type: "GET_ACCOUNT_STATE", autoConnect: true });
  assert.equal(r.ok, true);
  assert.equal(r.account.connected, false);
  assert.equal(r.reason, "account_link_required");
});

test("no failure category leaks a code, token, verifier or challenge", async () => {
  // The categories are derived from a status code, a server-chosen error name,
  // or Chrome's description of the WINDOW — never from credential material.
  // This walks the messages an operator can actually be shown and proves it.
  const SECRETS = [
    "auth-code-1",
    ACCESS_1,
    ACCESS_2,
    REFRESH_0,
    REFRESH_1,
    "code_verifier",
    "code_challenge",
  ];
  const categories = [
    "sign_in_cancelled",
    "sign_in_declined",
    "sign_in_incomplete",
    "authorization_expired",
    "extension_not_authorized",
    "account_link_revoked",
    "backend_unreachable",
    "token_endpoint_error",
    "state_mismatch",
    "identity_unavailable",
    "sign_in_failed",
  ];
  for (const error of categories) {
    const described = handoff.describeSendError({ error });
    const shown = `${described.headline} ${described.detail}`;
    assert.equal(described.code, error, `${error} must classify to itself`);
    assert.ok(described.headline, `${error} must have an operator-facing headline`);
    for (const secret of SECRETS) {
      assert.ok(!shown.includes(secret), `${error} must not mention ${secret}`);
    }
    assert.ok(!/vmre1\.|vmrr1\.|vmrx1\./.test(shown), `${error} must not show a token`);
    assert.ok(!/Bearer /.test(shown), `${error} must not show an authorization header`);
  }
});

test("an interactive failure the extension cannot classify stays generic", async () => {
  // The safe default. A wrong-but-specific explanation sends an operator to fix
  // something that was never broken.
  const error = await connectFailure({
    onAuthFlow: () => {
      throw new Error("something nobody has seen before");
    },
  });
  assert.equal(error, "sign_in_failed");
  const described = handoff.describeSendError({ error });
  assert.match(described.detail, /Nothing was connected/);
  assert.ok(
    !/window was closed|declined this install/i.test(described.detail),
    "the generic message must stop asserting two specific causes it cannot know"
  );
});

// --- 9. The live UAT failure: an application refusal is not an outage --------
//
// Reproduced against the hosted deployment on 2026-08-16. The operator was
// signed in to VMR Outbound in the same Chrome profile, clicked "Sign in to VMR
// Outbound", and ~90ms later read "VMR Outbound could not be reached." The
// deployment was up the whole time and answered every request put to it.
//
// The control flow, exactly:
//
//   connect({interactive:true})
//     -> chrome.identity.launchWebAuthFlow(GET /extension/authorize?...)
//     -> the app answers with a status of 400 or above
//     -> Chromium's WebAuthFlow calls that a failed load, destroys the window
//        before paint, and rejects with "Authorization page could not be
//        loaded."
//     -> the extension matched /could not be loaded/ and reported that THE
//        DEPLOYMENT WAS UNREACHABLE
//
// Chrome uses those same seven words when the server really is unreachable, so
// the message alone cannot decide it. These tests pin the two apart.

/** A hosted deployment whose token endpoint answers with one chosen status. */
function refusingServer(status, body) {
  const calls = [];
  function fetchImpl(url, init) {
    calls.push({ url, init, body: init && init.body ? JSON.parse(init.body) : null });
    if (url === TOKEN_URL) return Promise.resolve(jsonResponse(status, body || {}));
    return Promise.resolve(jsonResponse(204, {}));
  }
  return { fetchImpl, calls };
}

/** A deployment that is genuinely not there: every request throws. */
function deadServer() {
  const calls = [];
  function fetchImpl(url, init) {
    calls.push({ url, init });
    return Promise.reject(new TypeError("Failed to fetch"));
  }
  return { fetchImpl, calls };
}

/** Chrome's own words when the authorization page did not render. */
function pageLoadFailure() {
  throw new Error("Authorization page could not be loaded.");
}

test("an application refusal is NOT reported as an unreachable deployment", async () => {
  // THE LIVE DEFECT. The deployment answers -- it just will not deal with this
  // install, because the extension id is not one it approves (or account
  // linking is switched off). Both arrive as a 4xx on the authorization page,
  // and both used to be called a network outage.
  for (const status of [401, 403]) {
    const server = refusingServer(status, { error: "unauthorized" });
    const error = await connectFailure({ server, onAuthFlow: pageLoadFailure });

    assert.equal(
      error,
      "extension_not_authorized",
      `HTTP ${status} on the authorization page is a refusal, not an outage`
    );
    assert.notEqual(error, "backend_unreachable");

    const described = handoff.describeSendError({ error });
    assert.ok(
      !/could not be reached/i.test(described.headline),
      "an operator must not be sent to check a connection that was never the problem"
    );
    assert.match(described.headline, /not approved/i);
    // Retrying cannot help -- somebody has to approve the install first -- and
    // saying so is the whole value of getting the category right.
    assert.equal(described.canRetry, false);
  }
});

test("a deployment that really is unreachable is still backend_unreachable", async () => {
  // The other half. `backend_unreachable` did not become unreachable-in-name-
  // only: when nothing answers, it is still exactly the right word.
  const server = deadServer();
  const error = await connectFailure({ server, onAuthFlow: pageLoadFailure });

  assert.equal(error, "backend_unreachable");
  assert.match(handoff.describeSendError({ error }).headline, /could not be reached/i);
});

test("a deployment that answers with a server error is named as one", async () => {
  const server = refusingServer(503, {});
  const error = await connectFailure({ server, onAuthFlow: pageLoadFailure });
  assert.equal(error, "token_endpoint_error");
  assert.notEqual(error, "backend_unreachable");
});

test("a deployment that DOES approve this install stays generic", async () => {
  // It answered, and it knows this install -- so whatever stopped the
  // authorization page is something this extension cannot name. The safe
  // default, and deliberately not a guess.
  const server = refusingServer(400, { error: "invalid_request" });
  const error = await connectFailure({ server, onAuthFlow: pageLoadFailure });
  assert.equal(error, "sign_in_failed");
});

test("the reachability probe presents no credential and omits cookies", async () => {
  // It exists to be refused. If it ever carried a code, a verifier or a refresh
  // token it would be burning one to ask a question, and if it ever carried the
  // operator's VMR cookie it would be answering about a session the
  // authorization window does not have.
  const server = refusingServer(401, { error: "unauthorized" });
  await connectFailure({ server, onAuthFlow: pageLoadFailure });

  const probes = server.calls.filter((c) => c.url === TOKEN_URL);
  assert.equal(probes.length, 1, "one probe, not a retry loop");
  const probe = probes[0];
  assert.equal(probe.init.credentials, "omit");
  assert.equal(probe.init.method, "POST");
  for (const forbidden of ["code", "code_verifier", "refresh_token"]) {
    assert.ok(!(forbidden in probe.body), `the probe must not present ${forbidden}`);
  }
  assert.notEqual(probe.body.grant_type, "authorization_code");
  assert.notEqual(probe.body.grant_type, "refresh_token");
  // It still names itself, because that is the question being asked: does this
  // deployment approve THIS install?
  assert.ok(probe.body.extension_id);
  assert.ok(probe.body.installation_id);
  // And it never carries an Authorization header.
  assert.equal(authHeader(probe), null);
});

test("a cancelled window is still cancelled and never probes", async () => {
  // The operator closing the window is not a question about the deployment, so
  // nothing is asked of it.
  const server = refusingServer(401, { error: "unauthorized" });
  const error = await connectFailure({
    server,
    onAuthFlow: () => {
      throw new Error("The user did not approve access.");
    },
  });
  assert.equal(error, "sign_in_cancelled");
  assert.equal(server.calls.filter((c) => c.url === TOKEN_URL).length, 0);
});

test("no extension source reads an application session cookie", async () => {
  // The extension is a public OAuth client. Its authority is a PKCE code, a
  // rotating refresh token and an approved origin -- never the operator's VMR
  // session. Nothing in the shipped source may read one, ask Chrome for one, or
  // send one.
  const fs = require("node:fs");
  const path = require("node:path");
  const srcRoot = path.join(__dirname, "..", "src");

  function sources(dir) {
    const out = [];
    for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
      const full = path.join(dir, entry.name);
      if (entry.isDirectory()) out.push(...sources(full));
      else if (entry.name.endsWith(".js")) out.push(full);
    }
    return out;
  }

  for (const file of sources(srcRoot)) {
    const text = fs.readFileSync(file, "utf8");
    const where = path.relative(srcRoot, file);
    assert.ok(!/chrome\.cookies/.test(text), `${where} must not use chrome.cookies`);
    assert.ok(!/document\.cookie/.test(text), `${where} must not read document.cookie`);
    assert.ok(
      !/credentials\s*:\s*["'](include|same-origin)["']/.test(text),
      `${where} must not send ambient credentials`
    );
  }
});
