'use strict';

/**
 * The sheet-side proofs.
 *
 * The question these answer is not "does the add-on work" — a UAT answers that.
 * It is the narrower and much nastier one: **can a result ever land on the wrong
 * row?** Every mechanism that could cause it is exercised here — sorting,
 * insertion, deletion, a partially refreshed sheet, a duplicated key column, a
 * retried submission — against a fabricated grid, with no Google account.
 */

const test = require('node:test');
const assert = require('node:assert/strict');

const { loadAddon, FakeSheet } = require('./load');

const HEADER = ['First Name', 'Last Name', 'Company', 'Title', 'Notes'];

function grid(rows, { titleRow = false } = {}) {
  const values = [];
  if (titleRow) {
    values.push(['Q3 prospect list', '', '', '', '']);
  }
  values.push(HEADER.slice());
  for (const row of rows) {
    values.push(row.slice());
  }
  return values;
}

function addon() {
  return loadAddon();
}

// ---------------------------------------------------------------------------
// 1. Header detection and mapping
// ---------------------------------------------------------------------------

test('the header row is found even when it is not the first row', () => {
  const context = addon();
  const values = grid([['Ada', 'Lovelace', 'Kiln Systems', 'Head of Research', '']], {
    titleRow: true,
  });
  const detected = context.detectHeaders(values);

  assert.equal(detected.found, true);
  assert.equal(detected.rowIndex, 1);
  assert.deepEqual(detected.fields, {
    first_name: 0,
    last_name: 1,
    company_name: 2,
    job_title: 3,
    context: 4,
  });
});

test('common header spellings map to the same field', () => {
  const context = addon();
  const spellings = [
    ['FIRST NAME', 'SURNAME', 'Organisation'],
    ['first_name', 'last_name', 'company'],
    ['Given Name', 'Family Name', 'Account'],
  ];
  for (const row of spellings) {
    const mapped = context.mapHeaderRow(row);
    assert.deepEqual(mapped.fields, { first_name: 0, last_name: 1, company_name: 2 }, row.join('|'));
  }
});

test('a sheet with no recognisable header is reported as such, not guessed at', () => {
  const context = addon();
  const detected = context.detectHeaders([
    ['alpha', 'beta', 'gamma'],
    ['1', '2', '3'],
  ]);
  assert.equal(detected.found, false);
  assert.deepEqual(context.missingRequiredFields(detected.fields), [
    'First Name',
    'Last Name',
    'Company Name',
  ]);
});

test('a missing required column is named, and optional ones are not', () => {
  const context = addon();
  const detected = context.detectHeaders([['First Name', 'Company']]);
  assert.deepEqual(context.missingRequiredFields(detected.fields), ['Last Name']);
});

// ---------------------------------------------------------------------------
// 2. Output columns
// ---------------------------------------------------------------------------

test('output columns are planned after the operator data and never over it', () => {
  const context = addon();
  const plan = context.planOutputColumns(HEADER.slice());

  assert.equal(plan.create.length, context.OUTPUT_COLUMNS.length + 1);
  assert.equal(plan.columns[context.ROW_KEY_COLUMN], HEADER.length);
  for (const name of context.OUTPUT_COLUMNS) {
    assert.ok(plan.columns[name] >= HEADER.length, name + ' would overwrite operator data');
  }
});

test('an existing VMR column is reused wherever the operator moved it', () => {
  const context = addon();
  const header = ['VMR Status', 'First Name', 'Last Name', 'Company'];
  const plan = context.planOutputColumns(header);

  assert.equal(plan.columns['VMR Status'], 0);
  assert.ok(!plan.create.some((entry) => entry.name === 'VMR Status'));
});

test('creating the columns hides the row-key column', () => {
  const values = grid([['Ada', 'Lovelace', 'Kiln Systems', '', '']]);
  const sheet = new FakeSheet(values);
  const context = addon();
  const plan = context.planOutputColumns(values[0]);

  context.createOutputColumns(sheet, plan);

  assert.deepEqual(sheet.hiddenColumns, [plan.columns[context.ROW_KEY_COLUMN] + 1]);
  assert.equal(sheet.values[0][plan.columns['VMR Status']], 'VMR Status');
});

// ---------------------------------------------------------------------------
// 3. Row keys
// ---------------------------------------------------------------------------

test('every data row gets a key, and a row that has one keeps it', () => {
  const context = addon();
  const values = [
    ['First Name', 'Last Name', 'Company', 'VMR Row Key'],
    ['Ada', 'Lovelace', 'Kiln', 'already-here'],
    ['Grace', 'Hopper', 'Kiln', ''],
    ['', '', '', ''],
  ];
  let counter = 0;
  const assigned = context.assignRowKeys(values, {
    keyColumn: 3,
    firstDataRow: 1,
    mintKey: () => 'minted-' + (counter += 1),
  });

  // Only the one blank data row is minted for; the empty row is not a row.
  assert.deepEqual(assigned, [{ rowIndex: 2, key: 'minted-1' }]);
});

test('assigning keys twice mints nothing the second time', () => {
  const context = addon();
  const values = [
    ['First Name', 'Last Name', 'Company', 'VMR Row Key'],
    ['Ada', 'Lovelace', 'Kiln', ''],
  ];
  let counter = 0;
  const mintKey = () => 'minted-' + (counter += 1);
  const first = context.assignRowKeys(values, { keyColumn: 3, firstDataRow: 1, mintKey });
  values[1][3] = first[0].key;
  const second = context.assignRowKeys(values, { keyColumn: 3, firstDataRow: 1, mintKey });

  assert.equal(first.length, 1);
  assert.deepEqual(second, []);
});

// ---------------------------------------------------------------------------
// 4-5. Serialising a selection
// ---------------------------------------------------------------------------

test('a selection serialises to the payload the backend accepts', () => {
  const context = addon();
  const values = [
    ['First Name', 'Last Name', 'Company', 'Title', 'Notes', 'VMR Row Key'],
    ['Ada', 'Lovelace', 'Kiln Systems', 'Head of Research', 'Met at the summit', 'k1'],
  ];
  const built = context.buildRows(values, {
    mapping: { first_name: 0, last_name: 1, company_name: 2, job_title: 3, context: 4 },
    keyColumn: 5,
    rows: [1],
  });

  assert.deepEqual(built[0].payload, {
    client_row_id: 'k1',
    first_name: 'Ada',
    last_name: 'Lovelace',
    company_name: 'Kiln Systems',
    job_title: 'Head of Research',
    context: 'Met at the summit',
  });
});

test('an empty optional cell is omitted rather than sent as an empty string', () => {
  const context = addon();
  const values = [
    ['First Name', 'Last Name', 'Company', 'Title', 'VMR Row Key'],
    ['Ada', 'Lovelace', 'Kiln Systems', '   ', 'k1'],
  ];
  const built = context.buildRows(values, {
    mapping: { first_name: 0, last_name: 1, company_name: 2, job_title: 3 },
    keyColumn: 4,
    rows: [1],
  });

  assert.deepEqual(Object.keys(built[0].payload).sort(), [
    'client_row_id',
    'company_name',
    'first_name',
    'last_name',
  ]);
});

test('a row with a blank required cell is still sent, so the sheet hears the refusal', () => {
  const context = addon();
  const values = [
    ['First Name', 'Last Name', 'Company', 'VMR Row Key'],
    ['Ada', '', 'Kiln Systems', 'k1'],
  ];
  const built = context.buildRows(values, {
    mapping: { first_name: 0, last_name: 1, company_name: 2 },
    keyColumn: 3,
    rows: [1],
  });

  assert.equal(built.length, 1);
  assert.equal(built[0].payload.last_name, undefined);
});

// ---------------------------------------------------------------------------
// 6-7. Writing results back to the right row
// ---------------------------------------------------------------------------

function readyResult(key, submissionId) {
  const messages = [];
  const days = [0, 3, 7, 12, 18, 25, 35];
  for (let index = 1; index <= 7; index += 1) {
    messages.push({
      sequence_index: index,
      elapsed_day: days[index - 1],
      subject: 'Subject ' + index,
      body: 'Body ' + index,
    });
  }
  return {
    client_row_id: key,
    submission_id: submissionId,
    status: 'ready',
    email_address: key + '@kiln.example',
    messages: messages,
  };
}

function resultGrid(context) {
  const header = ['First Name', 'Last Name', 'Company'];
  const plan = context.planOutputColumns(header);
  const width = plan.totalColumns;
  const blank = () => new Array(width).fill('');
  const values = [blank(), blank(), blank()];
  values[0] = header.concat(new Array(width - header.length).fill(''));
  for (const entry of plan.create) {
    values[0][entry.column] = entry.name;
  }
  values[1][0] = 'Ada';
  values[1][1] = 'Lovelace';
  values[1][2] = 'Kiln Systems';
  values[1][plan.columns[context.ROW_KEY_COLUMN]] = 'k-ada';
  values[2][0] = 'Grace';
  values[2][1] = 'Hopper';
  values[2][2] = 'Kiln Systems';
  values[2][plan.columns[context.ROW_KEY_COLUMN]] = 'k-grace';
  return { plan, values };
}

test('a ready row receives the address and all seven messages', () => {
  const context = addon();
  const { plan, values } = resultGrid(context);
  const edits = context.planResultWrites(values, {
    columns: plan.columns,
    firstDataRow: 1,
    results: [readyResult('k-ada', 'sub-1')],
    timestamp: '2026-08-15T00:00:00Z',
  });
  const written = context.applyEdits(values, edits, plan.totalColumns);

  assert.equal(written[1][plan.columns['VMR Status']], 'Ready');
  assert.equal(written[1][plan.columns['Email Address']], 'k-ada@kiln.example');
  for (let index = 1; index <= 7; index += 1) {
    const cell = written[1][plan.columns['Email ' + index]];
    assert.ok(cell.includes('Subject ' + index), 'Email ' + index + ' subject');
    assert.ok(cell.includes('Body ' + index), 'Email ' + index + ' body');
  }
  assert.equal(written[1][plan.columns['VMR Campaign Contact ID']], 'sub-1');
  // The untouched row stays untouched.
  assert.equal(written[2][plan.columns['VMR Status']], '');
});

test('sorting the sheet between submit and refresh still writes to the right person', () => {
  const context = addon();
  const { plan, values } = resultGrid(context);
  // The operator sorts the sheet: Grace is now above Ada, keys travel with them.
  const sorted = [values[0], values[2], values[1]];

  const edits = context.planResultWrites(sorted, {
    columns: plan.columns,
    firstDataRow: 1,
    results: [readyResult('k-ada', 'sub-1')],
    timestamp: 't',
  });
  const written = context.applyEdits(sorted, edits, plan.totalColumns);

  assert.equal(written[1][0], 'Grace');
  assert.equal(written[1][plan.columns['VMR Status']], '');
  assert.equal(written[2][0], 'Ada');
  assert.equal(written[2][plan.columns['Email Address']], 'k-ada@kiln.example');
});

test('a row whose key was deleted receives nothing rather than somebody elses result', () => {
  const context = addon();
  const { plan, values } = resultGrid(context);
  values[1][plan.columns[context.ROW_KEY_COLUMN]] = '';

  const edits = context.planResultWrites(values, {
    columns: plan.columns,
    firstDataRow: 1,
    results: [readyResult('k-ada', 'sub-1')],
    timestamp: 't',
  });

  assert.deepEqual(edits, []);
});

test('a result never writes outside the VMR columns', () => {
  const context = addon();
  const { plan, values } = resultGrid(context);
  const inputColumns = new Set([0, 1, 2]);

  const edits = context.planResultWrites(values, {
    columns: plan.columns,
    firstDataRow: 1,
    results: [readyResult('k-ada', 'sub-1'), readyResult('k-grace', 'sub-2')],
    timestamp: 't',
  });

  for (const edit of edits) {
    for (const column of Object.keys(edit.cells)) {
      assert.ok(!inputColumns.has(Number(column)), 'wrote into an operator column');
    }
  }
});

// ---------------------------------------------------------------------------
// 8-9. Failures and partial states
// ---------------------------------------------------------------------------

test('a refusal writes the status and the reason, and clears no input', () => {
  const context = addon();
  const { plan, values } = resultGrid(context);
  const before = values[1].slice(0, 3);

  const edits = context.planResultWrites(values, {
    columns: plan.columns,
    firstDataRow: 1,
    results: [
      {
        client_row_id: 'k-ada',
        status: 'could_not_prepare',
        safe_failure_reason: 'the company could not be identified from this name',
      },
    ],
    timestamp: 't',
  });
  const written = context.applyEdits(values, edits, plan.totalColumns);

  assert.equal(written[1][plan.columns['VMR Status']], 'Could not prepare');
  assert.ok(written[1][plan.columns['VMR Note']].includes('could not be identified'));
  assert.equal(written[1][plan.columns['Email 1']], '');
  assert.deepEqual(written[1].slice(0, 3), before);
});

test('a processing row shows no half-written sequence', () => {
  const context = addon();
  const { plan, values } = resultGrid(context);
  const edits = context.planResultWrites(values, {
    columns: plan.columns,
    firstDataRow: 1,
    results: [{ client_row_id: 'k-ada', status: 'processing', messages: [] }],
    timestamp: 't',
  });
  const written = context.applyEdits(values, edits, plan.totalColumns);

  assert.equal(written[1][plan.columns['VMR Status']], 'Processing');
  for (let index = 1; index <= 7; index += 1) {
    assert.equal(written[1][plan.columns['Email ' + index]], '');
  }
});

// ---------------------------------------------------------------------------
// 10. Retries and refresh scope
// ---------------------------------------------------------------------------

test('a refresh asks only about rows that are submitted and not finished', () => {
  const context = addon();
  const { plan, values } = resultGrid(context);
  values[1][plan.columns['VMR Campaign Contact ID']] = 'sub-1';
  values[1][plan.columns['VMR Status']] = 'Ready';
  values[2][plan.columns['VMR Campaign Contact ID']] = 'sub-2';
  values[2][plan.columns['VMR Status']] = 'Processing';

  const pending = context.knownSubmissions(values, plan.columns, 1);

  assert.deepEqual(pending, [{ submissionId: 'sub-2', clientRowId: 'k-grace' }]);
});

test('a row that was never submitted is not asked about', () => {
  const context = addon();
  const { plan, values } = resultGrid(context);
  const pending = context.knownSubmissions(values, plan.columns, 1);
  assert.deepEqual(pending, []);
});

test('a second submission of the same rows reuses the keys the first one wrote', () => {
  const context = addon();
  const { plan, values } = resultGrid(context);
  const keyColumn = plan.columns[context.ROW_KEY_COLUMN];
  let minted = 0;

  const again = context.assignRowKeys(values, {
    keyColumn: keyColumn,
    firstDataRow: 1,
    mintKey: () => 'should-not-happen-' + (minted += 1),
  });

  assert.deepEqual(again, []);
  assert.equal(minted, 0);
  const built = context.buildRows(values, {
    mapping: { first_name: 0, last_name: 1, company_name: 2 },
    keyColumn: keyColumn,
    rows: [1, 2],
  });
  assert.deepEqual(
    built.map((entry) => entry.payload.client_row_id),
    ['k-ada', 'k-grace']
  );
});

// ---------------------------------------------------------------------------
// Rendering and selection
// ---------------------------------------------------------------------------

test('a message cell carries its day, its subject and its body', () => {
  const context = addon();
  const rendered = context.renderMessage({
    sequence_index: 3,
    elapsed_day: 7,
    subject: 'A short note',
    body: 'Hello there.',
  });
  assert.equal(rendered, 'Day 7 — Subject: A short note\n\nHello there.');
});

test('the selection ignores the header row and de-duplicates overlapping ranges', () => {
  const context = addon();
  const sheet = new FakeSheet(grid([['Ada', 'L', 'Kiln'], ['Grace', 'H', 'Kiln']]));
  sheet.setSelection([
    [1, 2],
    [2, 2],
  ]);
  assert.deepEqual(context.selectedRowIndexes(sheet, 1), [1, 2]);
});

test('the summary counts each state once', () => {
  const context = addon();
  assert.deepEqual(
    context.summarise([
      { status: 'ready' },
      { status: 'ready' },
      { status: 'processing' },
      { status: 'could_not_prepare' },
      { status: 'unknown-to-this-version' },
    ]),
    { pending: 0, processing: 1, ready: 2, could_not_prepare: 1 }
  );
});
