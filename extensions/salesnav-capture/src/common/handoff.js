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

  // Deliberately wider than `permissions.LOOPBACK_HOSTS`, and for a different
  // job. That set decides which host the operator may *configure as a send
  // target*, so it must match the manifest's optional host permissions exactly.
  // This set only decides whether a URL the backend *returned* is safe to open,
  // so it stays a plain "is this still on this machine" test and keeps the IPv6
  // spellings. Narrowing it would gain nothing and could refuse a legitimate
  // link from a backend whose OPERATOR_BASE_URL is written that way.
  const LOOPBACK_HOSTS = new Set(["127.0.0.1", "localhost", "[::1]", "::1"]);
  // The only workbench destinations the extension will open: the contact-first
  // capture and contact records, the legacy batch/profile pages, and the dev
  // mock receiver's workbench link.
  const WORKBENCH_PATH_PREFIXES = [
    "/contact-captures/",
    "/contacts/",
    "/imports/",
    "/workbench/",
    "/profiles/",
  ];

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

  /**
   * Reduce a successful profile-snapshot staging response to a small, safe,
   * recoverable summary (DAT-012). Stores only identifiers, the truthful
   * outcome, and the workbench URL when openable — never captured values.
   */
  function sanitizeProfileStageResult(body, meta) {
    const b = body || {};
    const m = meta || {};
    const url = b.operator_workbench_url || null;
    return {
      snapshotId: typeof b.snapshot_id === "string" ? b.snapshot_id : null,
      clientCaptureId: typeof b.client_capture_id === "string" ? b.client_capture_id : null,
      outcome: typeof b.outcome === "string" ? b.outcome : null,
      warningCount: Array.isArray(b.warnings) ? b.warnings.length : 0,
      alreadyReceived: b.already_received === true,
      receivedAt: typeof b.received_at === "string" ? b.received_at : null,
      workbenchUrl: isOpenableWorkbenchUrl(url) ? url : null,
      stagedAt: typeof m.stagedAt === "string" ? m.stagedAt : null,
      campaignId: typeof m.campaignId === "string" && m.campaignId ? m.campaignId : null,
    };
  }

  /**
   * Reduce a successful contact-capture submission to a small, safe, recoverable
   * summary (DAT-013). Stores identifiers, truthful counts, and per-capture
   * outcomes with openable record links only — never captured personal values.
   */
  function sanitizeContactSubmissionResult(body, meta) {
    const b = body || {};
    const m = meta || {};
    const counts = b.counts && typeof b.counts === "object" ? b.counts : {};
    const safeCounts = {};
    for (const [key, value] of Object.entries(counts)) {
      if (Number.isFinite(value)) safeCounts[key] = value;
    }
    const results = Array.isArray(b.results) ? b.results : [];
    return {
      submissionId: typeof b.submission_id === "string" ? b.submission_id : null,
      clientSubmissionId:
        typeof b.client_submission_id === "string" ? b.client_submission_id : null,
      alreadyReceived: b.already_received === true,
      receivedAt: typeof b.received_at === "string" ? b.received_at : null,
      counts: safeCounts,
      results: results.slice(0, 500).map((r) => ({
        outcome: typeof r.outcome === "string" ? r.outcome : null,
        captureUrl: isOpenableWorkbenchUrl(r.capture_url) ? r.capture_url : null,
        contactUrl: isOpenableWorkbenchUrl(r.contact_url) ? r.contact_url : null,
        reviewCandidateCount: Number.isFinite(r.review_candidate_count)
          ? r.review_candidate_count
          : 0,
        labelsApplied: Array.isArray(r.labels_applied) ? r.labels_applied.length : 0,
        campaignFiling:
          r.campaign_filing && typeof r.campaign_filing === "object"
            ? {
                status:
                  typeof r.campaign_filing.status === "string"
                    ? r.campaign_filing.status
                    : null,
                campaignContactId:
                  typeof r.campaign_filing.campaign_contact_id === "string"
                    ? r.campaign_filing.campaign_contact_id
                    : null,
                errorCode:
                  typeof r.campaign_filing.error_code === "string"
                    ? r.campaign_filing.error_code
                    : null,
              }
            : null,
      })),
      workbenchUrl: isOpenableWorkbenchUrl(b.operator_workbench_url)
        ? b.operator_workbench_url
        : null,
      submittedAt: typeof m.submittedAt === "string" ? m.submittedAt : null,
    };
  }

  // Stable, PII-free classification of a send failure. `resp` is the service
  // worker's send result ({ ok:false, error, status?, body? }).
  const BACKEND_MESSAGES = {
    invalid_json: "The backend could not read the batch (invalid request). This is a bug — retry, and report it if it persists.",
    validation_failed: "The batch failed backend validation (unsupported or invalid contract).",
    unsupported_contract:
      "The backend does not accept this capture contract. Reload the extension so it matches the backend version.",
    client_submission_id_conflict:
      "This submission was already saved with different content. Capture again before saving new content.",
    campaign_invalid: "The selected campaign is invalid or unavailable. Choose a valid campaign and retry.",
    payload_too_large: "The batch is too large for the backend. Capture fewer records and retry.",
    unauthorized: "The backend refused the request (local access or origin not allowed).",
    timeout: "The backend timed out staging the batch. It may be busy — retry.",
    client_batch_id_conflict: "This batch was already staged with different content. Clear or re-capture the batch before sending new content.",
    client_capture_id_conflict: "This capture was already staged with different content. Re-capture the profile before sending new content.",
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
      // `timeout` and `network_error` describe the transport, not the target.
      // The same two failures will be produced by any backend the extension is
      // ever pointed at, so the wording states what happened and what is known
      // (nothing was saved) instead of naming one deployment shape. Telling an
      // operator to check a loopback port is actively misleading when the real
      // cause is a slow backend, a wrong address or a rejected Host header.
      case "timeout":
        return { code: "timeout", headline: "The backend did not respond in time.", detail: "It may be busy or still starting up. Retry, or check the configured address.", canRetry: true };
      case "network_error":
        return { code: "network_error", headline: "Could not reach the backend at the configured address.", detail: "Check that it is running and that the address is correct.", canRetry: true };
      case "receiver_rejected": {
        const backendCode = resp.body && typeof resp.body.error === "string" ? resp.body.error : null;
        const headline = (backendCode && BACKEND_MESSAGES[backendCode]) || `The backend rejected the batch (HTTP ${resp.status || "?"}).`;
        // Never surface the raw body. For validation, a bare count is enough.
        let detail = "";
        if (backendCode === "validation_failed" && resp.body && Array.isArray(resp.body.details)) {
          detail = `${resp.body.details.length} validation issue(s).`;
        }
        return {
          code: backendCode || "receiver_rejected",
          headline,
          detail,
          canRetry:
            backendCode !== "validation_failed" &&
            backendCode !== "unsupported_contract" &&
            backendCode !== "client_capture_id_conflict" &&
            backendCode !== "client_submission_id_conflict",
        };
      }
      default:
        return { code: resp.error || "unknown", headline: "Send failed.", detail: "", canRetry: true };
    }
  }

  return {
    isOpenableWorkbenchUrl,
    sanitizeStageResult,
    sanitizeProfileStageResult,
    sanitizeContactSubmissionResult,
    describeSendError,
    WORKBENCH_PATH_PREFIXES,
  };
});
