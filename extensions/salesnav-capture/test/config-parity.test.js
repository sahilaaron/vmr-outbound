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
  ...constants.HOSTED_BACKEND_ORIGINS,
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

test("every accepted backend target maps to a declared host permission", () => {
  // Either list will do here — what must never happen is a target the validator
  // accepts that the manifest declares nowhere, which prompts the operator (or
  // silently does not) and then fails every send with `permission_denied`.
  // Which of the two lists each target belongs in is asserted below.
  const optional = new Set(manifest.optional_host_permissions || []);
  const required = new Set(manifest.host_permissions || []);
  assert.ok(optional.size > 0, "the manifest must declare the loopback permissions");

  const accepted = CANDIDATE_TARGETS.filter(validatorAccepts);
  assert.ok(accepted.length > 0, "the validator must still accept the documented local targets");

  for (const target of accepted) {
    const pattern = perms.originPatternForUrl(target + "/api/intake/contact-captures");
    assert.ok(pattern, `${target} is accepted by the validator but yields no permission pattern`);
    assert.ok(
      optional.has(pattern) || required.has(pattern),
      `${target} is accepted by the validator but its permission pattern ${pattern} is not ` +
        `declared in the manifest at all — it would fail every send with permission_denied`
    );
  }
});

test("the fixed hosted origin is a REQUIRED host permission, not an optional one", () => {
  // #280. As an optional permission this opened a Chrome dialog naming an
  // unfamiliar server at the moment the operator pressed "Sign in to VMR
  // Outbound", and dismissing it produced a click that did nothing visible. The
  // origin is product configuration, so there was never a decision to take.
  const optional = new Set(manifest.optional_host_permissions || []);
  const required = new Set(manifest.host_permissions || []);
  for (const origin of constants.HOSTED_BACKEND_ORIGINS) {
    const pattern = perms.originPatternForUrl(origin + "/api/intake/contact-captures");
    assert.ok(
      required.has(pattern),
      `${pattern} must be declared in manifest host_permissions so no runtime prompt is needed`
    );
    assert.ok(
      !optional.has(pattern),
      `${pattern} must NOT also be optional — two declarations of one origin is how the ` +
        `runtime prompt came back`
    );
    assert.equal(
      perms.requiresRuntimeGrant(origin + "/api/intake/contact-captures"),
      false,
      `${origin} must not be requested at runtime`
    );
  }
});

test("localhost development origins remain OPTIONAL and still requested at runtime", () => {
  const optional = new Set(manifest.optional_host_permissions || []);
  for (const target of ["http://127.0.0.1:8000", "http://localhost:8787"]) {
    const pattern = perms.originPatternForUrl(target + "/api/intake/contact-captures");
    assert.ok(optional.has(pattern), `${pattern} must stay in optional_host_permissions`);
    assert.equal(
      perms.requiresRuntimeGrant(target + "/api"),
      true,
      `${target} is a development origin and must still be asked for`
    );
  }
});

test("no wildcard or all-urls host permission is declared", () => {
  // Pinning exactly what the extension needs is what makes moving the hosted
  // origin into the required list a narrowing rather than a widening.
  const declared = [
    ...(manifest.host_permissions || []),
    ...(manifest.optional_host_permissions || []),
  ];
  assert.ok(declared.length > 0);
  for (const pattern of declared) {
    assert.notEqual(pattern, "<all_urls>", "<all_urls> must never be declared");
    assert.ok(
      !/^\w+:\/\/\*(\/|$)/.test(pattern) && !/^\*:\/\//.test(pattern),
      `${pattern} is a wildcard host pattern; every origin must be named exactly`
    );
    const host = pattern.replace(/^\w+:\/\//, "").replace(/\/.*$/, "");
    assert.ok(!host.includes("*"), `${pattern} wildcards its host; name the deployment exactly`);
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

test("remote targets other than the named deployments are still refused", () => {
  for (const target of ["https://vmr.example.com", "http://192.168.0.10:8000"]) {
    assert.equal(validatorAccepts(target), false, `${target} must not be an accepted target`);
  }
});

// --- hosted deployments -------------------------------------------------------
//
// A hosted target is where reviewed personal data goes over the Internet, under
// a bearer credential. Three things therefore have to agree, and each of them
// has failed silently in this file's history for the loopback pair: the origin
// validator, the permission-pattern helper, and the manifest.

test("every named hosted deployment is HTTPS, accepted, and declared", () => {
  const declared = new Set(manifest.host_permissions || []);
  assert.ok(
    constants.HOSTED_BACKEND_ORIGINS.length > 0,
    "at least one hosted deployment must be named, or hosted capture cannot work at all"
  );
  for (const origin of constants.HOSTED_BACKEND_ORIGINS) {
    assert.ok(origin.startsWith("https://"), `${origin} must be HTTPS: it carries a credential`);
    assert.equal(validatorAccepts(origin), true, `${origin} must pass the origin validator`);
    assert.equal(
      perms.isHostedUrl(origin + "/api/intake/contact-captures"),
      true,
      `${origin} must be recognised as hosted, or its requests carry no credential`
    );
    const pattern = perms.originPatternForUrl(origin + "/api/intake/contact-captures");
    assert.ok(declared.has(pattern), `${pattern} is not declared in host_permissions`);
  }
});

test("the hosted host set and the hosted origin list name the same deployments", () => {
  const fromOrigins = new Set(constants.HOSTED_BACKEND_ORIGINS.map((o) => new URL(o).hostname));
  assert.deepEqual(
    [...perms.HOSTED_HOSTS].sort(),
    [...fromOrigins].sort(),
    "a host in one list and not the other is either a target with no credential or a " +
      "credential with no target"
  );
});

test("a plaintext spelling of a hosted deployment is refused", () => {
  for (const host of perms.HOSTED_HOSTS) {
    const target = `http://${host}`;
    assert.equal(validatorAccepts(target), false, `${target} must not be an accepted target`);
    assert.equal(perms.isHostedUrl(target + "/api"), false);
    assert.equal(perms.originPatternForUrl(target + "/api"), null);
  }
});

test("loopback is not treated as hosted, so a local send carries no credential", () => {
  for (const target of ["http://127.0.0.1:8000", "http://localhost:8787"]) {
    assert.equal(perms.isHostedUrl(target + "/api"), false, `${target} must stay local`);
  }
});

// --- the product-configured backend, and what linking needs -------------------
//
// The backend used to be something an operator typed. It is product
// configuration now, so the default and the named deployment must be the same
// thing: a default that drifts from the approved list is a default that cannot
// be reached, and there is no longer a field to correct it in.

test("the default backend IS the named hosted deployment", () => {
  assert.equal(
    constants.DEFAULT_PREFERENCES.backendBaseUrl,
    constants.HOSTED_BACKEND_ORIGINS[0],
    "the ordinary default must be an approved hosted deployment: nothing in the panel " +
      "can change it, so a wrong value here is unrecoverable for the operator"
  );
  assert.equal(constants.DEFAULT_PREFERENCES.sendTarget, "backend");
});

test("the manifest declares the identity permission the account link needs", () => {
  assert.ok(
    (manifest.permissions || []).includes("identity"),
    "chrome.identity.launchWebAuthFlow is the whole sign-in path; without this " +
      "permission the extension cannot be linked to an account at all"
  );
  // And nothing else was widened while adding it. `downloads` left this set in
  // #280 along with the JSON/CSV export and the archived-draft download; the
  // extension has no remaining path that saves a file.
  assert.deepEqual(
    [...(manifest.permissions || [])].sort(),
    ["activeTab", "identity", "scripting", "sidePanel", "storage"],
    "the permission set must not grow beyond the identity permission"
  );
});

test("the downloads permission is gone, and so is every caller of it", () => {
  assert.ok(
    !(manifest.permissions || []).includes("downloads"),
    "nothing in the extension writes a file any more; the permission must not be requested"
  );
  const worker = fs.readFileSync(path.join(ROOT, "src", "background", "service-worker.js"), "utf8");
  const panel = fs.readFileSync(path.join(ROOT, "src", "sidepanel", "sidepanel.js"), "utf8");
  const html = fs.readFileSync(path.join(ROOT, "src", "sidepanel", "sidepanel.html"), "utf8");
  for (const [name, source] of [["service worker", worker], ["panel", panel]]) {
    assert.ok(
      !/chrome\.downloads/.test(source),
      `${name} still calls chrome.downloads, which the manifest no longer permits`
    );
  }
  assert.ok(!/EXPORT_BATCH|EXPORT_LEGACY_ARCHIVE/.test(worker + panel), "export messages remain");
  assert.ok(!/Download JSON|Download CSV|Download archived/i.test(html), "download controls remain");
});

test("the account-link endpoints live on the approved hosted origin", () => {
  for (const path of Object.values(constants.ACCOUNT_LINK_PATHS)) {
    const url = constants.HOSTED_BACKEND_ORIGINS[0] + path;
    assert.equal(validatorAccepts(constants.HOSTED_BACKEND_ORIGINS[0]), true);
    assert.equal(
      perms.originPatternForUrl(url),
      "https://" + new URL(url).hostname + "/*",
      `${path} must be reachable under a declared host permission`
    );
  }
});

// --- the credential format the backend actually verifies ----------------------

test("the credential pattern matches the scheme the backend parses", () => {
  const backend = fs.readFileSync(
    path.join(REPO_ROOT, "app", "core", "auth", "extension.py"),
    "utf8"
  );
  const scheme = /CREDENTIAL_SCHEME\s*=\s*"([^"]+)"/.exec(backend);
  assert.ok(scheme, "the backend must declare a credential scheme");
  assert.equal(
    constants.CREDENTIAL_SCHEME,
    scheme[1],
    "the extension and the backend must agree on the credential scheme, or every " +
      "well-formed credential is refused as malformed by one of them"
  );
  const minChars = /MIN_SECRET_CHARS\s*=\s*(\d+)/.exec(backend);
  assert.ok(minChars, "the backend must declare a minimum secret length");
  const shortSecret = `${scheme[1]}.beta-laptop.${"a".repeat(Number(minChars[1]) - 1)}`;
  assert.equal(
    constants.CREDENTIAL_PATTERN.test(shortSecret),
    false,
    "the extension must not accept a secret the backend will refuse for being too short"
  );
  const goodSecret = `${scheme[1]}.beta-laptop.${"a".repeat(Number(minChars[1]))}`;
  assert.equal(constants.CREDENTIAL_PATTERN.test(goodSecret), true);
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
