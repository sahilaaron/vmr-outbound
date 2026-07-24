"use strict";
const { test } = require("node:test");
const assert = require("node:assert/strict");
const { JSDOM } = require("jsdom");

const surface = require("../src/common/surface.js");
const { SURFACES } = require("../src/common/constants.js");

function doc(html, url) {
  return new JSDOM(html || "<main></main>", { url: url || "https://www.linkedin.com/" }).window
    .document;
}

test("salesnav people results URL classifies as salesnav surface", () => {
  const url = "https://www.linkedin.com/sales/search/people?keywords=ops&page=2";
  const d = doc("<main></main>", url);
  assert.equal(surface.detectSurface(url, d).surface, SURFACES.SALESNAV_PEOPLE_RESULTS);
});

test("salesnav account search / company surfaces stay unsupported", () => {
  for (const url of [
    "https://www.linkedin.com/sales/search/company?keywords=x",
    "https://www.linkedin.com/sales/company/12345",
    "https://www.linkedin.com/sales/home",
  ]) {
    const r = surface.detectSurface(url, doc("<main></main>", url));
    assert.equal(r.surface, SURFACES.UNSUPPORTED, url);
    assert.equal(r.reason, "unsupported_sales_surface");
  }
});

test("main person profile routes are supported; sub-routes are not", () => {
  assert.ok(surface.isSupportedPersonProfileUrl("https://www.linkedin.com/in/morgan-vale"));
  assert.ok(surface.isSupportedPersonProfileUrl("https://www.linkedin.com/in/morgan-vale/"));
  assert.ok(surface.isSupportedPersonProfileUrl("https://linkedin.com/in/zo%C3%AB-m%C3%BCller"));
  assert.ok(!surface.isSupportedPersonProfileUrl("https://www.linkedin.com/in/"));
  assert.ok(
    !surface.isSupportedPersonProfileUrl(
      "https://www.linkedin.com/in/morgan-vale/details/experience/"
    )
  );
  assert.ok(
    !surface.isSupportedPersonProfileUrl(
      "https://www.linkedin.com/in/morgan-vale/recent-activity/all/"
    )
  );
  assert.ok(!surface.isSupportedPersonProfileUrl("https://example.com/in/morgan-vale"));

  const sub = "https://www.linkedin.com/in/morgan-vale/details/experience/";
  const r = surface.detectSurface(sub, doc("<main><h1>x</h1></main>", sub));
  assert.equal(r.surface, SURFACES.UNSUPPORTED);
  assert.equal(r.reason, "profile_subroute");
});

test("public identifier is derived only from the URL, decoded", () => {
  assert.equal(
    surface.publicIdentifierFromUrl("https://www.linkedin.com/in/morgan-vale/"),
    "morgan-vale"
  );
  assert.equal(
    surface.publicIdentifierFromUrl("https://www.linkedin.com/in/zo%C3%AB-m%C3%BCller"),
    "zoë-müller"
  );
  assert.equal(surface.publicIdentifierFromUrl("https://www.linkedin.com/feed/"), null);
});

test("person profile page classifies as person surface with identifier", () => {
  const url = "https://www.linkedin.com/in/morgan-vale";
  const d = doc("<main><h1>Morgan Vale</h1></main>", url);
  const r = surface.detectSurface(url, d);
  assert.equal(r.surface, SURFACES.PERSON_PROFILE);
  assert.equal(r.publicIdentifier, "morgan-vale");
});

test("company routes classify as company surface (home + about)", () => {
  for (const url of [
    "https://www.linkedin.com/company/meridian-works",
    "https://www.linkedin.com/company/meridian-works/",
    "https://www.linkedin.com/company/meridian-works/about/",
  ]) {
    const r = surface.detectSurface(url, doc("<main><h1>Meridian</h1></main>", url));
    assert.equal(r.surface, SURFACES.COMPANY_PROFILE, url);
  }
  // Deeper company sub-routes are unsupported.
  const posts = "https://www.linkedin.com/company/meridian-works/posts/";
  assert.equal(
    surface.detectSurface(posts, doc("<main></main>", posts)).surface,
    SURFACES.UNSUPPORTED
  );
});

test("company/unavailable is reported unavailable", () => {
  const url = "https://www.linkedin.com/company/unavailable/";
  const r = surface.detectSurface(url, doc("<main></main>", url));
  assert.equal(r.surface, SURFACES.UNAVAILABLE);
});

test("checkpoint URLs and login walls classify as challenge", () => {
  const cp = "https://www.linkedin.com/checkpoint/challenge/xyz";
  assert.equal(surface.detectSurface(cp, doc("<main></main>", cp)).surface, SURFACES.CHALLENGE);

  const url = "https://www.linkedin.com/in/morgan-vale";
  const wall = doc(
    '<main><h1>Sign in</h1><form action="/uas/login-submit" class="join-form"></form></main>',
    url
  );
  assert.equal(surface.detectSurface(url, wall).surface, SURFACES.CHALLENGE);
});

test("non-linkedin hosts are unsupported", () => {
  const url = "https://example.com/in/morgan-vale";
  const r = surface.detectSurface(url, doc("<main></main>", url));
  assert.equal(r.surface, SURFACES.UNSUPPORTED);
  assert.equal(r.reason, "not_linkedin");
});
