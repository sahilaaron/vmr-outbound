"use strict";
/**
 * Parity between things that must agree but live in different files.
 *
 * Each check here corresponds to a real drift that shipped:
 *
 *  - `package.json` sat at 2.0.0 while the manifest said 2.1.0. Harmless at
 *    runtime (there is no build step) and exactly the kind of thing that stops
 *    being harmless the first time a release is packaged.
 *
 *  - The backend-origin validator accepted `http://[::1]`, whose permission
 *    pattern `http://[::1]/*` the manifest never declared. The target passed
 *    validation, prompted the operator, and then failed every send with
 *    `permission_denied`. A target that validates but can never work is worse
 *    than one refused immediately.
 *
 *  - The client abort budget for the contact-capture POST was shorter than the
 *    server's own bounded budget for that request, so a slow-but-successful
 *    submission was reported to the operator as a timeout while the backend
 *    went on to commit it.
 *
 * These are cross-file invariants, so they are asserted by reading the other
 * file rather than by restating its value here — restating it is how they
 * drifted in the first place.
 */
const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("fs");
const path = require("path");
const { stripComments } = require("./strip-comments.js");

const ROOT = path.join(__dirname, "..");
const REPO_ROOT = path.join(ROOT, "..", "..");
const manifest = JSON.parse(fs.readFileSync(path.join(ROOT, "manifest.json"), "utf8"));
const pkg = JSON.parse(fs.readFileSync(path.join(ROOT, "package.json"), "utf8"));
const constants = require("../src/common/constants.js");
const perms = require("../src/common/permissions.js");

// --- version parity ----------------------------------------------------------

test("package.json version matches the manifest, which is authoritative", () => {
  assert.equal(
    pkg.version,
    manifest.version,
    "manifest.json is what Chrome and the backend see (the envelope's " +
      "extension_version comes from it); package.json must not drift from it"
  );
});

// --- send target vs. manifest permissions ------------------------------------

// http/https across loopback spellings and one clearly remote host. The point is
// the matrix, not any single row: whatever the validator accepts must be
// grantable.
const CANDIDATE_TARGETS = [
  "http://127.0.0.1:8000",
  "http://127.0.0.1",
  "http://localhost:8787",
  "http://localhost",
  "https://127.0.0.1:8000",
  "https://localhost:8000",
  "http://[::1]:8000",
  "http://[::1]",
  "https://[::1]:8000",
  "http://192.168.0.10:8000",
  "https://vmr.example.com",
];

function validatorAccepts(urlStr) {
  let origin;
  try {
    origin = new URL(urlStr).origin;
  } catch (_e) {
    return false;
  }
  return constants.ALLOWED_BACKEND_ORIGIN_PATTERNS.some((re) => re.test(origin));
}

test("every accepted backend target maps to a declared optional host permission", () => {
  const declared = new Set(manifest.optional_host_permissions || []);
  assert.ok(declared.size > 0, "the manifest must declare the loopback permissions");

  const accepted = CANDIDATE_TARGETS.filter(validatorAccepts);
  assert.ok(accepted.length > 0, "the validator must still accept the documented local targets");

  for (const target of accepted) {
    const pattern = perms.originPatternForUrl(target + "/api/intake/contact-captures");
    assert.ok(pattern, `${target} is accepted by the validator but yields no permission pattern`);
    assert.ok(
      declared.has(pattern),
      `${target} is accepted by the validator but its permission pattern ${pattern} is not ` +
        `declared in manifest optional_host_permissions — it would prompt the operator and ` +
        `then fail every send with permission_denied`
    );
  }
});

test("the IPv6 loopback spelling is refused, not half-supported", () => {
  // Refused by BOTH gates, so the operator is told immediately instead of being
  // prompted for a permission that cannot be granted.
  for (const target of ["http://[::1]:8000", "http://[::1]", "https://[::1]:8000"]) {
    assert.equal(validatorAccepts(target), false, `${target} must not pass the origin validator`);
    assert.equal(
      perms.originPatternForUrl(target + "/api"),
      null,
      `${target} must not produce a permission pattern`
    );
  }
  assert.equal(perms.isLoopbackUrl("http://[::1]:8000/"), false);
  assert.equal(perms.LOOPBACK_HOSTS.has("[::1]"), false);
  assert.equal(perms.LOOPBACK_HOSTS.has("::1"), false);
});

test("the documented local development targets still work", () => {
  // The removal above must not have narrowed anything an operator actually uses:
  // docs/DEVELOPMENT.md runs the backend on 127.0.0.1:8000 and the mock receiver
  // on 127.0.0.1:8787.
  for (const target of ["http://127.0.0.1:8000", "http://localhost:8000", "http://127.0.0.1:8787"]) {
    assert.equal(validatorAccepts(target), true, `${target} must remain an accepted target`);
  }
  assert.equal(validatorAccepts(constants.DEFAULT_PREFERENCES.backendBaseUrl), true);
  assert.equal(validatorAccepts(constants.DEFAULT_PREFERENCES.mockReceiverUrl), true);
});

test("remote targets are still refused by the origin validator", () => {
  for (const target of ["https://vmr.example.com", "http://192.168.0.10:8000"]) {
    assert.equal(validatorAccepts(target), false, `${target} must not be an accepted target`);
  }
});

// --- client abort budget vs. the server's own budget -------------------------

function workerConstant(name) {
  const source = stripComments(
    fs.readFileSync(path.join(ROOT, "src", "background", "service-worker.js"), "utf8")
  );
  const match = new RegExp(`const\\s+${name}\\s*=\\s*(\\d+)\\s*;`).exec(source);
  assert.ok(match, `${name} not found in the service worker`);
  return Number(match[1]);
}

function serverDefaultSeconds(field) {
  // Read the backend's declared default rather than copying the number here.
  const source = fs.readFileSync(path.join(REPO_ROOT, "app", "core", "config.py"), "utf8");
  const match = new RegExp(`${field}:\\s*float\\s*=\\s*Field\\(\\s*default=([\\d.]+)`).exec(source);
  assert.ok(match, `${field} default not found in app/core/config.py`);
  return Number(match[1]);
}

test("the contact-capture abort budget outlives the server's own budget", () => {
  const clientMs = workerConstant("CONTACT_CAPTURE_TIMEOUT_MS");
  const serverMs = serverDefaultSeconds("contact_capture_intake_timeout_seconds") * 1000;

  // The service enforces its budget cooperatively and via PostgreSQL
  // statement_timeout, rolling the submission back and returning 504 on breach.
  // Aborting first turns that truthful verdict into a guess, so the client must
  // wait strictly longer.
  assert.ok(
    clientMs > serverMs,
    `client aborts at ${clientMs}ms but the server may legitimately take ${serverMs}ms ` +
      `before returning its own 504`
  );
  // And not absurdly longer: a wedged connection should not hang the panel for
  // minutes. One server budget of headroom is the ceiling.
  assert.ok(clientMs <= serverMs * 2, `client budget ${clientMs}ms is unreasonably far past the server's`);
});

test("routes with no server-side budget keep the original short timeout", () => {
  // The reads and the company intake POST declare no wall-clock budget on the
  // server, so there is no contract that would justify waiting longer for them.
  assert.equal(workerConstant("SEND_TIMEOUT_MS"), 15000);
  const configSource = fs.readFileSync(path.join(REPO_ROOT, "app", "core", "config.py"), "utf8");
  assert.ok(
    !/linkedin_company_intake_timeout_seconds/.test(configSource),
    "the company intake route has grown a server-side budget; revisit its client timeout"
  );
});
