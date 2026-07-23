"use strict";
const { test } = require("node:test");
const assert = require("node:assert/strict");
const perms = require("../src/common/permissions.js");

test("originPatternForUrl returns loopback host patterns only", () => {
  assert.equal(perms.originPatternForUrl("http://127.0.0.1:8000/api/x"), "http://127.0.0.1/*");
  assert.equal(perms.originPatternForUrl("http://localhost:8787/api/x"), "http://localhost/*");
  assert.equal(perms.originPatternForUrl("https://127.0.0.1/api"), "https://127.0.0.1/*");
});

test("originPatternForUrl refuses non-loopback / malformed URLs", () => {
  assert.equal(perms.originPatternForUrl("https://example.com/api"), null);
  assert.equal(perms.originPatternForUrl("http://10.0.0.5/api"), null);
  assert.equal(perms.originPatternForUrl("ftp://127.0.0.1/x"), null);
  assert.equal(perms.originPatternForUrl("not a url"), null);
  assert.equal(perms.originPatternForUrl(""), null);
});

test("isLoopbackUrl matches only http(s) loopback hosts", () => {
  assert.equal(perms.isLoopbackUrl("http://127.0.0.1:8000/"), true);
  assert.equal(perms.isLoopbackUrl("http://localhost/"), true);
  assert.equal(perms.isLoopbackUrl("https://linkedin.com/"), false);
  assert.equal(perms.isLoopbackUrl("http://192.168.0.1/"), false);
});
