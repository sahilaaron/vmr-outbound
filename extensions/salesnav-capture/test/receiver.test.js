"use strict";
const { test } = require("node:test");
const assert = require("node:assert/strict");
const { createReceiver, MOCK_CAMPAIGNS } = require("../tools/mock-receiver.js");
const constants = require("../src/common/constants.js");

const INTAKE_PATH = constants.INTAKE_PATH;

function listen(server) {
  return new Promise((resolve) => server.listen(0, "127.0.0.1", () => resolve(server.address().port)));
}
function close(server) {
  return new Promise((resolve) => server.close(resolve));
}
function samplePayload(id) {
  return {
    schema_version: constants.SCHEMA_VERSION,
    client_batch_id: id,
    campaign_id: "camp_demo_001",
    captured_at: "2026-07-23T00:00:00.000Z",
    source: constants.SOURCE_IDENTIFIER,
    current_search_url: "https://www.linkedin.com/sales/search/people?page=1",
    extraction_metadata: {},
    records: [{ rawFullName: "Test Person", salesNavLeadUrl: "https://www.linkedin.com/sales/lead/x", warnings: [] }],
  };
}

test("mock receiver: successful staging returns 201 with staging id + workbench url", async () => {
  const server = createReceiver();
  const port = await listen(server);
  try {
    const resp = await fetch(`http://127.0.0.1:${port}${INTAKE_PATH}`, {
      method: "POST",
      headers: { "Content-Type": "application/json", "Idempotency-Key": "batch-A" },
      body: JSON.stringify(samplePayload("batch-A")),
    });
    assert.equal(resp.status, 201);
    const body = await resp.json();
    assert.equal(body.record_count, 1);
    assert.equal(body.already_received, false);
    assert.ok(body.staging_id.startsWith("stg_"));
    assert.match(body.operator_workbench_url, /^http:\/\/127\.0\.0\.1:\d+\/workbench\//);
  } finally {
    await close(server);
  }
});

test("mock receiver: repeated client_batch_id is idempotent (already_received)", async () => {
  const server = createReceiver();
  const port = await listen(server);
  try {
    const url = `http://127.0.0.1:${port}${INTAKE_PATH}`;
    const opts = {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(samplePayload("batch-B")),
    };
    const first = await (await fetch(url, opts)).json();
    const second = await fetch(url, opts);
    const secondBody = await second.json();
    assert.equal(second.status, 200);
    assert.equal(secondBody.already_received, true);
    assert.equal(secondBody.staging_id, first.staging_id); // same staging record
  } finally {
    await close(server);
  }
});

test("mock receiver: rejection surfaces a non-2xx status + body", async () => {
  const server = createReceiver();
  const port = await listen(server);
  try {
    const resp = await fetch(`http://127.0.0.1:${port}${INTAKE_PATH}`, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Mock-Force-Status": "422" },
      body: JSON.stringify(samplePayload("batch-C")),
    });
    assert.equal(resp.status, 422);
    const body = await resp.json();
    assert.equal(body.error, "forced_status");
  } finally {
    await close(server);
  }
});

test("mock receiver: empty records -> 422 validation_failed", async () => {
  const server = createReceiver();
  const port = await listen(server);
  try {
    const bad = samplePayload("batch-D");
    bad.records = [];
    const resp = await fetch(`http://127.0.0.1:${port}${INTAKE_PATH}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(bad),
    });
    assert.equal(resp.status, 422);
    const body = await resp.json();
    assert.equal(body.error, "validation_failed");
  } finally {
    await close(server);
  }
});

test("client-side timeout: slow receiver triggers AbortError before response", async () => {
  const server = createReceiver({ delayMs: 300 });
  const port = await listen(server);
  try {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 60);
    let aborted = false;
    try {
      await fetch(`http://127.0.0.1:${port}${INTAKE_PATH}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(samplePayload("batch-E")),
        signal: controller.signal,
      });
    } catch (e) {
      aborted = e.name === "AbortError";
    } finally {
      clearTimeout(timer);
    }
    assert.equal(aborted, true);
  } finally {
    await close(server);
  }
});

test("mock receiver: campaigns endpoint returns minimal id/name/status", async () => {
  const server = createReceiver();
  const port = await listen(server);
  try {
    const resp = await fetch(`http://127.0.0.1:${port}/api/campaigns?fields=id,name,status`);
    assert.equal(resp.status, 200);
    const body = await resp.json();
    assert.equal(body.length, MOCK_CAMPAIGNS.length);
    assert.ok(body[0].id && body[0].name && body[0].status);
  } finally {
    await close(server);
  }
});

test("mock receiver: unknown path -> 404", async () => {
  const server = createReceiver();
  const port = await listen(server);
  try {
    const resp = await fetch(`http://127.0.0.1:${port}/nope`);
    assert.equal(resp.status, 404);
  } finally {
    await close(server);
  }
});
