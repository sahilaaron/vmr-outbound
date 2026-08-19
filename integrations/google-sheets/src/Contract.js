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

/**
 * The columns the add-on writes into, in the order it creates them.
 *
 * Every one of them is `VMR `-prefixed, and that prefix is load-bearing rather
 * than decorative. The previous list called two of these things `Email Address`
 * and `Email 1`…`Email 7` — names an operator's own source columns very
 * commonly already have — and `planOutputColumns` adopts an existing column by
 * name. A sheet that arrived with a filled `Email Address` column therefore had
 * that column claimed as VMR output and blanked the first time a result carried
 * no address. The prefix removes the collision for today's names;
 * `planOutputColumns` refuses the collision structurally for any future one.
 */
var OUTPUT_COLUMNS = [
  'VMR Status',
  'VMR Email Address',
  'VMR Email 1',
  'VMR Email 2',
  'VMR Email 3',
  'VMR Email 4',
  'VMR Email 5',
  'VMR Email 6',
  'VMR Email 7',
  'VMR Note',
  'VMR Last Updated',
  'VMR Contact ID',
  'VMR Campaign Contact ID',
];

/**
 * The unprefixed output columns the pre-repair client created, in its order.
 *
 * Kept only so those columns can be *recognised* on a sheet that older client
 * already wrote to. Nothing writes them any more and nothing renames them: the
 * add-on appends its new `VMR `-prefixed columns and leaves the stale ones
 * exactly where they are, holding exactly what they held. An operator can
 * delete them whenever they like, and until then the sheet keeps the record of
 * what the previous client produced.
 *
 * See `legacyOutputColumns` for why recognising them matters.
 */
var LEGACY_OUTPUT_SEQUENCE = [
  'VMR Status',
  'Email Address',
  'Email 1',
  'Email 2',
  'Email 3',
  'Email 4',
  'Email 5',
  'Email 6',
  'Email 7',
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

/**
 * The input fields the add-on maps, and which of them are required.
 *
 * `key` is the wire name, not a label: `buildRows` sends each mapped cell under
 * exactly this string, and the server reads `email` and `website` under exactly
 * those two. Renaming a key here silently stops that value reaching the product,
 * because the batch endpoint takes a plain object and ignores what it does not
 * recognise rather than refusing it.
 *
 * `email` and `website` are optional, like every non-name field. Supplying
 * neither is the ordinary case and changes nothing: the product discovers and
 * verifies the address and establishes the company domain itself, exactly as it
 * did before this surface could accept them.
 */
var INPUT_FIELDS = [
  { key: 'first_name', label: 'First Name', required: true },
  { key: 'last_name', label: 'Last Name', required: true },
  { key: 'company_name', label: 'Company Name', required: true },
  { key: 'job_title', label: 'Job Title', required: false },
  { key: 'linkedin_url', label: 'LinkedIn URL', required: false },
  { key: 'email', label: 'Email Address', required: false },
  { key: 'website', label: 'Company Website', required: false },
  { key: 'context', label: 'Context', required: false },
];

/**
 * Header spellings the add-on recognises without the operator mapping them.
 *
 * `website` covers a bare domain and a full URL under one key on purpose. The
 * server reads both through the same field, so offering `company_domain` as a
 * second wire name would be a second spelling of one fact — and the one the
 * server does not read.
 */
var HEADER_SYNONYMS = {
  first_name: ['first name', 'firstname', 'first', 'given name', 'fname'],
  last_name: ['last name', 'lastname', 'last', 'surname', 'family name', 'lname'],
  company_name: ['company name', 'company', 'organisation', 'organization', 'account', 'employer'],
  job_title: ['job title', 'title', 'role', 'position', 'designation'],
  linkedin_url: ['linkedin url', 'linkedin', 'linkedin profile', 'profile url', 'li url'],
  email: ['email', 'email address', 'e mail', 'work email', 'business email', 'corporate email'],
  website: ['company website', 'website', 'company domain', 'domain', 'company url', 'web site'],
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
 * The columns on this header row that are a previous client's output, not data.
 *
 * The problem this solves exists only on sheets the pre-repair add-on already
 * wrote to. There, a column headed `Email Address` holds an address *VMR
 * produced*, and a column headed `Email 1` holds a generated message — while on
 * an untouched operator sheet those same two headers mean an address the
 * operator supplied and their own first email. The two are indistinguishable by
 * name, and reading a generated address back as an operator-supplied one would
 * hand the product its own output as though a person had asserted it.
 *
 * What does distinguish them is that the old client wrote its columns as one
 * contiguous run in one fixed order: `VMR Status`, `Email Address`, `Email 1` …
 * `Email 7`. Nothing else is accepted as evidence — not `VMR Status` existing
 * somewhere, not `Email Address` existing somewhere, only the whole run in
 * order. An operator sheet reproducing that exact nine-column sequence, with a
 * `VMR Status` column of its own at the head of it, is not a thing that happens
 * by accident.
 *
 * When the run is not found the labels are treated as ordinary operator data,
 * which is the safe direction: operator data is never written to and never
 * renamed, so the worst case of guessing wrong here is that a stale generated
 * value gets submitted as though supplied — recoverable — rather than a real
 * source column being claimed and cleared, which is not.
 *
 * Returns a map of column index to `true`, empty when there is no legacy block.
 */
function legacyOutputColumns(headerRow) {
  var row = headerRow || [];
  for (var start = 0; start < row.length; start += 1) {
    if (normaliseHeader_(row[start]) !== normaliseHeader_(LEGACY_OUTPUT_SEQUENCE[0])) {
      continue;
    }
    if (start + LEGACY_OUTPUT_SEQUENCE.length > row.length) {
      continue;
    }
    var matched = true;
    for (var offset = 1; offset < LEGACY_OUTPUT_SEQUENCE.length; offset += 1) {
      var expected = normaliseHeader_(LEGACY_OUTPUT_SEQUENCE[offset]);
      if (normaliseHeader_(row[start + offset]) !== expected) {
        matched = false;
        break;
      }
    }
    if (!matched) {
      continue;
    }
    // The `VMR Status` cell at the head of the run is already VMR-owned by name
    // and stays mapped as output; only the eight unprefixed ones need hiding.
    var found = {};
    for (var index = 1; index < LEGACY_OUTPUT_SEQUENCE.length; index += 1) {
      found[start + index] = true;
    }
    return found;
  }
  return {};
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
    var row = values[index] || [];
    var candidate = mapHeaderRow(row, { ignoreColumns: legacyOutputColumns(row) });
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

/**
 * Map one row of header labels to field keys and to their column indexes.
 *
 * `options.ignoreColumns` names columns that must not be mapped whatever they
 * are called — see `legacyOutputColumns`. First match wins for any given field,
 * so a sheet carrying both `Email` and `Email Address` maps the leftmost and
 * mapping is a pure function of the row rather than of iteration order.
 */
function mapHeaderRow(row, options) {
  var ignore = (options && options.ignoreColumns) || {};
  var headers = [];
  var fields = {};
  for (var column = 0; column < row.length; column += 1) {
    var label = cellText_(row[column]);
    headers.push(label);
    if (ignore[column]) {
      continue;
    }
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

/** The column indexes an input mapping claims, as a set. */
function mappedInputColumns(mapping) {
  var claimed = {};
  if (!mapping) {
    return claimed;
  }
  for (var key in mapping) {
    if (!Object.prototype.hasOwnProperty.call(mapping, key)) {
      continue;
    }
    var column = mapping[key];
    if (column !== undefined && column !== null) {
      claimed[column] = true;
    }
  }
  return claimed;
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
 *
 * **A column the input mapping claims is never reused, whatever it is called.**
 * That is the whole of the fix for the defect this function used to have, and it
 * is deliberately expressed as a rule about *columns* rather than as a rule
 * about names. Renaming the output columns to `VMR …` removes today's
 * collisions; only refusing a claimed column removes the next one, whoever adds
 * it and whatever they call it. When the collision happens a fresh column is
 * appended after the operator's data, and the operator's own column is left
 * holding what it held.
 *
 * `options.mapping` is the field-to-column mapping from `detectHeaders`. Callers
 * that pass nothing get the old name-only behaviour, which is safe for a header
 * row that was never mapped; every caller in `Menu.js` passes it.
 *
 * First occurrence wins for a duplicated header, matching `mapHeaderRow`, so the
 * answer is a pure function of the row and not of which duplicate came last.
 */
function planOutputColumns(headerRow, options) {
  var claimed = mappedInputColumns(options && options.mapping);
  var existing = {};
  for (var column = 0; column < headerRow.length; column += 1) {
    var label = cellText_(headerRow[column]);
    if (!label || claimed[column]) {
      continue;
    }
    var normalisedLabel = label.toLowerCase();
    if (existing[normalisedLabel] === undefined) {
      existing[normalisedLabel] = column;
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
 * Whether this header row shows the sheet has been submitted at least once.
 *
 * The two columns named here are the ones that carry submission state: the row
 * key the result is matched against, and the identifier the refresh asks about.
 * A sheet holding both has been submitted, however many *other* VMR columns a
 * later version of the add-on has since added — which is exactly the question
 * `refreshResults` has to answer, and the question it used to get wrong by
 * asking instead whether the full output set was already present.
 */
function hasSubmittedColumns(headerRow) {
  var present = {};
  for (var column = 0; column < (headerRow || []).length; column += 1) {
    var label = cellText_(headerRow[column]);
    if (label) {
      present[label.toLowerCase()] = true;
    }
  }
  return Boolean(
    present[ROW_KEY_COLUMN.toLowerCase()] && present['vmr campaign contact id']
  );
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
 * Input columns are never written, and that is enforced twice. `planOutputColumns`
 * refuses to adopt a claimed column in the first place, and every edit computed
 * here is filtered against the same mapping before it is returned — so a caller
 * that forgets to hand the mapping to the planner still cannot produce a write
 * into operator data. Belt and braces for the one failure that cannot be undone
 * from inside a spreadsheet.
 */
function planResultWrites(values, options) {
  var columns = options.columns;
  var claimed = mappedInputColumns(options.mapping);
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
    // Written only when the response actually carried the field. The submit
    // response never does — it answers with an identifier and a status, not an
    // address — so treating a missing key as "the address is now nothing" turned
    // every first submission into a clearing write. A present-but-null value is
    // still honoured: that is the server saying this row has no address, which is
    // a fact about a VMR-owned column and belongs in it.
    if (Object.prototype.hasOwnProperty.call(payload, 'email_address')) {
      cells[columns['VMR Email Address']] = payload.email_address || '';
    }
    var messages = payload.messages || [];
    for (var position = 1; position <= 7; position += 1) {
      var message = null;
      for (var m = 0; m < messages.length; m += 1) {
        if (messages[m] && messages[m].sequence_index === position) {
          message = messages[m];
        }
      }
      // Cleared rather than left stale on a row that is no longer ready. These
      // are VMR-owned columns holding a generated sequence, and half of an old
      // sequence beside a new status is worse than an empty cell.
      cells[columns['VMR Email ' + position]] = message ? renderMessage(message) : '';
    }
    cells[columns['VMR Note']] = payload.safe_failure_reason || payload.note || '';
    cells[columns['VMR Last Updated']] = timestamp;
    if (payload.contact_id) {
      cells[columns['VMR Contact ID']] = payload.contact_id;
    }
    if (payload.submission_id) {
      cells[columns['VMR Campaign Contact ID']] = payload.submission_id;
    }
    edits.push({ rowIndex: rowIndex, cells: safeCells_(cells, claimed) });
  }
  return edits;
}

/** Drop any cell that names a column the input mapping claims. */
function safeCells_(cells, claimed) {
  var safe = {};
  for (var column in cells) {
    if (!Object.prototype.hasOwnProperty.call(cells, column)) {
      continue;
    }
    if (claimed[column] || claimed[Number(column)]) {
      continue;
    }
    safe[column] = cells[column];
  }
  return safe;
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
    LEGACY_OUTPUT_SEQUENCE: LEGACY_OUTPUT_SEQUENCE,
    OUTPUT_COLUMNS: OUTPUT_COLUMNS,
    ROW_KEY_COLUMN: ROW_KEY_COLUMN,
    STATUS_LABELS: STATUS_LABELS,
    allDataRows: allDataRows,
    applyEdits: applyEdits,
    assignRowKeys: assignRowKeys,
    buildRows: buildRows,
    detectHeaders: detectHeaders,
    hasSubmittedColumns: hasSubmittedColumns,
    legacyOutputColumns: legacyOutputColumns,
    mapHeaderRow: mapHeaderRow,
    mappedInputColumns: mappedInputColumns,
    missingRequiredFields: missingRequiredFields,
    planOutputColumns: planOutputColumns,
    planResultWrites: planResultWrites,
    renderMessage: renderMessage,
    summarise: summarise,
  };
}
