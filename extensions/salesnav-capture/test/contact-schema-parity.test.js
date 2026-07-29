"use strict";
/**
 * Parity proof between the dependency-free contact-first validator and the
 * COMMITTED schema (docs/contact-capture.schema.json), which the backend loads
 * as its single source of truth for the wire shape.
 *
 *   1. SOUNDNESS (always): any submission the extension ACCEPTS must be
 *      schema-valid, so the extension can never send a body the backend
 *      contract rejects.
 *   2. AGREEMENT (corpus): every representative valid/invalid submission
 *      produces the same accept/reject under both, except cases explicitly
 *      marked `validatorStricter` — semantic rules (identity presence,
 *      in-submission id uniqueness) that a JSON Schema cannot express. For
 *      those the validator must REJECT while remaining sound.
 *
 * The committed example fixtures are validated too, so the documentation, the
 * backend tests, and the extension can never drift apart.
 */
const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("fs");
const path = require("path");
const { JSDOM } = require("jsdom");

const contactSchema = require("../src/common/contact-schema.js");
const profileExtraction = require("../src/common/profile-extraction.js");
const { evaluate, collectKeywords, SUPPORTED } = require("./json-schema-subset.js");

const DOCS = path.join(__dirname, "..", "docs");
const SCHEMA = JSON.parse(
  fs.readFileSync(path.join(DOCS, "contact-capture.schema.json"), "utf8")
);
const PROFILE_EXAMPLE = JSON.parse(
  fs.readFileSync(path.join(DOCS, "fixtures", "contact-capture.profile.example.json"), "utf8")
);
const SALESNAV_EXAMPLE = JSON.parse(
  fs.readFileSync(path.join(DOCS, "fixtures", "contact-capture.salesnav.example.json"), "utf8")
);

const PROFILE_URL = "https://www.linkedin.com/in/test-profile";

test("the parity evaluator covers every keyword the contact schema uses", () => {
  const found = new Set();
  collectKeywords(SCHEMA, found);
  const unknown = [...found].filter((k) => !SUPPORTED.has(k) && k !== "minItems");
  assert.deepEqual(unknown, [], `unsupported keywords: ${unknown}`);
});

function basePayload() {
  const html = fs.readFileSync(
    path.join(__dirname, "fixtures-profile", "profile-basic.html"),
    "utf8"
  );
  const doc = new JSDOM(html, { url: PROFILE_URL }).window.document;
  const extraction = profileExtraction.extractProfile(doc, {
    sourceUrl: PROFILE_URL,
    capturedAt: "2026-07-26T10:00:00.000Z",
  });
  return contactSchema.buildSubmission({
    clientSubmissionId: "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
    captureMode: "linkedin_profile",
    submittedAt: "2026-07-26T10:00:00.000Z",
    extensionVersion: "2.0.0",
    metadata: { labels: ["Healthcare"], note: "Met at SaaStr." },
    contacts: [
      contactSchema.buildProfileCapture({
        extraction,
        clientCaptureId: "11111111-2222-3333-4444-555555555555",
        pageTitle: "Test Profile | LinkedIn",
        metadata: null,
      }),
    ],
  });
}

function withPayload(fn) {
  const p = JSON.parse(JSON.stringify(basePayload()));
  fn(p);
  return p;
}

// `minItems` is the one keyword the shared subset evaluator does not implement;
// the corpus covers the empty-contacts case through the extension validator,
// and this helper keeps the schema side honest about it too.
function schemaValid(payload) {
  if (Array.isArray(payload && payload.contacts) && payload.contacts.length < 1) return false;
  return evaluate(SCHEMA, payload, SCHEMA);
}

const CORPUS = [
  { name: "built profile submission", payload: basePayload(), expectValid: true },
  { name: "committed profile example", payload: PROFILE_EXAMPLE, expectValid: true },
  { name: "committed salesnav example", payload: SALESNAV_EXAMPLE, expectValid: true },
  {
    name: "no labels and no note",
    payload: withPayload((p) => {
      p.operator_metadata = { labels: [], note: null };
    }),
    expectValid: true,
  },
  {
    name: "null profile URL (uncertain identity, name present)",
    payload: withPayload((p) => {
      p.contacts[0].person.linkedin_profile_url = null;
      p.contacts[0].person.linkedin_public_identifier = null;
    }),
    expectValid: true,
  },
  {
    name: "no experience observations",
    payload: withPayload((p) => {
      p.contacts[0].experience_observations = [];
    }),
    expectValid: true,
  },

  {
    name: "wrong schema_version",
    payload: withPayload((p) => {
      p.schema_version = "linkedin-contact-capture/3.0.0";
    }),
    expectValid: false,
  },
  {
    name: "wrong source",
    payload: withPayload((p) => {
      p.source = "someone-else";
    }),
    expectValid: false,
  },
  {
    name: "malformed campaign_id",
    payload: withPayload((p) => {
      p.campaign_id = "camp-1";
    }),
    expectValid: false,
  },
  {
    name: "unknown capture mode",
    payload: withPayload((p) => {
      p.capture_mode = "autopilot";
    }),
    expectValid: false,
  },
  {
    name: "empty contacts",
    payload: withPayload((p) => {
      p.contacts = [];
    }),
    expectValid: false,
  },
  {
    name: "operator_triggered false",
    payload: withPayload((p) => {
      p.contacts[0].source.operator_triggered = false;
    }),
    expectValid: false,
  },
  {
    name: "deceptive profile host",
    payload: withPayload((p) => {
      p.contacts[0].person.linkedin_profile_url = "https://linkedin.com.evil.example/in/x";
    }),
    expectValid: false,
  },
  {
    name: "profile sub-route",
    payload: withPayload((p) => {
      p.contacts[0].person.linkedin_profile_url =
        "https://www.linkedin.com/in/x/details/experience";
    }),
    expectValid: false,
  },
  {
    name: "undeclared person key",
    payload: withPayload((p) => {
      p.contacts[0].person.secret = "x";
    }),
    expectValid: false,
  },
  {
    name: "note too long",
    payload: withPayload((p) => {
      p.operator_metadata.note = "y".repeat(2001);
    }),
    expectValid: false,
  },
  {
    name: "too many labels",
    payload: withPayload((p) => {
      p.operator_metadata.labels = Array.from({ length: 26 }, (_, i) => `l${i}`);
    }),
    expectValid: false,
  },
  {
    name: "bad experience layout",
    payload: withPayload((p) => {
      p.contacts[0].experience_observations[0].layout = "unknown";
    }),
    expectValid: false,
  },
  {
    name: "start_date month out of range",
    payload: withPayload((p) => {
      p.contacts[0].experience_observations[0].start_date = { year: 2021, month: 13 };
    }),
    expectValid: false,
  },
  {
    name: "person with no identity at all",
    payload: withPayload((p) => {
      p.contacts[0].person.linkedin_profile_url = null;
      p.contacts[0].person.salesnav_lead_url = null;
      p.contacts[0].person.full_name = null;
    }),
    expectValid: false,
    validatorStricter: true,
  },
  {
    name: "repeated client_capture_id",
    payload: withPayload((p) => {
      p.contacts.push(JSON.parse(JSON.stringify(p.contacts[0])));
    }),
    expectValid: false,
    validatorStricter: true,
  },
];

for (const entry of CORPUS) {
  test(`parity: ${entry.name}`, () => {
    const bySchema = schemaValid(entry.payload);
    const byValidator = contactSchema.validateSubmission(entry.payload).valid;

    // 1. Soundness: accepted by the extension implies schema-valid.
    if (byValidator) assert.equal(bySchema, true, "extension accepted a schema-invalid payload");

    // 2. Agreement, unless the validator is deliberately stricter.
    if (entry.validatorStricter) {
      assert.equal(byValidator, false, "validator must reject the stricter case");
    } else {
      assert.equal(byValidator, entry.expectValid);
      assert.equal(bySchema, entry.expectValid);
    }
  });
}
