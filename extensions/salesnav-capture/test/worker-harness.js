"use strict";
/**
 * Service-worker test harness.
 *
 * Loads the REAL src/background/service-worker.js — through its own
 * `importScripts` list, so the shared modules are the shipped ones — into a vm
 * sandbox with a stubbed `chrome.*`. A test can then dispatch a message exactly
 * as the side panel does and assert what the worker does with it.
 *
 * Only the browser edges are faked: storage, tabs, messaging, scripting.
 */
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const BACKGROUND = path.join(__dirname, "..", "src", "background");

/**
 * @param {object} options
 *   tabs: array of tab objects returned by chrome.tabs.query
 *   onTabMessage: (tabId, message) => response | throws  (the content script)
 *   storage: initial chrome.storage.local contents
 *   sessionStorage: initial chrome.storage.session contents (null = no such API)
 *   onAuthFlow: (details) => redirect URL | throws  (chrome.identity, the
 *     hosted app's authorize endpoint and its redirect back to the extension).
 *     Absent means the flow cannot complete — which is exactly what a browser
 *     with nobody signed in does to `{interactive:false}`.
 */
function createWorker(options) {
  const o = options || {};
  const store = Object.assign({}, o.storage);
  // `chrome.storage.session` is a separate area with separate contents. Faking
  // it as an alias of `local` would hide the property the credential design
  // depends on: the credential is never written to the area that persists to
  // disk. `sessionStorage: null` models a browser without the API at all.
  const sessionStore =
    o.sessionStorage === null ? null : Object.assign({}, o.sessionStorage);
  const tabs = o.tabs || [];
  const tabMessages = [];
  const injected = [];

  function area(backing) {
    return {
      get: (keys) => {
        const out = {};
        const list = Array.isArray(keys) ? keys : keys == null ? Object.keys(backing) : [keys];
        for (const k of list) if (k in backing) out[k] = backing[k];
        return Promise.resolve(out);
      },
      set: (obj) => {
        Object.assign(backing, obj);
        return Promise.resolve();
      },
      remove: (keys) => {
        for (const k of Array.isArray(keys) ? keys : [keys]) delete backing[k];
        return Promise.resolve();
      },
      setAccessLevel: () => Promise.resolve(),
    };
  }

  const listeners = { message: [], installed: [], startup: [] };
  // Every launchWebAuthFlow the worker attempted, so a test can assert that a
  // restart re-authorized with ZERO prompts rather than merely that it worked.
  const authFlows = [];

  function addListener(bucket) {
    return { addListener: (fn) => listeners[bucket].push(fn) };
  }

  const chrome = {
    runtime: {
      lastError: null,
      id: "test-extension",
      getURL: (p) => `chrome-extension://test/${p}`,
      getManifest: () => ({ version: "2.0.0", name: "VM Prospector" }),
      onInstalled: addListener("installed"),
      onStartup: addListener("startup"),
      onMessage: addListener("message"),
      sendMessage: () => Promise.resolve(undefined),
    },
    storage: Object.assign(
      { local: area(store) },
      sessionStore === null ? {} : { session: area(sessionStore) }
    ),
    tabs: {
      query: () => Promise.resolve(tabs.slice()),
      sendMessage: (tabId, message) => {
        tabMessages.push({ tabId, message });
        if (!o.onTabMessage) return Promise.reject(new Error("no content script"));
        try {
          return Promise.resolve(o.onTabMessage(tabId, message));
        } catch (e) {
          return Promise.reject(e);
        }
      },
    },
    scripting: {
      executeScript: (arg) => {
        injected.push(arg);
        return Promise.resolve([]);
      },
    },
    permissions: {
      // `hostPermission: false` models an operator who has not approved the
      // optional origin yet — the worker never requests one itself.
      contains: () => Promise.resolve(o.hostPermission !== false),
      request: () => Promise.resolve(o.hostPermission !== false),
    },
    identity: {
      // Exactly what Chrome mints for an extension: the id it was loaded under.
      getRedirectURL: () => "https://test-extension.chromiumapp.org/",
      launchWebAuthFlow: (details) => {
        authFlows.push(details);
        if (!o.onAuthFlow) {
          return Promise.reject(new Error("user interaction required"));
        }
        try {
          return Promise.resolve(o.onAuthFlow(details));
        } catch (e) {
          return Promise.reject(e);
        }
      },
    },
    // No `downloads` stub: the extension dropped that permission in #280, and a
    // stub for an API the manifest no longer requests would let a reintroduced
    // call pass here and fail in a real browser.
    sidePanel: { setPanelBehavior: () => Promise.resolve() },
  };

  const sandbox = {
    chrome,
    console,
    setTimeout,
    clearTimeout,
    setInterval,
    clearInterval,
    URL,
    URLSearchParams,
    TextEncoder,
    TextDecoder,
    AbortController,
    fetch: o.fetch || (() => Promise.reject(new Error("no network in tests"))),
    crypto: globalThis.crypto,
    Blob: globalThis.Blob,
  };
  sandbox.self = sandbox;
  sandbox.globalThis = sandbox;
  sandbox.importScripts = (...files) => {
    for (const file of files) {
      const full = path.resolve(BACKGROUND, file);
      vm.runInContext(fs.readFileSync(full, "utf8"), context, { filename: full });
    }
  };

  const context = vm.createContext(sandbox);
  const workerFile = path.join(BACKGROUND, "service-worker.js");
  vm.runInContext(fs.readFileSync(workerFile, "utf8"), context, { filename: workerFile });

  /** Send a message the way the side panel does, and await the response. */
  function dispatch(message) {
    return new Promise((resolve, reject) => {
      if (!listeners.message.length) {
        reject(new Error("the worker registered no message listener"));
        return;
      }
      let settled = false;
      const respond = (value) => {
        if (settled) return;
        settled = true;
        resolve(value);
      };
      const kept = listeners.message[0](message, { id: "test" }, respond);
      if (kept !== true) {
        // Synchronous handler: give it a turn, then report what it sent.
        setTimeout(() => respond(undefined), 0);
      }
    });
  }

  return { chrome, dispatch, store, sessionStore, tabMessages, injected, authFlows, sandbox };
}

const SALES_TAB = {
  id: 7,
  active: true,
  url: "https://www.linkedin.com/sales/search/people",
  title: "Search",
};

const constants = require("../src/common/constants.js");

/**
 * What `chrome.storage` looks like on an install that is already linked to a
 * VMR Outbound account: a rotating refresh token on disk, a live access token in
 * memory. This is the ordinary state of a working install, so any test whose
 * subject is NOT authorization starts from it rather than re-deriving it.
 *
 * `expiresInMs: 0` models the state right after a browser restart — the access
 * token is gone and only the persisted refresh token remains.
 */
function linkedAccount(options) {
  const o = options || {};
  const expiresInMs = o.expiresInMs === undefined ? 15 * 60 * 1000 : o.expiresInMs;
  const local = {
    [constants.ACCOUNT_STORAGE.INSTALLATION_ID]: o.installationId || "11111111-2222-4333-8444-555555555555",
    [constants.ACCOUNT_STORAGE.ACCOUNT_LINK]: {
      sessionId: o.sessionId || "0123456789abcdef0123456789abcdef",
      refreshToken: o.refreshToken || "vmrr1.0123456789abcdef0123456789abcdef.RefreshSecretAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
      accountEmail: o.accountEmail || "operator@example.com",
      scope: "capture",
      linkedAt: "2026-01-01T00:00:00.000Z",
    },
  };
  const session = {};
  if (expiresInMs > 0) {
    session[constants.ACCOUNT_STORAGE.ACCESS_TOKEN] = {
      accessToken:
        o.accessToken || "vmre1.0123456789abcdef0123456789abcdef.AccessSecretAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
      expiresAt: Date.now() + expiresInMs,
    };
  }
  return { local, session };
}

module.exports = { createWorker, SALES_TAB, linkedAccount };
