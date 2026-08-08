/**
 * Loopback host-permission helpers.
 *
 * Loopback backend/mock origins are OPTIONAL host permissions (manifest
 * `optional_host_permissions`). They are requested explicitly, with a user
 * gesture, from the side panel before the first backend/mock send — never held
 * ambiently. These pure helpers map a target URL to the optional-permission
 * origin pattern to request/check.
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

  // Hostnames permitted for the local backend / mock receiver.
  //
  // Exactly the hosts the manifest declares under `optional_host_permissions`.
  // The IPv6 loopback spellings (`[::1]`, `::1`) used to be accepted here and
  // yielded the pattern `http://[::1]/*`, which the manifest does not declare —
  // so `chrome.permissions.request` could never grant it and the send failed
  // with `permission_denied` after the operator had already been prompted. This
  // set and the manifest must stay in step: a host here that the manifest does
  // not declare is a target that validates and then always fails.
  const LOOPBACK_HOSTS = new Set(["127.0.0.1", "localhost"]);

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
   * The optional host-permission match pattern for a loopback target URL, e.g.
   * "http://127.0.0.1/*". Returns null for anything that is not a loopback URL —
   * the caller must refuse to send to a null pattern.
   */
  function originPatternForUrl(urlStr) {
    const u = parse(urlStr);
    if (!u || !isLoopbackUrl(urlStr)) return null;
    return `${u.protocol}//${u.hostname}/*`;
  }

  return { isLoopbackUrl, originPatternForUrl, LOOPBACK_HOSTS };
});
