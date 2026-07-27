"use strict";
/**
 * Content-script injection parity.
 *
 * A content script can arrive on a page two ways:
 *   1. the manifest declares it, for pages loaded after install;
 *   2. the service worker injects it with chrome.scripting.executeScript, for a
 *      page that was ALREADY OPEN when the extension was installed or reloaded.
 *
 * Those are two separate lists of files, and nothing in the language keeps them
 * in step. DAT-018 added src/common/scroller.js to the manifest and not to the
 * fallback, so a Sales Navigator tab opened before a reload hit the shared
 * module guard in content-script.js and capture stopped working — silently,
 * because the guard's job is to fail quietly rather than half-run.
 *
 * These tests make that class of drift impossible to reintroduce:
 *   - every module a content script destructures off self.SNCapture must be in
 *     BOTH lists;
 *   - the two lists must agree exactly, including order;
 *   - the content script itself must come last.
 *
 * All parsing strips comments first, so a module named only in prose — such as
 * the comment explaining this very fix — can never satisfy an assertion.
 */
const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("fs");
const path = require("path");

const { stripComments } = require("./strip-comments.js");

const ROOT = path.join(__dirname, "..");

function read(rel) {
  return fs.readFileSync(path.join(ROOT, rel), "utf8");
}

/** CODE only. A naive regex stripper is unsafe here — see strip-comments.js. */
function codeOnly(src) {
  return stripComments(src);
}

const MANIFEST = JSON.parse(read("manifest.json"));
const WORKER = codeOnly(read("src/background/service-worker.js"));

/**
 * Every "src/..." path inside the first `files: [...]` (or bare `[...]`) that
 * follows `anchor`. The anchor must be CODE: comments are stripped before this
 * runs, so anchoring on a comment would never match — including the comment
 * that documents this very fix.
 */
function fileListAfter(anchor, label) {
  const at = WORKER.indexOf(anchor);
  assert.notEqual(at, -1, `could not find ${label} (anchor: ${anchor})`);
  const filesAt = WORKER.indexOf("files:", at);
  const from = filesAt !== -1 && filesAt - at < 600 ? filesAt : at;
  const open = WORKER.indexOf("[", from);
  const close = WORKER.indexOf("]", open);
  assert.ok(open !== -1 && close > open, `could not read the ${label} array`);
  const paths = WORKER.slice(open, close).match(/"(src\/[^"]+)"/g);
  assert.ok(paths && paths.length, `${label} contains no src/ paths`);
  return paths.map((s) => s.slice(1, -1));
}

/** The manifest content_scripts entry whose js list ends with `entryFile`. */
function manifestFilesFor(entryFile) {
  const entry = MANIFEST.content_scripts.find((cs) => cs.js.includes(entryFile));
  assert.ok(entry, `no manifest content_scripts entry loads ${entryFile}`);
  return entry.js;
}

/**
 * Modules a content script pulls off the shared namespace. Covers both
 * `const { a, b } = NS;` destructuring and direct `NS.thing` access, which is
 * how the guard clauses are written.
 */
function requiredModules(rel) {
  const src = codeOnly(read(rel));
  const found = new Set();
  const destructured = src.match(/const\s*\{([^}]*)\}\s*=\s*NS\s*;/g) || [];
  for (const block of destructured) {
    for (const name of block.replace(/.*\{|\}.*/g, "").split(",")) {
      const clean = name.split(":")[0].trim();
      if (clean) found.add(clean);
    }
  }
  for (const m of src.match(/\bNS\.([A-Za-z_$][\w$]*)/g) || []) {
    found.add(m.slice(3));
  }
  return [...found];
}

/** Map a module name (`scroller`) to the file that defines it. */
function moduleFile(name) {
  const candidate = `src/common/${name.replace(/[A-Z]/g, (c) => "-" + c.toLowerCase())}.js`;
  return fs.existsSync(path.join(ROOT, candidate)) ? candidate : null;
}

const SURFACES = [
  {
    label: "Sales Navigator results",
    entry: "src/content/content-script.js",
    fallback: () => fileListAfter("async function askContentScript", "the salesnav fallback list"),
  },
  {
    label: "LinkedIn profile",
    entry: "src/content/profile-content-script.js",
    fallback: () => fileListAfter("const PROFILE_CS_FILES", "PROFILE_CS_FILES"),
  },
  {
    label: "LinkedIn company",
    entry: "src/content/company-content-script.js",
    fallback: () => fileListAfter("const COMPANY_CS_FILES", "COMPANY_CS_FILES"),
  },
];

// --- the specific defect ------------------------------------------------------

test("the salesnav fallback injects scroller.js before the content script", () => {
  const files = fileListAfter("async function askContentScript", "the salesnav fallback list");
  const scroller = files.indexOf("src/common/scroller.js");
  const entry = files.indexOf("src/content/content-script.js");
  assert.notEqual(scroller, -1, "src/common/scroller.js is missing from the fallback injection");
  assert.notEqual(entry, -1, "the content script is missing from the fallback injection");
  assert.ok(scroller < entry, "scroller.js must be injected BEFORE content-script.js");
  assert.deepEqual(files, [
    "src/common/constants.js",
    "src/common/normalize.js",
    "src/common/extraction.js",
    "src/common/scroller.js",
    "src/content/content-script.js",
  ]);
});

// --- the general invariant ----------------------------------------------------

for (const surface of SURFACES) {
  test(`${surface.label}: every required shared module is in the fallback list`, () => {
    const files = surface.fallback();
    for (const name of requiredModules(surface.entry)) {
      const file = moduleFile(name);
      if (!file) continue; // not a file-backed module (e.g. a local alias)
      assert.ok(
        files.includes(file),
        `${surface.entry} requires NS.${name} but the fallback never injects ${file}`
      );
      assert.ok(
        files.indexOf(file) < files.indexOf(surface.entry),
        `${file} must be injected before ${surface.entry}`
      );
    }
  });

  test(`${surface.label}: the manifest and fallback lists agree exactly`, () => {
    // This is the invariant DAT-018 actually broke: the manifest was updated
    // and the fallback was not. Identical lists, identical order.
    assert.deepEqual(
      surface.fallback(),
      manifestFilesFor(surface.entry),
      `${surface.label}: manifest and executeScript injection lists have drifted`
    );
  });

  test(`${surface.label}: the content script is injected last`, () => {
    const files = surface.fallback();
    assert.equal(files[files.length - 1], surface.entry);
  });

  test(`${surface.label}: every injected file exists on disk`, () => {
    for (const file of surface.fallback()) {
      assert.ok(fs.existsSync(path.join(ROOT, file)), `injected file is missing: ${file}`);
    }
  });
}

// --- the guard that turns a missing module into a silent failure --------------

test("content scripts refuse to run when a required module is absent", () => {
  // The parity above matters because this guard fails QUIETLY by design — it
  // warns and returns rather than throwing. Without the guard a missing module
  // would at least be loud; with it, the tests are the only safety net.
  const src = codeOnly(read("src/content/content-script.js"));
  assert.match(src, /if\s*\(!NS[^)]*\)\s*\{/, "the shared-module guard has been removed");
  assert.ok(src.includes("NS.scroller"), "the guard must check for the scroller module");
});
