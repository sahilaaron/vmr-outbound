"use strict";
const fs = require("fs");
const path = require("path");
const { JSDOM } = require("jsdom");

const FIXTURE_DIR = path.join(__dirname, "fixtures");

function loadFixtureDoc(name, url) {
  const html = fs.readFileSync(path.join(FIXTURE_DIR, name), "utf8");
  const dom = new JSDOM(html, { url: url || "https://www.linkedin.com/sales/search/people?page=1" });
  return dom.window.document;
}

const SUPPORTED_URL = "https://www.linkedin.com/sales/search/people?keywords=ops&page=2";

module.exports = { loadFixtureDoc, SUPPORTED_URL, FIXTURE_DIR };
