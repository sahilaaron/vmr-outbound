#!/usr/bin/env node
/**
 * Local mock intake receiver for the Sales Navigator capture extension.
 *
 * Implements the shape of `POST /api/intake/sales-navigator/stage` (see
 * docs/BACKEND_CONTRACT.md) so the extension's send flow can be tested end to
 * end BEFORE the real backend adapter lands. It STAGES only — it never creates
 * contacts and holds nothing but the client_batch_id -> response it minted, in
 * memory, for idempotency.
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

const MOCK_CAMPAIGNS = [
  { id: "camp_demo_001", name: "Pilot — Q3 SaaS Ops", status: "draft" },
  { id: "camp_demo_002", name: "Manufacturing DACH", status: "draft" },
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

function createReceiver(opts) {
  const options = opts || {};
  const seen = new Map(); // client_batch_id -> response body
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

module.exports = { createReceiver, MOCK_CAMPAIGNS };

// CLI
if (require.main === module) {
  const port = Number(process.env.PORT) || 8787;
  const host = process.env.HOST || "127.0.0.1";
  const server = createReceiver();
  server.listen(port, host, () => {
    // eslint-disable-next-line no-console
    console.log(`[mock-receiver] listening on http://${host}:${port}${INTAKE_PATH}`);
  });
}
