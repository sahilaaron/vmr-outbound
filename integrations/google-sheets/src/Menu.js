/**
 * The menu, the sidebar, and the two actions behind its two buttons.
 *
 * Two actions and no workflow editor. The operator picks a campaign, confirms
 * the column mapping the add-on already guessed, selects rows and presses one
 * button; a second button asks for updates. Everything else this add-on could
 * plausibly offer — scheduling, filtering, per-row overrides, saved
 * configurations — is a decision that already has a home in VMR Outbound, and
 * putting a second copy of it in a spreadsheet would create two answers to the
 * same question.
 */

function onOpen() {
  SpreadsheetApp.getUi()
    .createAddonMenu()
    .addItem('Open VMR Outbound', 'showSidebar')
    .addToUi();
}

function onInstall(event) {
  onOpen(event);
}

function showSidebar() {
  var html = HtmlService.createHtmlOutputFromFile('Sidebar').setTitle('VMR Outbound');
  SpreadsheetApp.getUi().showSidebar(html);
}

/** Everything the sidebar needs to render itself in one call. */
function sidebarState() {
  var sheet = SpreadsheetApp.getActiveSheet();
  var values = readGrid(sheet, 0);
  var detected = detectHeaders(values);
  var state = {
    baseUrl: apiBaseUrl(),
    spreadsheetName: SpreadsheetApp.getActiveSpreadsheet().getName(),
    sheetName: sheet.getName(),
    headers: detected.found ? detected.headers : [],
    mapping: detected.fields,
    headerRowIndex: detected.rowIndex,
    missingRequired: missingRequiredFields(detected.fields),
    fields: INPUT_FIELDS,
    audience: '',
    account: null,
    campaigns: [],
    limits: null,
    error: null,
  };
  try {
    state.audience = identityAudience();
    var response = fetchCampaigns();
    state.account = response.account;
    state.campaigns = response.campaigns;
    state.limits = response.limits;
  } catch (error) {
    state.error = error.message;
  }
  return state;
}

/**
 * Submit the operator's current selection.
 *
 * The order is: make sure every selected row can be identified, then send. Keys
 * are written *before* the request rather than after it, so a request that times
 * out mid-flight still leaves the sheet able to identify its own rows — and a
 * retry then presents the same keys, which is what makes the retry free.
 */
function submitSelection(campaignId) {
  if (!campaignId) {
    throw new Error('Choose a campaign first.');
  }
  var sheet = SpreadsheetApp.getActiveSheet();
  var values = readGrid(sheet, 0);
  var detected = detectHeaders(values);
  if (!detected.found) {
    throw new Error('No header row was recognised on this sheet.');
  }
  var missing = missingRequiredFields(detected.fields);
  if (missing.length) {
    throw new Error('This sheet has no ' + missing.join(', ') + ' column.');
  }

  var plan = planOutputColumns(values[detected.rowIndex]);
  createOutputColumns(sheet, plan);
  writeHeaderLabels(sheet, detected.rowIndex, plan);

  values = readGrid(sheet, plan.totalColumns - sheet.getLastColumn());
  var firstDataRow = detected.rowIndex + 1;
  var rows = selectedRowIndexes(sheet, firstDataRow);
  if (!rows.length) {
    rows = allDataRows(values, firstDataRow);
  }
  if (!rows.length) {
    throw new Error('Select the rows to process first.');
  }

  var keyColumn = plan.columns[ROW_KEY_COLUMN];
  var assignments = assignRowKeys(values, {
    keyColumn: keyColumn,
    firstDataRow: firstDataRow,
    rows: rows,
    mintKey: function () {
      return Utilities.getUuid().replace(/-/g, '');
    },
  });
  writeRowKeys(sheet, keyColumn, assignments);
  for (var index = 0; index < assignments.length; index += 1) {
    values[assignments[index].rowIndex][keyColumn] = assignments[index].key;
  }

  var built = buildRows(values, {
    mapping: detected.fields,
    keyColumn: keyColumn,
    rows: rows,
  });
  var payloads = [];
  for (var built_index = 0; built_index < built.length; built_index += 1) {
    payloads.push(built[built_index].payload);
  }

  var response = submitBatch({
    campaign_id: campaignId,
    installation_id: installationId(),
    spreadsheet_id: SpreadsheetApp.getActiveSpreadsheet().getId(),
    sheet_id: String(sheet.getSheetId()),
    generation: 1,
    rows: payloads,
  });

  var results = [];
  for (var r = 0; r < response.rows.length; r += 1) {
    var entry = response.rows[r];
    results.push({
      client_row_id: entry.client_row_id,
      status: entry.status,
      submission_id: entry.submission_id,
      contact_id: entry.contact_id,
      safe_failure_reason: entry.safe_failure_reason,
    });
  }
  writeResultEdits(
    sheet,
    planResultWrites(values, {
      columns: plan.columns,
      firstDataRow: firstDataRow,
      results: results,
      timestamp: new Date().toISOString(),
    })
  );
  return { counts: response.counts, batchId: response.batch_id };
}

/**
 * Ask for updates on every row that has been submitted and is not finished.
 *
 * Chunked against the server's own stated ceiling rather than a number chosen
 * here, so raising the limit on the deployment raises it for the add-on without
 * a new version of this file.
 */
function refreshResults() {
  var sheet = SpreadsheetApp.getActiveSheet();
  var values = readGrid(sheet, 0);
  var detected = detectHeaders(values);
  if (!detected.found) {
    throw new Error('No header row was recognised on this sheet.');
  }
  var plan = planOutputColumns(values[detected.rowIndex]);
  if (plan.create.length) {
    throw new Error('Nothing on this sheet has been submitted to VMR Outbound yet.');
  }
  var firstDataRow = detected.rowIndex + 1;
  var pending = knownSubmissions(values, plan.columns, firstDataRow);
  if (!pending.length) {
    return { counts: { pending: 0, processing: 0, ready: 0, could_not_prepare: 0 }, checked: 0 };
  }

  var limits = fetchCampaigns().limits;
  var chunkSize = (limits && limits.max_result_ids) || 100;
  var collected = [];
  for (var start = 0; start < pending.length; start += chunkSize) {
    var slice = pending.slice(start, start + chunkSize);
    var ids = [];
    var byId = {};
    for (var index = 0; index < slice.length; index += 1) {
      ids.push(slice[index].submissionId);
      byId[slice[index].submissionId] = slice[index].clientRowId;
    }
    var response = fetchResults(ids);
    for (var r = 0; r < response.rows.length; r += 1) {
      var entry = response.rows[r];
      entry.client_row_id = byId[entry.submission_id];
      collected.push(entry);
    }
  }

  writeResultEdits(
    sheet,
    planResultWrites(values, {
      columns: plan.columns,
      firstDataRow: firstDataRow,
      results: collected,
      timestamp: new Date().toISOString(),
    })
  );
  return { counts: summarise(collected), checked: pending.length };
}
