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

  // A hosted deployment returns a link to the saved Contact on its own origin,
  // and that link is the whole point of the outcome card. Refusing it would not
  // make anything safer — the extension just sent the capture to that origin —
  // but it would silently drop the affordance and look like a backend bug.
  //
  // Read from `permissions` rather than restated, so there is one list of
  // approved deployments. Over HTTPS only, and still checked against the same
  // workbench path prefixes: a returned URL is untrusted input whatever its
  // host, and a deployment answering with a link to somewhere else is refused.
  function hostedHosts() {
    const perms =
      (typeof self !== "undefined" && self.SNCapture && self.SNCapture.permissions) ||
      (typeof require === "function" ? require("./permissions.js") : null);
    return (perms && perms.HOSTED_HOSTS) || new Set();
  }
  // The only workbench destinations the extension will open: the contact-first
  // capture and contact records, the legacy batch/profile pages, and the dev
  // mock receiver's workbench link.
  // How many per-contact detail rows one sanitized result keeps. A display and
  // recovery bound, NOT a statement about how many contacts were processed —
  // see `sanitizeContactSubmissionResult`, which reports both numbers so the two
  // can never be confused for each other.
  const MAX_RETAINED_RESULT_DETAILS = 500;

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
   * or an HTTPS URL on a named VMR deployment, pointing at a known workbench
   * path. Anything else (an unknown host, another scheme, an unexpected path)
   * is refused, so a malicious or mistaken backend response can never send the
   * operator somewhere the extension does not already talk to.
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
    const known =
      LOOPBACK_HOSTS.has(u.hostname) ||
      (u.protocol === "https:" && hostedHosts().has(u.hostname));
    if (!known) return false;
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
    // TWO NUMBERS, NEVER ONE. `resultsSeen` is how many people the backend
    // reported on; `results` is how many detail rows are kept for display. They
    // are equal for an ordinary submission and deliberately are not for a large
    // one, and a caller that has only the second will state the wrong total.
    return {
      submissionId: typeof b.submission_id === "string" ? b.submission_id : null,
      clientSubmissionId:
        typeof b.client_submission_id === "string" ? b.client_submission_id : null,
      alreadyReceived: b.already_received === true,
      receivedAt: typeof b.received_at === "string" ? b.received_at : null,
      counts: safeCounts,
      results: results.slice(0, MAX_RETAINED_RESULT_DETAILS).map((r) => ({
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
      resultsSeen: results.length,
      resultsRetained: Math.min(results.length, MAX_RETAINED_RESULT_DETAILS),
      resultsTruncated: results.length > MAX_RETAINED_RESULT_DETAILS,
      workbenchUrl: isOpenableWorkbenchUrl(b.operator_workbench_url)
        ? b.operator_workbench_url
        : null,
      submittedAt: typeof m.submittedAt === "string" ? m.submittedAt : null,
    };
  }

  // A hosted refusal is about this install's AUTHORIZATION, and the operator can
  // only act if they are told which of the causes went wrong. The backend answers
  // all of them with a deliberately identical body — it must not tell a caller
  // whether a session id exists — so the classification here is by status, and
  // each message names every cause rather than guessing one.
  //
  //   401: the middleware saw no usable authorization — expired, revoked, or (on
  //        a development install) a missing/wrong legacy credential.
  //   403: the authorization verified but this request is not permitted — a
  //        deployment whose approved-origin list does not name this install, or
  //        a route outside the capture contract.
  const CREDENTIAL_STATUS_MESSAGES = {
    401: {
      headline: "Hosted VMR did not accept this install's authorization.",
      detail:
        "The account link may have expired or been revoked. Sign in to VMR Outbound " +
        "again from the connection panel, then retry.",
    },
    403: {
      headline: "Hosted VMR refused this extension.",
      detail:
        "The authorization was read but this extension ID is not approved for capture. " +
        "Ask whoever administers the deployment to approve it, then retry.",
    },
  };

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
    // A chunk, not a capture. The extension never sends the whole reviewed set
    // in one request, so this can only mean one bounded chunk was still refused
    // — a backend limit lower than the one the chunk planner was built against.
    payload_too_large:
      "The backend refused a part of this save as too large. Its request limit is " +
      "lower than this extension expects — report it; retrying will not help.",
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
        return {
          code: "payload_too_large",
          headline: "A part of this save was too large to send.",
          detail: "Nothing about the capture can fix it — report it.",
          canRetry: false,
        };
      // The LOGICAL ceiling, refused locally before anything is transmitted.
      // It names the real number, because "too large" without one leaves the
      // operator guessing how many rows to remove.
      case "push_limit_exceeded":
        return {
          code: "push_limit_exceeded",
          headline:
            resp.limit && resp.count
              ? `One save may contain up to ${resp.limit} contacts — this capture has ${resp.count}.`
              : "This capture is larger than one save may contain.",
          detail: "Nothing was sent. Exclude some rows, or clear and capture again.",
          canRetry: false,
        };
      case "push_in_progress":
        return {
          code: "push_in_progress",
          headline: "This capture is still being saved.",
          detail: "Wait for it to finish. Nothing has been lost.",
          canRetry: false,
        };
      case "already_pushed":
        return {
          code: "already_pushed",
          headline: "This capture has already been saved.",
          detail: "Capture again, or clear it, before saving.",
          canRetry: false,
        };
      // A record so large that no single request could carry it. Refused at
      // plan time, named, and never retried — the alternative is a chunk that
      // fails identically for ever.
      case "record_too_large":
        return {
          code: "record_too_large",
          headline: "One captured contact was too large to send on its own.",
          detail: "It was left out and named; every other contact was unaffected.",
          canRetry: false,
        };
      case "chunk_payload_missing":
        return {
          code: "chunk_payload_missing",
          headline: "Part of this save is no longer in local storage.",
          detail: "Those contacts were not sent. Capture them again to save them.",
          canRetry: false,
        };
      case "origin_not_allowed":
        return { code: "origin_not_allowed", headline: "Send target must be loopback (127.0.0.1 / localhost) or an approved VMR deployment.", detail: "", canRetry: false };
      case "permission_denied":
        return { code: "permission_denied", headline: "Access to the send target was not granted. Approve the permission prompt, then retry.", detail: "", canRetry: true };
      // The account link, not a credential: nothing was sent, and the same
      // reviewed draft retries unchanged after one sign-in.
      case "account_link_required":
        return {
          code: "account_link_required",
          headline: "Sign in to VMR Outbound to capture.",
          detail:
            "VM Prospector saves into your own VMR Outbound account. Signing in takes one " +
            "click and is remembered — including after you restart Chrome.",
          canRetry: true,
        };
      case "account_link_unavailable":
        return {
          code: "account_link_unavailable",
          headline: "VMR Outbound could not authorise this capture just now.",
          detail: "Nothing was sent. Try again shortly.",
          canRetry: true,
        };
      // --- account link failures, by category ------------------------------
      //
      // These replaced one catch-all ("The window was closed, or VMR Outbound
      // declined this install"), which named two unrelated causes at once and
      // was wrong about both whenever the real cause was a third thing. Each
      // category below is decided by `account-link.js` from a status code, a
      // server-chosen error name, or Chrome's description of the auth window —
      // never from a code, token or verifier, so none of these strings can
      // carry credential material. `sign_in_failed` remains the generic case
      // and is deliberately vague.
      case "sign_in_cancelled":
        return {
          code: "sign_in_cancelled",
          headline: "Sign-in was cancelled.",
          detail: "The sign-in window closed before it finished. Nothing changed — try again.",
          canRetry: true,
        };
      case "sign_in_declined":
        return {
          code: "sign_in_declined",
          headline: "The connection was declined.",
          detail:
            "VMR Outbound was asked to connect this extension and the request was refused. " +
            "Try again and choose Connect extension.",
          canRetry: true,
        };
      case "sign_in_incomplete":
        return {
          code: "sign_in_incomplete",
          headline: "Sign-in did not finish.",
          detail:
            "The window came back without completing the connection. If you were asked to " +
            "sign in to VMR Outbound, finish that first, then try again.",
          canRetry: true,
        };
      case "authorization_expired":
        return {
          code: "authorization_expired",
          headline: "The sign-in request expired.",
          detail: "It is only valid for a minute. Try again and complete it without pausing.",
          canRetry: true,
        };
      case "extension_not_authorized":
        return {
          code: "extension_not_authorized",
          headline: "This install is not approved for this VMR deployment.",
          detail:
            "Ask whoever administers the deployment to approve this extension, then try " +
            "again. Retrying now will fail the same way.",
          canRetry: false,
        };
      case "account_link_revoked":
        return {
          code: "account_link_revoked",
          headline: "This browser is no longer connected to VMR Outbound.",
          detail:
            "The connection was disconnected, expired, or the account was disabled. " +
            "Sign in again to reconnect.",
          canRetry: true,
        };
      case "backend_unreachable":
        return {
          code: "backend_unreachable",
          headline: "VMR Outbound could not be reached.",
          detail: "Check your connection, then try again. Nothing was sent.",
          canRetry: true,
        };
      case "token_endpoint_error":
        return {
          code: "token_endpoint_error",
          headline: "VMR Outbound could not complete the sign-in just now.",
          detail: "The server reported a problem. Nothing changed — try again shortly.",
          canRetry: true,
        };
      case "state_mismatch":
        return {
          code: "state_mismatch",
          headline: "The sign-in response did not match this request.",
          detail: "It was discarded and nothing was connected. Try again.",
          canRetry: true,
        };
      case "identity_unavailable":
        return {
          code: "identity_unavailable",
          headline: "This browser cannot open the VMR sign-in window.",
          detail: "Chrome 116 or newer is required to connect an account.",
          canRetry: false,
        };
      case "sign_in_failed":
        return {
          code: "sign_in_failed",
          headline: "Sign-in did not complete.",
          detail: "Nothing was connected and nothing was sent. Try again.",
          canRetry: true,
        };
      case "account_link_not_hosted":
        return {
          code: "account_link_not_hosted",
          headline: "This install is pointed at a development backend.",
          detail: "Account linking applies to hosted VMR only.",
          canRetry: false,
        };
      // Development path only: the gate is on, and neither an account link nor a
      // legacy credential is configured. An ordinary install cannot reach this —
      // it is told to sign in instead.
      case "credential_missing":
        return {
          code: "credential_missing",
          headline: "This development install has no capture credential.",
          detail:
            "Sign in to VMR Outbound, or set a credential under the development overrides " +
            "in Settings, then save again.",
          // Nothing was sent, and the same reviewed draft retries unchanged once
          // one of the two is in place.
          canRetry: true,
        };
      case "credential_malformed":
        return { code: "credential_malformed", headline: "That does not look like a VMR capture credential.", detail: "It should start with \"vmrx1.\". Copy the whole line you were issued.", canRetry: false };
      case "credential_storage_unavailable":
        return { code: "credential_storage_unavailable", headline: "This browser cannot hold the capture credential securely.", detail: "Chrome 116 or newer is required.", canRetry: false };
      case "dev_mode_required":
        return {
          code: "dev_mode_required",
          headline: "Capture credentials belong to development builds only.",
          detail: "This install captures under your VMR Outbound account instead.",
          canRetry: false,
        };
      case "company_capture_local_only":
        return { code: "company_capture_local_only", headline: "Company evidence capture is local-backend only.", detail: "Contact capture works against hosted VMR; company evidence does not yet.", canRetry: false };
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
        // Status first for the two authentication refusals. The backend body is
        // the same bare `unauthorized` for every cause, so mapping it through
        // BACKEND_MESSAGES would produce "local access or origin not allowed" —
        // wording that is actively wrong on a hosted deployment and sends the
        // operator looking in the wrong place.
        const credentialFailure = CREDENTIAL_STATUS_MESSAGES[resp.status];
        if (credentialFailure) {
          return {
            code: resp.status === 401 ? "credential_rejected" : "extension_not_approved",
            headline: credentialFailure.headline,
            detail: credentialFailure.detail,
            // Retrying the same credential will fail the same way; the operator
            // has to change something first.
            canRetry: false,
          };
        }
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
    MAX_RETAINED_RESULT_DETAILS,
  };
});
