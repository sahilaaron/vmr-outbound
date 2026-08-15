/**
 * The three calls the add-on makes, and the credential it presents.
 *
 * The credential is `ScriptApp.getIdentityToken()` — a Google-signed OpenID
 * Connect ID token for the person running the sheet, minted fresh on every
 * execution. It is never written into a cell, never logged and never stored in a
 * script property. There is deliberately no "paste your API key" field anywhere
 * in this add-on: a key pasted into a spreadsheet travels with every copy of
 * that spreadsheet, and the person who copies it is not always the person who
 * pasted it.
 *
 * The backend verifies the token's signature against Google's published key set,
 * checks it was minted for *this* add-on's OAuth client, and resolves it to an
 * active VMR account. All three are its job, not this file's; what this file
 * must get right is presenting the token and never persisting it.
 */

/**
 * The only production origin an authenticated add-on request may reach.
 *
 * This value is code-owned on purpose. A spreadsheet collaborator must never be
 * able to redirect a fresh Google identity token by editing a document property.
 * Development against another host requires a deliberate source/build change,
 * not a value carried by the sheet.
 */
var VMR_API_ORIGIN = 'https://srv1885453.hstgr.cloud';

function apiBaseUrl() {
  return VMR_API_ORIGIN;
}

function trustedApiUrl(path) {
  var value = String(path || '');
  if (!/^\/integrations\/sheets(?:\/|$)/.test(value)) {
    throw new Error('The requested VMR Outbound integration path is not allowed.');
  }
  return VMR_API_ORIGIN + value;
}

/**
 * A stable identifier for this install of the add-on.
 *
 * Stored in *user* properties, not document properties: it identifies the person
 * and their install, so two colleagues working the same shared spreadsheet get
 * different values and cannot collide on each other's rows. It authorises
 * nothing on its own — it is one component of a row key, and the server derives
 * the key rather than trusting one.
 */
function installationId() {
  var properties = PropertiesService.getUserProperties();
  var existing = properties.getProperty('VMR_INSTALLATION_ID');
  if (existing) {
    return existing;
  }
  var minted = Utilities.getUuid().replace(/-/g, '');
  properties.setProperty('VMR_INSTALLATION_ID', minted);
  return minted;
}

/**
 * The audience claim of the token this script mints.
 *
 * Shown on the setup screen so the operator can copy the exact value into the
 * deployment's `SHEETS__ALLOWED_AUDIENCES`. Read off the token itself rather than
 * looked up in the Cloud console, because the console shows several client ids
 * and only one of them is the one that will actually arrive.
 *
 * Decoding here is display-only. Nothing in this add-on trusts a claim it read
 * itself; the server verifies the signature before believing anything.
 */
function identityAudience() {
  var token = ScriptApp.getIdentityToken();
  if (!token) {
    return '';
  }
  var parts = String(token).split('.');
  if (parts.length !== 3) {
    return '';
  }
  try {
    var decoded = Utilities.newBlob(
      Utilities.base64DecodeWebSafe(parts[1])
    ).getDataAsString();
    var claims = JSON.parse(decoded);
    return claims && claims.aud ? String(claims.aud) : '';
  } catch (error) {
    return '';
  }
}

function request_(path, method, payload) {
  // Resolve and validate the destination before asking Google for an identity
  // token. The token must never exist in a request path chosen by sheet content.
  var url = trustedApiUrl(path);
  var token = ScriptApp.getIdentityToken();
  if (!token) {
    throw new Error(
      'Google did not provide an identity token for this script. Re-authorise the add-on ' +
        'and make sure the script is attached to a standard Google Cloud project.'
    );
  }
  var options = {
    method: method,
    muteHttpExceptions: true,
    headers: { Authorization: 'Bearer ' + token },
  };
  if (payload !== undefined && payload !== null) {
    options.contentType = 'application/json';
    options.payload = JSON.stringify(payload);
  }
  var response = UrlFetchApp.fetch(url, options);
  var code = response.getResponseCode();
  var text = response.getContentText();
  if (code === 401) {
    throw new Error(
      'VMR Outbound did not recognise this Google account. Ask an administrator to confirm ' +
        'you have an active VMR Outbound account and that this add-on is authorised.'
    );
  }
  if (code === 403) {
    throw new Error('You do not have access to that campaign in VMR Outbound.');
  }
  if (code === 404) {
    throw new Error(
      'The Google Sheets integration is switched off on this VMR Outbound deployment.'
    );
  }
  if (code >= 400) {
    // The server's 400 text is written to be shown to an operator. Everything
    // else is reported as a status rather than echoed: an unexpected body may be
    // a proxy error page, and pasting one into a sidebar helps nobody.
    var detail = '';
    try {
      var parsed = JSON.parse(text);
      detail = parsed && parsed.detail ? String(parsed.detail) : '';
    } catch (error) {
      detail = '';
    }
    throw new Error(code === 400 && detail ? detail : 'VMR Outbound refused the request (' + code + ').');
  }
  return JSON.parse(text);
}

function fetchCampaigns() {
  return request_('/integrations/sheets/campaigns', 'get', null);
}

function submitBatch(payload) {
  return request_('/integrations/sheets/batches', 'post', payload);
}

function fetchResults(submissionIds) {
  return request_('/integrations/sheets/results', 'post', { submission_ids: submissionIds });
}
