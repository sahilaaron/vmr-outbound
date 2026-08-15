'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');

const { loadAddon } = require('./load');

const TRUSTED_ORIGIN = 'https://srv1885453.hstgr.cloud';

test('authenticated requests are hard-bound to the trusted VMR origin', () => {
  const calls = [];
  const context = loadAddon({
    // Deliberately hostile document property. The request path must never read it.
    PropertiesService: {
      getDocumentProperties() {
        return {
          getProperty() {
            return 'https://attacker.example';
          },
        };
      },
    },
    ScriptApp: {
      getIdentityToken() {
        return 'fresh-google-id-token';
      },
    },
    UrlFetchApp: {
      fetch(url, options) {
        calls.push({ url, options });
        return {
          getResponseCode() {
            return 200;
          },
          getContentText() {
            return '{"campaigns":[],"limits":{},"account":{"email":"user@example.com"}}';
          },
        };
      },
    },
  });

  context.fetchCampaigns();

  assert.equal(calls.length, 1);
  assert.equal(calls[0].url, TRUSTED_ORIGIN + '/integrations/sheets/campaigns');
  assert.equal(calls[0].options.headers.Authorization, 'Bearer fresh-google-id-token');
  assert.ok(!calls[0].url.includes('attacker.example'));
});

test('an off-origin or non-Sheets path is refused before an identity token is minted', () => {
  let tokenCalls = 0;
  let fetchCalls = 0;
  const context = loadAddon({
    ScriptApp: {
      getIdentityToken() {
        tokenCalls += 1;
        return 'should-not-be-minted';
      },
    },
    UrlFetchApp: {
      fetch() {
        fetchCalls += 1;
        throw new Error('must not fetch');
      },
    },
  });

  assert.throws(
    () => context.request_('https://attacker.example/collect', 'get', null),
    /integration path is not allowed/
  );
  assert.equal(tokenCalls, 0);
  assert.equal(fetchCalls, 0);
});

test('the visible API base is the code-owned trusted origin', () => {
  const context = loadAddon();
  assert.equal(context.apiBaseUrl(), TRUSTED_ORIGIN);
  assert.equal(context.VMR_API_ORIGIN, TRUSTED_ORIGIN);
});
