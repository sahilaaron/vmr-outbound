/**
 * Everything the add-on decides about a sheet, as pure functions over a grid.
 *
 * Apps Script has no module system and one global scope, so the temptation is to
 * reach for `SpreadsheetApp` from wherever a value is needed. Everything in this
 * file deliberately refuses that: each function takes a plain two-dimensional
 * array of cell values and returns either a new array or a plan of what to
 * write. Nothing here opens a spreadsheet, calls a URL or reads a property.
 *
 * That is not tidiness for its own sake. The failure this add-on has to not have
 * is *writing one prospect's messages onto another prospect's row*, and the only
 * way to prove that does not happen — under sorting, insertion, deletion and a
 * partially-refreshed sheet — is to be able to run the mapping over a fabricated
 * grid, hundreds of times, without a Google account. `test/contract.test.js` does
 * exactly that.
 *
 * Loaded by Apps Script as a `.gs` file (clasp converts the extension). The
 * `module.exports` tail at the bottom is invisible there — `module` is undefined
 * in the Apps Script runtime — and is what lets Node run the same source.
 */

/** The column the add-on writes into, in the order it creates them. */
var OUTPUT_COLUMNS = [
  'VMR Status',
  'Email Address',
  'Email 1',
  'Email 2',
  'Email 3',
  'Email 4',
  'Email 5',
  'Email 6',
  'Email 7',
  'VMR Note',
  'VMR Last Updated',
  'VMR Contact ID',
  'VMR Campaign Contact ID',
];

/**
 * The hidden column that carries row identity.
 *
 * A spreadsheet row number is not an identity — sorting renames every row at
 * once — so results are never written back by position. Each row gets an opaque
 * key here the first time it is submitted, and that key is what the response is
 * matched against. The column is hidden because it is machinery, not data, and
 * an operator who deletes it simply causes the next submission to mint new keys
 * rather than causing a wrong write.
 */
var ROW_KEY_COLUMN = 'VMR Row Key';

/** The input fields the add-on maps, and which of them are required. */
var INPUT_FIELDS = [
  { key: 'first_name', label: 'First Name', required: true },
  { key: 'last_name', label: 'Last Name', required: true },
  { key: 'company_name', label: 'Company Name', required: true },
  { key: 'job_title', label: 'Job Title', required: false },
  { key: 'linkedin_url', label: 'LinkedIn URL', required: false },
  { key: 'context', label: 'Context', required: false },
];

/** Header spellings the add-on recognises without the operator mapping them. */
var HEADER_SYNONYMS = {
  first_name: ['first name', 'firstname', 'first', 'given name', 'fname'],
  last_name: ['last name', 'lastname', 'last', 'surname', 'family name', 'lname'],
  company_name: ['company name', 'company', 'organisation', 'organization', 'account', 'employer'],
  job_title: ['job title', 'title', 'role', 'position', 'designation'],
  linkedin_url: ['linkedin url', 'linkedin', 'linkedin profile', 'profile url', 'li url'],
  context: ['context', 'notes', 'note', 'comment', 'comments', 'prospect context'],
};

var STATUS_LABELS = {
  pending: 'Pending',
  processing: 'Processing',
  ready: 'Ready',
  could_not_prepare: 'Could not prepare',
  not_submitted: 'Not submitted',
};

function normaliseHeader_(value) {
  return String(value === null || value === undefined ? '' : value)
    .replace(/ /g, ' ')
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, ' ')
    .trim();
}

function cellText_(value) {
  if (value === null || value === undefined) {
    return '';
  }
  return String(value).replace(/ /g, ' ').trim();
}

/**
 * Find the header row and what each recognised column is called.
 *
 * The header is not assumed to be row 1. Operators put a title, a filter note or
 * a blank line above their data often enough that assuming row 1 produces a
 * confident, wrong mapping — and a wrong mapping is worse than no mapping,
 * because it submits the company column as a surname. So the first ten rows are
 * scored and the best-scoring one wins, with a floor: a row that matches nothing
 * is not a header, and the answer is "no header found" rather than row 1.
 */
function detectHeaders(values, options) {
  var limit = Math.min(values.length, (options && options.searchRows) || 10);
  var best = null;
  for (var index = 0; index < limit; index += 1) {
    var candidate = mapHeaderRow(values[index]);
    var score = Object.keys(candidate.fields).length;
    if (score > 0 && (best === null || score > best.score)) {
      best = { rowIndex: index, score: score, fields: candidate.fields, headers: candidate.headers };
    }
  }
  if (best === null) {
    return { found: false, rowIndex: -1, fields: {}, headers: [] };
  }
  return {
    found: true,
    rowIndex: best.rowIndex,
    fields: best.fields,
    headers: best.headers,
  };
}

/** Map one row of header labels to field keys and to their column indexes. */
function mapHeaderRow(row) {
  var headers = [];
  var fields = {};
  for (var column = 0; column < row.length; column += 1) {
    var label = cellText_(row[column]);
    headers.push(label);
    var normalised = normaliseHeader_(label);
    if (!normalised) {
      continue;
    }
    for (var key in HEADER_SYNONYMS) {
      if (!Object.prototype.hasOwnProperty.call(HEADER_SYNONYMS, key)) {
        continue;
      }
      if (fields[key] !== undefined) {
        continue;
      }
      if (HEADER_SYNONYMS[key].indexOf(normalised) !== -1) {
        fields[key] = column;
      }
    }
  }
  return { headers: headers, fields: fields };
}

/** Which required inputs the current mapping still lacks. */
function missingRequiredFields(mapping) {
  var missing = [];
  for (var index = 0; index < INPUT_FIELDS.length; index += 1) {
    var field = INPUT_FIELDS[index];
    if (field.required && (mapping[field.key] === undefined || mapping[field.key] === null)) {
      missing.push(field.label);
    }
  }
  return missing;
}

/**
 * Work out which columns must be created, without creating any.
 *
 * Returns a plan rather than performing the change, so the caller can apply it
 * in one write and a test can assert the plan directly. Existing VMR columns are
 * reused wherever they already are: an operator who moved the Status column has
 * moved it on purpose, and re-creating it beside their copy is the behaviour
 * that makes an add-on untrustworthy.
 */
function planOutputColumns(headerRow) {
  var existing = {};
  for (var column = 0; column < headerRow.length; column += 1) {
    var label = cellText_(headerRow[column]);
    if (label) {
      existing[label.toLowerCase()] = column;
    }
  }
  var wanted = [ROW_KEY_COLUMN].concat(OUTPUT_COLUMNS);
  var columns = {};
  var create = [];
  var next = headerRow.length;
  for (var index = 0; index < wanted.length; index += 1) {
    var name = wanted[index];
    var found = existing[name.toLowerCase()];
    if (found !== undefined) {
      columns[name] = found;
      continue;
    }
    columns[name] = next;
    create.push({ name: name, column: next });
    next += 1;
  }
  return { columns: columns, create: create, totalColumns: next };
}

/**
 * Give every data row a durable key, minting one only where none exists.
 *
 * Idempotent by construction: a row that already carries a key keeps it, which
 * is what makes a second submission of the same selection reach the same server
 * row rather than a new one. `mintKey` is injected so a test can make the keys
 * deterministic; in the add-on it is a UUID.
 */
function assignRowKeys(values, options) {
  var keyColumn = options.keyColumn;
  var firstDataRow = options.firstDataRow;
  var mintKey = options.mintKey;
  var rows = options.rows || allDataRows(values, firstDataRow);
  var assigned = [];
  for (var index = 0; index < rows.length; index += 1) {
    var rowIndex = rows[index];
    var row = values[rowIndex] || [];
    var existing = cellText_(row[keyColumn]);
    if (existing) {
      continue;
    }
    var key = mintKey();
    assigned.push({ rowIndex: rowIndex, key: key });
  }
  return assigned;
}

function allDataRows(values, firstDataRow) {
  var rows = [];
  for (var index = firstDataRow; index < values.length; index += 1) {
    if (rowIsEmpty_(values[index])) {
      continue;
    }
    rows.push(index);
  }
  return rows;
}

function rowIsEmpty_(row) {
  if (!row) {
    return true;
  }
  for (var index = 0; index < row.length; index += 1) {
    if (cellText_(row[index])) {
      return false;
    }
  }
  return true;
}

/**
 * Turn selected rows into the payload the backend accepts.
 *
 * A row with a blank required cell is *not* filtered out here. It is sent, the
 * server refuses it by name, and the refusal is written into that row's status —
 * which is how the operator finds out. Silently skipping it would leave a blank
 * status cell and a person quietly missing from the campaign.
 */
function buildRows(values, options) {
  var mapping = options.mapping;
  var keyColumn = options.keyColumn;
  var rows = [];
  for (var index = 0; index < options.rows.length; index += 1) {
    var rowIndex = options.rows[index];
    var row = values[rowIndex] || [];
    var key = cellText_(row[keyColumn]);
    if (!key) {
      continue;
    }
    var payload = { client_row_id: key };
    for (var field = 0; field < INPUT_FIELDS.length; field += 1) {
      var name = INPUT_FIELDS[field].key;
      var column = mapping[name];
      if (column === undefined || column === null) {
        continue;
      }
      var value = cellText_(row[column]);
      if (value) {
        payload[name] = value;
      }
    }
    rows.push({ rowIndex: rowIndex, payload: payload });
  }
  return rows;
}

/** How one sequence message is rendered into a single cell. */
function renderMessage(message) {
  var subject = cellText_(message && message.subject);
  var body = message && message.body ? String(message.body) : '';
  var day = message && typeof message.elapsed_day === 'number' ? message.elapsed_day : null;
  var header = 'Subject: ' + subject;
  if (day !== null) {
    header = 'Day ' + day + ' — ' + header;
  }
  return header + '\n\n' + body;
}

/**
 * Compute the cells to write for a set of results, matched by row key.
 *
 * The one rule that matters: a result reaches a row **only** when that row's own
 * key column still holds the key the result was issued for. Rows are looked up
 * by reading the grid as it is now, so a sheet sorted, filtered or edited between
 * submitting and refreshing is not a hazard — a moved row is found at its new
 * position, and a row whose key was deleted receives nothing rather than
 * receiving somebody else's messages.
 *
 * Input columns are never written. The returned edits only ever name columns
 * from `planOutputColumns`, which is asserted directly in the tests.
 */
function planResultWrites(values, options) {
  var columns = options.columns;
  var keyColumn = options.columns[ROW_KEY_COLUMN];
  var firstDataRow = options.firstDataRow;
  var timestamp = options.timestamp || '';
  var byKey = {};
  for (var index = 0; index < options.results.length; index += 1) {
    var result = options.results[index];
    if (result && result.client_row_id) {
      byKey[result.client_row_id] = result;
    }
  }

  var edits = [];
  for (var rowIndex = firstDataRow; rowIndex < values.length; rowIndex += 1) {
    var row = values[rowIndex] || [];
    var key = cellText_(row[keyColumn]);
    if (!key || !Object.prototype.hasOwnProperty.call(byKey, key)) {
      continue;
    }
    var payload = byKey[key];
    var cells = {};
    cells[columns['VMR Status']] = STATUS_LABELS[payload.status] || payload.status || '';
    cells[columns['Email Address']] = payload.email_address || '';
    var messages = payload.messages || [];
    for (var position = 1; position <= 7; position += 1) {
      var message = null;
      for (var m = 0; m < messages.length; m += 1) {
        if (messages[m] && messages[m].sequence_index === position) {
          message = messages[m];
        }
      }
      cells[columns['Email ' + position]] = message ? renderMessage(message) : '';
    }
    cells[columns['VMR Note']] = payload.safe_failure_reason || payload.note || '';
    cells[columns['VMR Last Updated']] = timestamp;
    if (payload.contact_id) {
      cells[columns['VMR Contact ID']] = payload.contact_id;
    }
    if (payload.submission_id) {
      cells[columns['VMR Campaign Contact ID']] = payload.submission_id;
    }
    edits.push({ rowIndex: rowIndex, cells: cells });
  }
  return edits;
}

/** Apply a set of edits to a copy of the grid. Used by the tests and by writes. */
function applyEdits(values, edits, totalColumns) {
  var copy = [];
  for (var index = 0; index < values.length; index += 1) {
    var row = (values[index] || []).slice();
    while (row.length < totalColumns) {
      row.push('');
    }
    copy.push(row);
  }
  for (var edit = 0; edit < edits.length; edit += 1) {
    var change = edits[edit];
    for (var column in change.cells) {
      if (Object.prototype.hasOwnProperty.call(change.cells, column)) {
        copy[change.rowIndex][Number(column)] = change.cells[column];
      }
    }
  }
  return copy;
}

/** Count the four states for the sidebar's summary line. */
function summarise(results) {
  var counts = { pending: 0, processing: 0, ready: 0, could_not_prepare: 0 };
  for (var index = 0; index < results.length; index += 1) {
    var status = results[index] && results[index].status;
    if (Object.prototype.hasOwnProperty.call(counts, status)) {
      counts[status] += 1;
    }
  }
  return counts;
}

if (typeof module !== 'undefined' && module.exports) {
  module.exports = {
    HEADER_SYNONYMS: HEADER_SYNONYMS,
    INPUT_FIELDS: INPUT_FIELDS,
    OUTPUT_COLUMNS: OUTPUT_COLUMNS,
    ROW_KEY_COLUMN: ROW_KEY_COLUMN,
    STATUS_LABELS: STATUS_LABELS,
    allDataRows: allDataRows,
    applyEdits: applyEdits,
    assignRowKeys: assignRowKeys,
    buildRows: buildRows,
    detectHeaders: detectHeaders,
    mapHeaderRow: mapHeaderRow,
    missingRequiredFields: missingRequiredFields,
    planOutputColumns: planOutputColumns,
    planResultWrites: planResultWrites,
    renderMessage: renderMessage,
    summarise: summarise,
  };
}
