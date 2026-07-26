"use strict";
/**
 * End-to-end save flow against the local mock receiver (DAT-013).
 *
 * Proves the operator-visible behaviour of the contact-first path without a
 * running backend: a reviewed submission is accepted, a retry of the same
 * content replays idempotently, a campaign field is refused, a validation
 * failure is reported clearly, and the returned record links are the only ones
 * the panel will open.
 */
const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("fs");
const path = require("path");

const { createReceiver } = require("../tools/mock-receiver.js");
const constants = require("../src/common/constants.js");
const contactSchema = require("../src/common/contact-schema.js");
const handoff = require("../src/common/handoff.js");

const EXAMPLE = JSON.parse(
  fs.readFileSync(
    path.join(__dirname, "..", "docs", "fixtures", "contact-capture.profile.example.json"),
    "utf8"
  )
);

function listen(server) {
  return new Promise((resolve) => server.listen(0, "127.0.0.1", () => resolve(server.address())));
}

function close(server) {
  return new Promise((resolve) => server.close(resolve));
}

async function post(base, payload, headers) {
  const res = await fetch(base + constants.CONTACT_CAPTURE_PATH, {
    method: "POST",
    headers: Object.assign(
      { "Content-Type": "application/json", "Idempotency-Key": payload.client_submission_id },
      headers || {}
    ),
    body: JSON.stringify(payload),
  });
  return { status: res.status, body: await res.json() };
}

function freshPayload(overrides) {
  const payload = JSON.parse(JSON.stringify(EXAMPLE));
  payload.client_submission_id = "sub-" + Math.random().toString(16).slice(2) + "-0000-0000";
  Object.assign(payload, overrides || {});
  return payload;
}

test("a reviewed submission is accepted and returns per-capture outcomes", async () => {
  const server = createReceiver();
  const { port } = await listen(server);
  const base = `http://127.0.0.1:${port}`;
  try {
    const payload = freshPayload();
    assert.equal(contactSchema.validateSubmission(payload).valid, true);

    const { status, body } = await post(base, payload);
    assert.equal(status, 201);
    assert.equal(body.already_received, false);
    assert.equal(body.counts.submitted, 1);
    assert.equal(body.counts.created, 0);
    assert.equal(body.results.length, 1);

    const result = handoff.sanitizeContactSubmissionResult(body, { submittedAt: "now" });
    assert.equal(result.counts.submitted, 1);
    assert.ok(handoff.isOpenableWorkbenchUrl(result.workbenchUrl));
    assert.ok(handoff.isOpenableWorkbenchUrl(result.results[0].captureUrl));
  } finally {
    await close(server);
  }
});

test("retrying the same submission replays it instead of duplicating", async () => {
  const server = createReceiver();
  const { port } = await listen(server);
  const base = `http://127.0.0.1:${port}`;
  try {
    const payload = freshPayload();
    const first = await post(base, payload);
    const second = await post(base, payload);
    assert.equal(first.status, 201);
    assert.equal(second.status, 200);
    assert.equal(second.body.already_received, true);
    assert.equal(second.body.submission_id, first.body.submission_id);
  } finally {
    await close(server);
  }
});

test("a submission carrying a campaign is refused", async () => {
  const server = createReceiver();
  const { port } = await listen(server);
  const base = `http://127.0.0.1:${port}`;
  try {
    const { status, body } = await post(base, freshPayload({ campaign_id: "camp_demo_001" }));
    assert.equal(status, 422);
    assert.equal(body.error, "validation_failed");
    assert.ok(body.details.some((d) => /campaign_id is not part of this contract/.test(d)));
  } finally {
    await close(server);
  }
});

test("a rejection is described to the operator without echoing the body", async () => {
  const server = createReceiver();
  const { port } = await listen(server);
  const base = `http://127.0.0.1:${port}`;
  try {
    const invalid = freshPayload();
    invalid.contacts = [];
    const { status, body } = await post(base, invalid);
    assert.equal(status, 422);
    assert.equal(body.error, "validation_failed");
    const described = handoff.describeSendError({
      ok: false,
      error: "receiver_rejected",
      status,
      body,
    });
    // A contract failure is not something a retry can fix, and the operator
    // message never echoes captured values back.
    assert.equal(described.canRetry, false);
    assert.match(described.detail, /validation issue/);
    assert.equal(/Morgan/.test(described.headline + described.detail), false);
  } finally {
    await close(server);
  }
});

test("the label list and lookup endpoints answer without exposing contact data", async () => {
  const server = createReceiver();
  const { port } = await listen(server);
  const base = `http://127.0.0.1:${port}`;
  try {
    const labels = await (await fetch(base + constants.CONTACT_LABELS_PATH)).json();
    assert.ok(Array.isArray(labels.labels));
    assert.ok(labels.labels.every((l) => typeof l.name === "string"));

    const lookup = await (
      await fetch(
        base +
          constants.CONTACT_LOOKUP_PATH +
          "?linkedin_profile_url=https://www.linkedin.com/in/morgan-vale"
      )
    ).json();
    assert.deepEqual(Object.keys(lookup).sort(), ["contact_count", "match"]);
  } finally {
    await close(server);
  }
});

test("a backend that is not running surfaces a retryable failure", () => {
  const described = handoff.describeSendError({ ok: false, error: "network_error" });
  assert.equal(described.canRetry, true);
  assert.match(described.headline, /Could not reach the backend/);
});
