/**
 * Backend handoff helpers: safe workbench-URL validation, recovery-state
 * sanitization, and stable error classification for the send flow (UI-010).
 *
 * These are pure, dependency-free functions so both the service worker and the
 * side panel share one implementation and the behaviour is unit-testable. They
 * never handle credentials, cookies, or raw page content, and they never surface
 * a raw response body (which could echo submitted values) — only stable codes,
 * counts, and short safe messages.
 *
 * UMD module -> Node CommonJS + self.SNCapture.handoff.
 */
(function (root, factory) {
  const g = typeof self !== "undefined" ? self : root;
  const isNode = typeof module !== "undefined" && module.exports;
  const mod = factory();
  if (isNode) module.exports = mod;
  g.SNCapture = Object.assign(g.SNCapture || {}, { handoff: mod });
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  const LOOPBACK_HOSTS = new Set(["127.0.0.1", "localhost", "[::1]", "::1"]);
  // The only workbench destinations the extension will open: the real backend
  // batch page and the dev mock receiver's workbench link.
  const WORKBENCH_PATH_PREFIXES = ["/imports/", "/workbench/"];

  /**
   * Whether a backend-returned URL may be opened as the operator workbench.
   * The returned URL is untrusted input: it must be an http(s) loopback origin
   * pointing at a known workbench path. Anything else (remote host, other
   * scheme, unexpected path) is refused so a malicious/mistaken backend response
   * can never redirect the operator off the local machine.
   */
  function isOpenableWorkbenchUrl(url) {
    if (typeof url !== "string" || url === "") return false;
    let u;
    try {
      u = new URL(url);
    } catch (_e) {
      return false;
    }
    if (u.protocol !== "http:" && u.protocol !== "https:") return false;
    if (!LOOPBACK_HOSTS.has(u.hostname)) return false;
    return WORKBENCH_PATH_PREFIXES.some((p) => u.pathname.startsWith(p));
  }

  /**
   * Reduce a successful staging response body to a small, safe, recoverable
   * summary. Stores only identifiers and counts plus the workbench URL when it
   * is openable — never the raw records or response body.
   */
  function sanitizeStageResult(body, meta) {
    const b = body || {};
    const m = meta || {};
    const url = b.operator_workbench_url || b.workbench_url || null;
    const warnings = Array.isArray(b.warnings) ? b.warnings.length : 0;
    return {
      stagingId: typeof b.staging_id === "string" ? b.staging_id : null,
      clientBatchId: typeof b.client_batch_id === "string" ? b.client_batch_id : null,
      recordCount: Number.isFinite(b.record_count) ? b.record_count : null,
      warningCount: warnings,
      alreadyReceived: b.already_received === true,
      expiresAt: typeof b.expires_at === "string" ? b.expires_at : null,
      workbenchUrl: isOpenableWorkbenchUrl(url) ? url : null,
      stagedAt: typeof m.stagedAt === "string" ? m.stagedAt : null,
      campaignId: typeof m.campaignId === "string" && m.campaignId ? m.campaignId : null,
    };
  }

  // Stable, PII-free classification of a send failure. `resp` is the service
  // worker's send result ({ ok:false, error, status?, body? }).
  const BACKEND_MESSAGES = {
    invalid_json: "The backend could not read the batch (invalid request). This is a bug — retry, and report it if it persists.",
    validation_failed: "The batch failed backend validation (unsupported or invalid contract).",
    campaign_invalid: "The selected campaign is invalid or unavailable. Choose a valid campaign and retry.",
    payload_too_large: "The batch is too large for the backend. Capture fewer records and retry.",
    unauthorized: "The backend refused the request (local access or origin not allowed).",
    timeout: "The backend timed out staging the batch. It may be busy — retry.",
    client_batch_id_conflict: "This batch was already staged with different content. Clear or re-capture the batch before sending new content.",
    rate_limited: "Too many attempts. Wait a moment, then retry.",
    internal_error: "The backend hit an unexpected error. Retry; the batch was not staged.",
  };

  function describeSendError(resp) {
    if (!resp) return { code: "unknown", headline: "Send failed.", detail: "", canRetry: true };
    switch (resp.error) {
      case "empty_batch":
        return { code: "empty_batch", headline: "Nothing to send — all records excluded or the batch is empty.", detail: "", canRetry: false };
      case "invalid_payload":
        return { code: "invalid_payload", headline: "The batch failed local validation before sending.", detail: `${(resp.messages || []).length} issue(s).`, canRetry: false };
      case "payload_too_large":
        return { code: "payload_too_large", headline: "The batch is too large to send. Capture fewer records.", detail: "", canRetry: false };
      case "origin_not_allowed":
        return { code: "origin_not_allowed", headline: "Send target must be a loopback (127.0.0.1 / localhost) URL.", detail: "", canRetry: false };
      case "permission_denied":
        return { code: "permission_denied", headline: "Loopback access was not granted. Approve the permission prompt, then retry.", detail: "", canRetry: true };
      case "timeout":
        return { code: "timeout", headline: "No response from the backend (timed out). Is it running?", detail: "", canRetry: true };
      case "network_error":
        return { code: "network_error", headline: "Could not reach the backend. Is it running on the configured loopback port?", detail: "", canRetry: true };
      case "receiver_rejected": {
        const backendCode = resp.body && typeof resp.body.error === "string" ? resp.body.error : null;
        const headline = (backendCode && BACKEND_MESSAGES[backendCode]) || `The backend rejected the batch (HTTP ${resp.status || "?"}).`;
        // Never surface the raw body. For validation, a bare count is enough.
        let detail = "";
        if (backendCode === "validation_failed" && resp.body && Array.isArray(resp.body.details)) {
          detail = `${resp.body.details.length} validation issue(s).`;
        }
        return { code: backendCode || "receiver_rejected", headline, detail, canRetry: backendCode !== "validation_failed" };
      }
      default:
        return { code: resp.error || "unknown", headline: "Send failed.", detail: "", canRetry: true };
    }
  }

  return { isOpenableWorkbenchUrl, sanitizeStageResult, describeSendError, WORKBENCH_PATH_PREFIXES };
});
