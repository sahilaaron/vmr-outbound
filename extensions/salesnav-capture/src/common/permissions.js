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
  const LOOPBACK_HOSTS = new Set(["127.0.0.1", "localhost", "[::1]", "::1"]);

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
