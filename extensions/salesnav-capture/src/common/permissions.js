/**
 * Backend host-permission helpers.
 *
 * There are two classes of backend origin, and the difference is the whole
 * point of this module:
 *
 *   REQUIRED  the hosted VMR deployment. Declared in the manifest's
 *             `host_permissions`, so it is granted at install time and is never
 *             requested at runtime.
 *   OPTIONAL  the loopback development origins. Declared in
 *             `optional_host_permissions` and requested explicitly, with a user
 *             gesture, before the first local send.
 *
 * The hosted origin used to be optional too, which made "Sign in to VMR
 * Outbound" open a Chrome permission prompt naming a server the operator has
 * never heard of, *before* anything visibly happened. Dismissing that prompt
 * left the panel with a message and no sign-in window at all — a click that
 * reads as a no-op. The origin is fixed product configuration, not an operator
 * choice, so nothing was being decided by asking: an install that cannot reach
 * the hosted deployment cannot do anything.
 *
 * The permission is not what protects the deployment. The extension holds an
 * account-linked token bound to one approved `chrome-extension://` origin, and
 * the server admits it to exactly four routes; a host permission decides which
 * addresses this extension may open a connection to, and pinning it to one
 * exact HTTPS host is narrower than the prompt that replaced it.
 *
 * UMD module -> Node CommonJS + self.SNCapture.permissions
 */
(function (root, factory) {
  const mod = factory();
  if (typeof module !== "undefined" && module.exports) module.exports = mod;
  const g = typeof self !== "undefined" ? self : root;
  g.SNCapture = Object.assign(g.SNCapture || {}, { permissions: mod });
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  // Hostnames permitted for the local backend / mock receiver. These are the
  // OPTIONAL ones — the only origins still requested at runtime.
  //
  // Exactly the hosts the manifest declares under `optional_host_permissions`.
  // The IPv6 loopback spellings (`[::1]`, `::1`) used to be accepted here and
  // yielded the pattern `http://[::1]/*`, which the manifest does not declare —
  // so `chrome.permissions.request` could never grant it and the send failed
  // with `permission_denied` after the operator had already been prompted. This
  // set and the manifest must stay in step: a host here that the manifest does
  // not declare is a target that validates and then always fails.
  const LOOPBACK_HOSTS = new Set(["127.0.0.1", "localhost"]);

  // The hosted deployments, https only. Same rule as the loopback set above — a
  // host named here that the manifest does not declare is a target that
  // validates and then always fails — except that these are declared under
  // `host_permissions` rather than `optional_host_permissions`, so they are held
  // from install. `test/config-parity.test.js` keeps the two sides in step and
  // fails if a hosted host slips back into the optional list.
  const HOSTED_HOSTS = new Set(["srv1885453.hstgr.cloud"]);

  function parse(urlStr) {
    try {
      return new URL(urlStr);
    } catch (_e) {
      return null;
    }
  }

  /** True only for an http(s) URL on a loopback host. */
  function isLoopbackUrl(urlStr) {
    const u = parse(urlStr);
    if (!u) return false;
    if (!/^https?:$/.test(u.protocol)) return false;
    return LOOPBACK_HOSTS.has(u.hostname);
  }

  /**
   * True only for an HTTPS URL on a named hosted deployment.
   *
   * This is the test the service worker uses to decide that a request needs the
   * capture credential, so it is deliberately the narrow one: not "is this
   * remote?", but "is this one of the deployments we ship a credential for?".
   */
  function isHostedUrl(urlStr) {
    const u = parse(urlStr);
    if (!u) return false;
    return u.protocol === "https:" && HOSTED_HOSTS.has(u.hostname);
  }

  /**
   * The optional host-permission match pattern for a target URL, e.g.
   * "http://127.0.0.1/*" or "https://srv1885453.hstgr.cloud/*". Returns null for
   * anything that is neither loopback nor a named hosted deployment — the caller
   * must refuse to send to a null pattern.
   */
  function originPatternForUrl(urlStr) {
    const u = parse(urlStr);
    if (!u || !(isLoopbackUrl(urlStr) || isHostedUrl(urlStr))) return null;
    return `${u.protocol}//${u.hostname}/*`;
  }

  /**
   * Whether reaching this URL still needs a runtime `chrome.permissions.request`.
   *
   * True only for the optional loopback origins. A hosted deployment is a
   * required host permission, so asking for it would either be answered
   * instantly by Chrome or — if the operator dismissed the dialog — refuse a
   * capability the install already holds.
   *
   * An unrecognised target answers `true`, which is the safe direction: it has
   * no pattern to request, so the caller's own `originPatternForUrl` check
   * refuses it rather than sending anywhere.
   */
  function requiresRuntimeGrant(urlStr) {
    return !isHostedUrl(urlStr);
  }

  return {
    isLoopbackUrl,
    isHostedUrl,
    originPatternForUrl,
    requiresRuntimeGrant,
    LOOPBACK_HOSTS,
    HOSTED_HOSTS,
  };
});
