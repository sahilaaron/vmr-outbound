#!/usr/bin/env node
/**
 * Local mock intake receiver for the VMR contact-capture extension.
 *
 * Implements the shape of `POST /api/intake/contact-captures` (see
 * docs/CONTACT_CAPTURE_CONTRACT.md) plus its two companion reads, so the
 * extension's save flow can be exercised without a running backend. The legacy
 * `POST /api/intake/sales-navigator/stage` shape is retained so a previously
 * captured batch can still be replayed against the mock.
 *
 * It stages only — it never creates contacts, and it holds nothing but the
 * client id -> response it minted, in memory, for idempotency.
 *
 * Dependency-free (node:http). Runnable as a CLI and importable by tests.
 *
 *   node tools/mock-receiver.js            # listen on 127.0.0.1:8787
 *   PORT=9000 node tools/mock-receiver.js
 *
 * Test hooks (never used in production; the real backend ignores them):
 *   - opts.delayMs           delay before responding (simulate slow/timeout)
 *   - opts.forceStatus       always respond with this HTTP status
 *   - request header "x-mock-force-status: 422"  per-request override
 */
"use strict";
const http = require("http");
const path = require("path");

const constants = require(path.join(__dirname, "..", "src", "common", "constants.js"));
const INTAKE_PATH = constants.INTAKE_PATH;
const CONTACT_CAPTURE_PATH = constants.CONTACT_CAPTURE_PATH;
const CONTACT_LABELS_PATH = constants.CONTACT_LABELS_PATH;
const CONTACT_LOOKUP_PATH = constants.CONTACT_LOOKUP_PATH;

// Minimal Campaign list for the optional filing selector.
const MOCK_CAMPAIGNS = [
  { id: "11111111-1111-4111-8111-111111111111", name: "Pilot — Q3 SaaS Ops", status: "draft" },
  { id: "22222222-2222-4222-8222-222222222222", name: "Manufacturing DACH", status: "draft" },
];

const MOCK_LABELS = [
  { slug: "healthcare", name: "Healthcare" },
  { slug: "venture-capital", name: "Venture Capital" },
];

function readBody(req, maxBytes) {
  return new Promise((resolve, reject) => {
    let size = 0;
    const chunks = [];
    req.on("data", (c) => {
      size += c.length;
      if (size > maxBytes) {
        reject(new Error("payload_too_large"));
        req.destroy();
        return;
      }
      chunks.push(c);
    });
    req.on("end", () => resolve(Buffer.concat(chunks).toString("utf8")));
    req.on("error", reject);
  });
}

function corsHeaders(origin) {
  return {
    "Access-Control-Allow-Origin": origin || "*",
    "Access-Control-Allow-Methods": "POST, GET, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type, Idempotency-Key, X-Client-Batch-Id, X-Mock-Force-Status",
    "Access-Control-Max-Age": "600",
  };
}

function json(res, status, obj, extraHeaders) {
  const headers = Object.assign({ "Content-Type": "application/json" }, extraHeaders || {});
  res.writeHead(status, headers);
  res.end(JSON.stringify(obj));
}

function validate(payload) {
  const errors = [];
  if (!payload || typeof payload !== "object") return ["body is not an object"];
  if (typeof payload.schema_version !== "string") errors.push("schema_version missing");
  if (typeof payload.client_batch_id !== "string" || !payload.client_batch_id) errors.push("client_batch_id missing");
  if (!Array.isArray(payload.records)) errors.push("records must be an array");
  else if (payload.records.length === 0) errors.push("records must not be empty");
  return errors;
}

function validateSubmission(payload) {
  const errors = [];
  if (!payload || typeof payload !== "object") return ["body is not an object"];
  if (payload.schema_version !== constants.CONTACT_CAPTURE_SCHEMA_VERSION) {
    errors.push("schema_version must be " + constants.CONTACT_CAPTURE_SCHEMA_VERSION);
  }
  if (typeof payload.client_submission_id !== "string" || !payload.client_submission_id) {
    errors.push("client_submission_id missing");
  }
  if (
    payload.campaign_id !== null &&
    (typeof payload.campaign_id !== "string" ||
      !/^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(
        payload.campaign_id
      ))
  ) {
    errors.push("campaign_id must be a UUID string or null");
  }
  if (!Array.isArray(payload.contacts)) errors.push("contacts must be an array");
  else if (payload.contacts.length === 0) errors.push("contacts must not be empty");
  return errors;
}

function createReceiver(opts) {
  const options = opts || {};
  const seen = new Map(); // client id -> response body
  const maxBytes = options.maxBytes || 6 * 1024 * 1024;

  const server = http.createServer(async (req, res) => {
    const origin = req.headers.origin;
    if (req.method === "OPTIONS") {
      res.writeHead(204, corsHeaders(origin));
      res.end();
      return;
    }
    const cors = corsHeaders(origin);
    const url = new URL(req.url, "http://127.0.0.1");

    // Simulated latency (timeout tests).
    if (options.delayMs) await new Promise((r) => setTimeout(r, options.delayMs));

    // Forced status (rejection tests).
    const forced = Number(req.headers["x-mock-force-status"]) || options.forceStatus;
    if (forced) {
      json(res, forced, { error: "forced_status", status: forced }, cors);
      return;
    }

    if (req.method === "GET" && url.pathname === "/api/campaigns") {
      json(res, 200, MOCK_CAMPAIGNS, cors);
      return;
    }

    if (req.method === "GET" && url.pathname === CONTACT_LABELS_PATH) {
      json(res, 200, { labels: MOCK_LABELS }, cors);
      return;
    }

    if (req.method === "GET" && url.pathname === CONTACT_LOOKUP_PATH) {
      // Existence only — the mock never invents a match.
      json(res, 200, { match: "none", contact_count: 0 }, cors);
      return;
    }

    if (req.method === "POST" && url.pathname === CONTACT_CAPTURE_PATH) {
      let raw;
      try {
        raw = await readBody(req, maxBytes);
      } catch (_e) {
        json(res, 413, { error: "payload_too_large" }, cors);
        return;
      }
      let payload;
      try {
        payload = JSON.parse(raw);
      } catch (_e) {
        json(res, 400, { error: "invalid_json" }, cors);
        return;
      }
      const errors = validateSubmission(payload);
      if (errors.length) {
        json(res, 422, { error: "validation_failed", details: errors }, cors);
        return;
      }
      if (seen.has(payload.client_submission_id)) {
        const prior = Object.assign({}, seen.get(payload.client_submission_id), {
          already_received: true,
        });
        json(res, 200, prior, cors);
        return;
      }
      const port = server.address() ? server.address().port : 8787;
      const submissionId = "sub_" + payload.client_submission_id.slice(0, 8);
      const filingRequested = typeof payload.campaign_id === "string";
      const results = payload.contacts.map((c) => {
        const captureId = "cap_" + String(c.client_capture_id).slice(0, 8);
        const contactId = "contact_" + String(c.client_capture_id).slice(0, 8);
        return {
          client_capture_id: c.client_capture_id,
          capture_id: captureId,
          // Mirror the create-only intake boundary without pretending later
          // identity or Company work has completed.
          outcome: "created",
          matched_contact_id: contactId,
          contact_url: `http://127.0.0.1:${port}/contacts/${contactId}`,
          capture_url: `http://127.0.0.1:${port}/contact-captures/${captureId}`,
          review_candidate_count: 0,
          labels_applied: [],
          campaign_filing: filingRequested
            ? {
                status: "pending",
                requested_campaign_id: payload.campaign_id,
                campaign_id: payload.campaign_id,
                campaign_contact_id: null,
                attempts: 0,
                error_code: null,
                error_detail: null,
              }
            : null,
          warnings: [],
        };
      });
      const body = {
        submission_id: submissionId,
        client_submission_id: payload.client_submission_id,
        received_at: new Date().toISOString(),
        already_received: false,
        counts: {
          submitted: payload.contacts.length,
          created: payload.contacts.length,
          refreshed_exact_match: 0,
          exact_match_unchanged: 0,
          staged_unmatched: 0,
          staged_ambiguous: 0,
          duplicate_in_submission: 0,
          suppressed: 0,
          labels_applied: 0,
          notes_recorded: payload.operator_metadata && payload.operator_metadata.note ? payload.contacts.length : 0,
          campaign_filings_applied: 0,
          campaign_filings_pending: filingRequested ? payload.contacts.length : 0,
          campaign_filings_failed: 0,
        },
        results,
        operator_workbench_url: `http://127.0.0.1:${port}/contact-captures/submissions/${submissionId}`,
      };
      seen.set(payload.client_submission_id, body);
      json(res, 201, body, cors);
      return;
    }

    if (req.method === "POST" && url.pathname === INTAKE_PATH) {
      let raw;
      try {
        raw = await readBody(req, maxBytes);
      } catch (_e) {
        json(res, 413, { error: "payload_too_large" }, cors);
        return;
      }
      let payload;
      try {
        payload = JSON.parse(raw);
      } catch (_e) {
        json(res, 400, { error: "invalid_json" }, cors);
        return;
      }
      const errors = validate(payload);
      if (errors.length) {
        json(res, 422, { error: "validation_failed", details: errors }, cors);
        return;
      }
      // Idempotency by client_batch_id.
      if (seen.has(payload.client_batch_id)) {
        const prior = Object.assign({}, seen.get(payload.client_batch_id), { already_received: true });
        json(res, 200, prior, cors);
        return;
      }
      const stagingId = "stg_" + payload.client_batch_id.slice(0, 8);
      const port = server.address() ? server.address().port : 8787;
      const body = {
        staging_id: stagingId,
        client_batch_id: payload.client_batch_id,
        record_count: payload.records.length,
        warnings: [],
        received_at: new Date().toISOString(),
        expires_at: new Date(Date.now() + 24 * 3600 * 1000).toISOString(),
        operator_workbench_url: `http://127.0.0.1:${port}/workbench/imports/${stagingId}`,
        already_received: false,
      };
      seen.set(payload.client_batch_id, body);
      json(res, 201, body, cors);
      return;
    }

    json(res, 404, { error: "not_found", path: url.pathname }, cors);
  });

  server._seen = seen; // exposed for tests
  return server;
}

module.exports = { createReceiver, MOCK_CAMPAIGNS, MOCK_LABELS };

// CLI
if (require.main === module) {
  const port = Number(process.env.PORT) || 8787;
  const host = process.env.HOST || "127.0.0.1";
  const server = createReceiver();
  server.listen(port, host, () => {
    // eslint-disable-next-line no-console
    console.log(`[mock-receiver] listening on http://${host}:${port}${CONTACT_CAPTURE_PATH}`);
  });
}
