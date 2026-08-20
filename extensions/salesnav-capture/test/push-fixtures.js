"use strict";
/**
 * Shared scaffolding for the durable-push tests.
 *
 * Extracted so the large-push suite and the resave / orphan-storage / cancel
 * suites all drive the SAME service worker through the SAME backend double.
 * A second copy of `recordingBackend` would be a second, weaker definition of
 * what the backend guarantees — and it was exactly a too-permissive double that
 * hid LP-001 (a `client_capture_id` is unique across the whole table, not per
 * submission).
 */
const assert = require("node:assert/strict");

const { createWorker, SALES_TAB, linkedAccount } = require("./worker-harness.js");
const constants = require("../src/common/constants.js");

const { CAPTURE_STATUS } = constants;

// ---- fixtures ---------------------------------------------------------------

/**
 * One Sales Navigator result row, shaped exactly as `extraction.extractPage`
 * emits it. `padding` inflates the row's visible metadata, which is how a
 * genuinely byte-heavy capture is modelled without inventing a new record shape.
 */
function row(i, padding) {
  const meta = [
    "Northwind Logistics International Holdings GmbH",
    "501-1,000 employees",
    "Logistics and Supply Chain",
  ];
  if (padding) meta.push("x".repeat(padding));
  return {
    firstName: "Alexandra",
    lastName: "Featherstonehaugh" + i,
    rawFullName: "Alexandra Featherstonehaugh" + i,
    title: "Senior Director of Revenue Operations and Strategic Partnerships, EMEA",
    companyName: "Northwind Logistics International Holdings GmbH",
    location: "Greater Munich Metropolitan Area, Bavaria, Germany",
    linkedinProfileUrl: "https://www.linkedin.com/in/alexandra-featherstonehaugh" + i,
    linkedinProfileUrlSource: "observed",
    linkedinMemberId: "ACwAAAB1x9k" + i,
    linkedinAliasUrl: "https://www.linkedin.com/in/ACwAAAB1x9k" + i,
    salesNavLeadUrl: "https://www.linkedin.com/sales/lead/ACwAAAB1x9k" + i + ",NAME_SEARCH,ab12",
    companyLinkedInUrl: "https://www.linkedin.com/company/northwind-logistics",
    salesNavCompanyUrl: "https://www.linkedin.com/sales/company/123456",
    visibleCompanyMetadata: meta,
    sourceSearchUrl: "https://www.linkedin.com/sales/search/people?keywords=ops&page=1",
    sourcePageNumber: 1,
    sourcePosition: i + 1,
    capturedAt: "2026-08-20T09:15:04.000Z",
    _stableKey: "https://www.linkedin.com/sales/lead/ACwAAAB1x9k" + i,
    warnings: [],
  };
}

function capturePage(count, padding, offset) {
  const start = offset || 0;
  const records = [];
  for (let i = 0; i < count; i += 1) records.push(row(start + i, padding));
  return {
    status: CAPTURE_STATUS.OK,
    records,
    pageWarnings: [],
    sourcePageNumber: 1,
    sourceSearchUrl: "https://www.linkedin.com/sales/search/people?keywords=ops&page=1",
    capturedAt: "2026-08-20T09:15:04.000Z",
    count,
    visibleCount: count,
    skipped: [],
    skippedCount: 0,
    scroll: null,
  };
}

/**
 * A recording backend that enforces the intake's OWN uniqueness rules.
 *
 * The rules mirrored here are the three from `app/services/captures/intake.py`
 * that a chunked client can actually violate, and nothing else — a stub that is
 * stricter than production would fail honest code:
 *
 *   1. `client_submission_id` + identical content  -> replay, HTTP 200.
 *      (`stage_contact_captures` compares `content_hash` and calls `_replay`.)
 *   2. `client_submission_id` + DIFFERENT content  -> 409
 *      `client_submission_id_conflict`.
 *   3. a `client_capture_id` already committed under ANOTHER submission -> 409
 *      `client_capture_id_conflict`. This is the rule the branch's original stub
 *      did not model, and the one LP-001 violates: a capture id is unique across
 *      the whole table, not per submission.
 *   4. a `client_capture_id` repeated inside ONE submission -> 422, matching
 *      `_check_capture_ids_unique`.
 *
 * A commit happens before the response is written, so a dropped response still
 * leaves the ids taken — which is exactly what makes rule 3 dangerous.
 */
function recordingBackend(options) {
  const o = options || {};
  const requests = [];
  const submissions = new Map();
  const captureIds = [];
  // client_capture_id -> the client_submission_id that owns it.
  const captureOwner = new Map();
  const conflicts = [];

  function contentHash(payload) {
    return JSON.stringify(payload);
  }

  async function fetchImpl(url, init) {
    const payload = JSON.parse(init.body);
    const entry = {
      url,
      bytes: Buffer.byteLength(init.body, "utf8"),
      contacts: payload.contacts.length,
      clientSubmissionId: payload.client_submission_id,
      campaignId: payload.campaign_id,
      captureIds: payload.contacts.map((c) => c.client_capture_id),
    };
    requests.push(entry);
    const decision = o.onRequest ? o.onRequest(entry, requests.length) : null;
    if (decision && decision.throw) throw new Error(decision.throw);

    const prior = submissions.get(payload.client_submission_id);
    const replay = prior !== undefined;
    if (replay && prior.hash !== contentHash(payload)) {
      conflicts.push({ kind: "client_submission_id_conflict", entry });
      return response(409, { error: "client_submission_id_conflict", status: 409 });
    }
    // A 4xx decision is the backend refusing DETERMINISTICALLY, before it writes
    // anything — `stage_contact_captures` validates the whole body before it
    // opens a transaction. A 5xx is modelled the other way round (commit, then
    // fail) because that is the case the client cannot distinguish from success
    // and must therefore be cautious about.
    if (decision && decision.status && decision.status < 500) {
      return response(decision.status, decision.body || { error: "validation_failed" });
    }
    if (!replay) {
      // Rule 4: within one submission.
      if (new Set(entry.captureIds).size !== entry.captureIds.length) {
        conflicts.push({ kind: "duplicate_capture_id_in_submission", entry });
        return response(422, { error: "validation_failed", status: 422 });
      }
      // Rule 3: across submissions. THE LP-001 RULE.
      const taken = entry.captureIds.filter((id) => captureOwner.has(id));
      if (taken.length) {
        conflicts.push({ kind: "client_capture_id_conflict", entry, taken });
        return response(409, {
          error: "client_capture_id_conflict",
          status: 409,
          details: taken.map((id) => `client_capture_id '${id}' already exists`),
        });
      }
      // The commit happens whether or not the caller ever sees the response —
      // which is the whole point of the "lost response" case.
      submissions.set(payload.client_submission_id, {
        entry,
        hash: contentHash(payload),
      });
      for (const id of entry.captureIds) {
        captureIds.push(id);
        captureOwner.set(id, payload.client_submission_id);
      }
    }
    if (decision && decision.dropResponse) throw new Error("network dropped the response");
    if (decision && decision.status) {
      return response(decision.status, decision.body || { error: "internal_error" });
    }
    return response(replay ? 200 : 201, {
      submission_id: "sub_" + payload.client_submission_id.slice(0, 8),
      client_submission_id: payload.client_submission_id,
      received_at: "2026-08-20T09:20:00.000Z",
      already_received: replay,
      counts: {
        submitted: payload.contacts.length,
        created: payload.contacts.length,
        campaign_filings_applied: payload.campaign_id ? payload.contacts.length : 0,
      },
      results: payload.contacts.map((c) => ({
        client_capture_id: c.client_capture_id,
        outcome: "created",
        capture_url: "https://srv1885453.hstgr.cloud/contact-captures/submissions/x",
        contact_url: "https://srv1885453.hstgr.cloud/contacts/" + c.client_capture_id.slice(0, 8),
        review_candidate_count: 0,
        labels_applied: [],
      })),
      operator_workbench_url: "https://srv1885453.hstgr.cloud/contact-captures/submissions/x",
    });
  }
  function response(status, body) {
    return {
      ok: status < 400,
      status,
      text: () => Promise.resolve(JSON.stringify(body)),
    };
  }
  return { fetchImpl, requests, submissions, captureIds, captureOwner, conflicts };
}

/** A worker with a linked account, an open Sales Navigator tab, and a backend. */
function worker(backend, storage) {
  const account = linkedAccount();
  return createWorker({
    tabs: [SALES_TAB],
    storage: Object.assign({}, account.local, storage || {}),
    sessionStorage: account.session,
    fetch: backend.fetchImpl,
    onTabMessage: () => {
      throw new Error("no capture in this test");
    },
  });
}

/** Capture `count` rows into the reviewed batch, then start the push. */
async function captureAndPush(w, count, padding) {
  // Pages of at most 500 so the harness's synthetic capture stays realistic.
  let done = 0;
  while (done < count) {
    const size = Math.min(500, count - done);
    w.sandbox.chrome.tabs.sendMessage = () => Promise.resolve(capturePage(size, padding, done));
    const r = await w.dispatch({ type: "CAPTURE_ACTIVE_PAGE" });
    assert.equal(r.ok, true, "capture failed: " + JSON.stringify(r).slice(0, 200));
    done += size;
  }
  return w.dispatch({ type: "SAVE_INCLUDED_CONTACTS" });
}

/** Wait until the push settles (or the deadline passes). No real clock waits. */
async function drain(w, options) {
  const o = options || {};
  const limit = o.turns || 400;
  for (let i = 0; i < limit; i += 1) {
    await w.settle(20);
    const state = await w.dispatch({ type: "PUSH_STATE" });
    if (!state.push) return state;
    if (!state.pushActive) return state;
    if (o.stopWhen && o.stopWhen(state)) return state;
  }
  return w.dispatch({ type: "PUSH_STATE" });
}


module.exports = {
  row,
  capturePage,
  recordingBackend,
  worker,
  captureAndPush,
  drain,
};
