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
 */
function createWorker(options) {
  const o = options || {};
  const store = Object.assign({}, o.storage);
  const tabs = o.tabs || [];
  const tabMessages = [];
  const injected = [];

  const listeners = { message: [], installed: [], startup: [] };

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
    storage: {
      local: {
        get: (keys) => {
          const out = {};
          const list = Array.isArray(keys) ? keys : keys == null ? Object.keys(store) : [keys];
          for (const k of list) if (k in store) out[k] = store[k];
          return Promise.resolve(out);
        },
        set: (obj) => {
          Object.assign(store, obj);
          return Promise.resolve();
        },
        remove: (keys) => {
          for (const k of Array.isArray(keys) ? keys : [keys]) delete store[k];
          return Promise.resolve();
        },
      },
    },
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
      contains: () => Promise.resolve(true),
      request: () => Promise.resolve(true),
    },
    downloads: { download: () => Promise.resolve(1) },
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

  return { chrome, dispatch, store, tabMessages, injected, sandbox };
}

const SALES_TAB = {
  id: 7,
  active: true,
  url: "https://www.linkedin.com/sales/search/people",
  title: "Search",
};

module.exports = { createWorker, SALES_TAB };
