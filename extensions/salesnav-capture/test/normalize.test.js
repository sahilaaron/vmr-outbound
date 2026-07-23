"use strict";
const { test } = require("node:test");
const assert = require("node:assert/strict");
const nm = require("../src/common/normalize.js");

test("cleanText trims and collapses whitespace, null for empty", () => {
  assert.equal(nm.cleanText("  a   b  "), "a b");
  assert.equal(nm.cleanText("   "), null);
  assert.equal(nm.cleanText(null), null);
});

test("splitName splits on first space and preserves unicode", () => {
  assert.deepEqual(nm.splitName("Dana Whitfield"), { firstName: "Dana", lastName: "Whitfield" });
  assert.deepEqual(nm.splitName("大角 知也"), { firstName: "大角", lastName: "知也" });
  assert.deepEqual(nm.splitName("José de la Cruz"), { firstName: "José", lastName: "de la Cruz" });
  assert.deepEqual(nm.splitName("Madonna"), { firstName: "Madonna", lastName: null });
  assert.deepEqual(nm.splitName("   "), { firstName: null, lastName: null });
});

test("normalizeLinkedInUrl absolutizes path-only hrefs", () => {
  const r = nm.normalizeLinkedInUrl("/sales/lead/ABC123");
  assert.equal(r.valid, true);
  assert.equal(r.url, "https://www.linkedin.com/sales/lead/ABC123");
});

test("normalizeLinkedInUrl strips volatile comma suffix from lead urls", () => {
  const r = nm.normalizeLinkedInUrl("/sales/lead/ABC123,NAME_SEARCH,xY9?foo=1#frag");
  assert.equal(r.url, "https://www.linkedin.com/sales/lead/ABC123");
});

test("normalizeLinkedInUrl lowercases host, strips query/fragment/trailing slash", () => {
  const r = nm.normalizeLinkedInUrl("https://WWW.LinkedIn.com/in/jane-doe/?trk=x#a");
  assert.equal(r.url, "https://www.linkedin.com/in/jane-doe");
});

test("normalizeLinkedInUrl rejects non-linkedin and garbage", () => {
  assert.equal(nm.normalizeLinkedInUrl("acme").valid, false);
  assert.equal(nm.normalizeLinkedInUrl("javascript:void(0)").valid, false);
  assert.equal(nm.normalizeLinkedInUrl("https://evil.example.com/in/phish").valid, false);
  assert.equal(nm.normalizeLinkedInUrl("not a url").valid, false);
});

test("classifyLinkedInUrl buckets surfaces", () => {
  assert.equal(nm.classifyLinkedInUrl("https://www.linkedin.com/sales/lead/x"), "sales_lead");
  assert.equal(nm.classifyLinkedInUrl("https://www.linkedin.com/sales/company/1"), "sales_company");
  assert.equal(nm.classifyLinkedInUrl("https://www.linkedin.com/in/x"), "public_profile");
  assert.equal(nm.classifyLinkedInUrl("https://www.linkedin.com/company/x"), "public_company");
});

test("pageNumberFromUrl reads page param", () => {
  assert.equal(nm.pageNumberFromUrl("https://www.linkedin.com/sales/search/people?page=3"), 3);
  assert.equal(nm.pageNumberFromUrl("https://www.linkedin.com/sales/search/people"), null);
  assert.equal(nm.pageNumberFromUrl("garbage"), null);
});
