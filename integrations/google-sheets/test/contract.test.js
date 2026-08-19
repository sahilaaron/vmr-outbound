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
// The sheet that broke, and one round-trip over it
// ---------------------------------------------------------------------------

/**
 * A header whose own columns are called the things the add-on used to call its
 * own. Every column after the third is operator data, and none of it may ever
 * be written to.
 */
const COLLIDING_HEADER = [
  'First Name',
  'Last Name',
  'Company Name',
  'Email Address',
  'Company Website',
  'Email 1',
  'Email 2',
];
const COLLIDING_ROW = [
  'Ada',
  'Lovelace',
  'Kiln Systems',
  'ada@kiln.example',
  'https://kiln.example/about',
  'first-touch draft',
  'second-touch draft',
];

/** The header the pre-repair client left behind, in the order it created it. */
const LEGACY_HEADER = [
  'First Name',
  'Last Name',
  'Company Name',
  'VMR Row Key',
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
 * Every shape a result can take, including the one the submit response has.
 *
 * The submit response carries no `email_address` key at all — it answers with an
 * identifier and a status — and that omission is what used to blank the column.
 * The refresh response always carries the key, sometimes null.
 */
const EVERY_RESULT_SHAPE = [
  { status: 'pending', submission_id: 'sub-1' },
  { status: 'pending', email_address: null },
  { status: 'processing', email_address: null },
  { status: 'could_not_prepare', email_address: null, safe_failure_reason: 'no address found' },
  { status: 'ready', email_address: 'a.lovelace@kiln.example', messages: [] },
];

/**
 * One submission, exactly as `Menu.js` performs it, over a fabricated grid.
 *
 * Detect the header, plan the output columns *with the mapping*, write the new
 * headings, mint keys, and build the payload. Doing the whole sequence rather
 * than calling one function is the point: the defect was in how two of these
 * steps combined, and either one alone looked correct.
 */
function round(context, header, dataRows) {
  const values = [header.slice()].concat(dataRows.map((row) => row.slice()));
  const detected = context.detectHeaders(values);
  const plan = context.planOutputColumns(values[detected.rowIndex], { mapping: detected.fields });
  const grid = values.map((row) => {
    const padded = row.slice();
    while (padded.length < plan.totalColumns) {
      padded.push('');
    }
    return padded;
  });
  for (const entry of plan.create) {
    grid[detected.rowIndex][entry.column] = entry.name;
  }
  const keyColumn = plan.columns[context.ROW_KEY_COLUMN];
  const firstDataRow = detected.rowIndex + 1;
  let minted = 0;
  const assignments = context.assignRowKeys(grid, {
    keyColumn: keyColumn,
    firstDataRow: firstDataRow,
    mintKey: () => 'k' + (minted += 1),
  });
  for (const assignment of assignments) {
    grid[assignment.rowIndex][keyColumn] = assignment.key;
  }
  const built = context.buildRows(grid, {
    mapping: detected.fields,
    keyColumn: keyColumn,
    rows: context.allDataRows(grid, firstDataRow),
  });
  return {
    detected,
    plan,
    grid,
    keyColumn,
    firstDataRow,
    headerWidth: header.length,
    payloads: built.map((entry) => entry.payload),
  };
}

/** Apply results to a round, the way `Menu.js` does. */
function write(context, state, results) {
  const edits = context.planResultWrites(state.grid, {
    columns: state.plan.columns,
    mapping: state.detected.fields,
    firstDataRow: state.firstDataRow,
    results: results,
    timestamp: 't',
  });
  return { edits, after: context.applyEdits(state.grid, edits, state.plan.totalColumns) };
}

/** The operator's own cells, before and after, for one data row. */
function operatorCells(state, grid) {
  return grid[state.firstDataRow].slice(0, state.headerWidth);
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
  assert.equal(written[1][plan.columns['VMR Email Address']], 'k-ada@kiln.example');
  for (let index = 1; index <= 7; index += 1) {
    const cell = written[1][plan.columns['VMR Email ' + index]];
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
  assert.equal(written[2][plan.columns['VMR Email Address']], 'k-ada@kiln.example');
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

// This assertion used to be made only against `First Name | Last Name | Company`
// — a header that cannot collide with any output name, so the invariant it reads
// as proving could not fail whatever the code did. It is restated here against
// the header that actually broke: one whose own columns are called the things
// the add-on wanted to call its own, with the claimed columns taken from the
// real mapping rather than written down by hand.
test('no edit ever targets a column the input mapping claims', () => {
  const context = addon();
  const state = round(context, COLLIDING_HEADER, [COLLIDING_ROW]);
  const claimed = context.mappedInputColumns(state.detected.fields);

  for (const payload of EVERY_RESULT_SHAPE) {
    const { edits } = write(context, state, [Object.assign({ client_row_id: 'k1' }, payload)]);
    assert.ok(edits.length, 'the row should have received a result');
    for (const edit of edits) {
      for (const column of Object.keys(edit.cells)) {
        assert.ok(!claimed[Number(column)], 'wrote into operator column ' + column);
      }
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
  assert.equal(written[1][plan.columns['VMR Email 1']], '');
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
    assert.equal(written[1][plan.columns['VMR Email ' + index]], '');
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

// ---------------------------------------------------------------------------
// 11. Operator-owned columns survive, whatever the result says
//
// The defect these exist for: `Email Address` and `Email 1`…`Email 7` were both
// output column names and extremely common source column names, and
// `planOutputColumns` adopted an existing column by name. A source column was
// therefore claimed as VMR output and cleared the first time a result carried no
// address — on the submit response, which never carries one, so on the very
// first click. Every case below is that sheet.
// ---------------------------------------------------------------------------

test('the operator Email Address column survives a submit response', () => {
  const context = addon();
  const state = round(context, COLLIDING_HEADER, [COLLIDING_ROW]);
  const before = operatorCells(state, state.grid);

  // Exactly what `POST /integrations/sheets/batches` answers with: no address.
  const { after } = write(context, state, [
    { client_row_id: 'k1', status: 'pending', submission_id: 'sub-1', contact_id: 'c-1' },
  ]);

  assert.deepEqual(operatorCells(state, after), before);
  assert.equal(after[1][state.detected.fields.email], 'ada@kiln.example');
});

for (const shape of EVERY_RESULT_SHAPE) {
  test('the operator Email Address column survives a ' + shape.status + ' result', () => {
    const context = addon();
    const state = round(context, COLLIDING_HEADER, [COLLIDING_ROW]);
    const before = operatorCells(state, state.grid);

    const { after } = write(context, state, [Object.assign({ client_row_id: 'k1' }, shape)]);

    assert.deepEqual(operatorCells(state, after), before);
    assert.equal(after[1][state.detected.fields.email], 'ada@kiln.example');
  });
}

test('operator columns called Email 1 and Email 2 survive every result', () => {
  const context = addon();
  for (const shape of EVERY_RESULT_SHAPE) {
    const state = round(context, COLLIDING_HEADER, [COLLIDING_ROW]);
    const { after } = write(context, state, [Object.assign({ client_row_id: 'k1' }, shape)]);
    assert.equal(after[1][5], 'first-touch draft', shape.status);
    assert.equal(after[1][6], 'second-touch draft', shape.status);
  }
});

test('a source Company Website column is never overwritten', () => {
  const context = addon();
  for (const shape of EVERY_RESULT_SHAPE) {
    const state = round(context, COLLIDING_HEADER, [COLLIDING_ROW]);
    const { after } = write(context, state, [Object.assign({ client_row_id: 'k1' }, shape)]);
    assert.equal(after[1][state.detected.fields.website], 'https://kiln.example/about');
  }
});

// ---------------------------------------------------------------------------
// 12. The supplied inputs actually reach the backend
// ---------------------------------------------------------------------------

test('a supplied address and website travel as the wire keys the server reads', () => {
  const context = addon();
  const state = round(context, COLLIDING_HEADER, [COLLIDING_ROW]);

  assert.deepEqual(state.payloads, [
    {
      client_row_id: 'k1',
      first_name: 'Ada',
      last_name: 'Lovelace',
      company_name: 'Kiln Systems',
      email: 'ada@kiln.example',
      website: 'https://kiln.example/about',
    },
  ]);
});

test('the wire keys are exactly email and website, and no synonym of them', () => {
  const context = addon();
  const keys = context.INPUT_FIELDS.map((field) => field.key);

  assert.ok(keys.includes('email'), 'the address must be sent as email');
  assert.ok(keys.includes('website'), 'the website must be sent as website');
  for (const invented of ['email_address', 'supplied_email', 'company_website', 'company_domain']) {
    assert.ok(!keys.includes(invented), invented + ' is not a field the server reads');
  }
});

for (const pair of [
  ['Email', 'email'],
  ['Email Address', 'email'],
  ['E-Mail', 'email'],
  ['Work Email', 'email'],
  ['Business Email', 'email'],
  ['Corporate Email', 'email'],
  ['Company Website', 'website'],
  ['Website', 'website'],
  ['Company Domain', 'website'],
  ['Domain', 'website'],
  ['Company URL', 'website'],
  ['Web Site', 'website'],
]) {
  test('the header "' + pair[0] + '" maps to ' + pair[1], () => {
    const context = addon();
    const detected = context.detectHeaders([['First Name', 'Last Name', 'Company Name', pair[0]]]);
    assert.equal(detected.fields[pair[1]], 3);
  });
}

test('a blank supplied cell is omitted rather than sent as an empty string', () => {
  const context = addon();
  const state = round(context, COLLIDING_HEADER, [
    ['Ada', 'Lovelace', 'Kiln Systems', '', '', '', ''],
  ]);

  assert.ok(!('email' in state.payloads[0]), 'a blank address must not be sent');
  assert.ok(!('website' in state.payloads[0]), 'a blank website must not be sent');
});

// ---------------------------------------------------------------------------
// 13. The structural guard, not the naming convention
// ---------------------------------------------------------------------------

test('an output column that collides with a claimed input gets a fresh column', () => {
  const context = addon();
  // Deliberately artificial: no header spelling produces this collision today.
  // That is the point — the guard has to hold for the collision somebody
  // introduces later, not only for the two that were found.
  const header = ['First Name', 'Last Name', 'Company Name', 'VMR Email Address'];
  const plan = context.planOutputColumns(header, { mapping: { email: 3 } });

  assert.ok(plan.columns['VMR Email Address'] >= header.length, 'must not reuse the claimed column');
  assert.ok(plan.create.some((entry) => entry.name === 'VMR Email Address'));
  assert.ok(
    Object.keys(plan.columns).every((name) => plan.columns[name] !== 3),
    'no output column may land on a claimed input column'
  );
});

test('an edit naming a claimed input column is dropped before it is returned', () => {
  const context = addon();
  const state = round(context, COLLIDING_HEADER, [COLLIDING_ROW]);
  // A caller that planned its columns without the mapping, and so aimed the
  // address straight at the operator's own column.
  const columns = Object.assign({}, state.plan.columns, {
    'VMR Email Address': state.detected.fields.email,
  });

  const edits = context.planResultWrites(state.grid, {
    columns: columns,
    mapping: state.detected.fields,
    firstDataRow: state.firstDataRow,
    results: [{ client_row_id: 'k1', status: 'ready', email_address: 'x@kiln.example' }],
    timestamp: 't',
  });

  assert.equal(edits.length, 1);
  assert.ok(!(state.detected.fields.email in edits[0].cells), 'the unsafe cell must be dropped');
});

test('every VMR output column is VMR-prefixed', () => {
  const context = addon();
  for (const name of context.OUTPUT_COLUMNS.concat([context.ROW_KEY_COLUMN])) {
    assert.ok(/^VMR /.test(name), name + ' is not visibly VMR-owned');
  }
});

test('the VMR address and message columns are created in their own right', () => {
  const context = addon();
  const state = round(context, COLLIDING_HEADER, [COLLIDING_ROW]);
  const created = state.plan.create.map((entry) => entry.name);

  assert.ok(created.includes('VMR Email Address'));
  for (let index = 1; index <= 7; index += 1) {
    assert.ok(created.includes('VMR Email ' + index), 'VMR Email ' + index);
  }
  for (const name of created) {
    assert.ok(
      state.plan.columns[name] >= state.headerWidth,
      name + ' was planned over operator data'
    );
  }
});

// ---------------------------------------------------------------------------
// 14. Partial results
// ---------------------------------------------------------------------------

test('a result without an email_address key does not blank VMR Email Address', () => {
  const context = addon();
  const state = round(context, COLLIDING_HEADER, [COLLIDING_ROW]);
  const address = state.plan.columns['VMR Email Address'];

  const ready = write(context, state, [
    {
      client_row_id: 'k1',
      status: 'ready',
      email_address: 'a.lovelace@kiln.example',
      messages: [],
    },
  ]);
  assert.equal(ready.after[1][address], 'a.lovelace@kiln.example');

  // The same sheet, resubmitted: the submit response omits the key entirely.
  state.grid = ready.after;
  const resubmitted = write(context, state, [
    { client_row_id: 'k1', status: 'pending', submission_id: 'sub-2' },
  ]);

  assert.equal(resubmitted.after[1][address], 'a.lovelace@kiln.example');
  assert.equal(resubmitted.after[1][state.plan.columns['VMR Status']], 'Pending');
});

test('a ready result writes the address VMR produced into the VMR column', () => {
  const context = addon();
  const state = round(context, COLLIDING_HEADER, [COLLIDING_ROW]);
  const { after } = write(context, state, [
    {
      client_row_id: 'k1',
      status: 'ready',
      email_address: 'a.lovelace@kiln.example',
      messages: [],
    },
  ]);

  assert.equal(after[1][state.plan.columns['VMR Email Address']], 'a.lovelace@kiln.example');
  // And the operator's own supplied address is still theirs.
  assert.equal(after[1][state.detected.fields.email], 'ada@kiln.example');
});

test('an explicit null address is honoured, because the column is VMR-owned', () => {
  const context = addon();
  const state = round(context, COLLIDING_HEADER, [COLLIDING_ROW]);
  const address = state.plan.columns['VMR Email Address'];
  state.grid[1][address] = 'stale@kiln.example';

  const { after } = write(context, state, [
    { client_row_id: 'k1', status: 'processing', email_address: null },
  ]);

  assert.equal(after[1][address], '');
});

// ---------------------------------------------------------------------------
// 15. Sheets the previous client already wrote to
// ---------------------------------------------------------------------------

test('the legacy output block is recognised only as the exact run it was', () => {
  const context = addon();
  const found = context.legacyOutputColumns(LEGACY_HEADER);

  assert.deepEqual(
    Object.keys(found)
      .map(Number)
      .sort((left, right) => left - right),
    [5, 6, 7, 8, 9, 10, 11, 12]
  );
  // A sheet that merely happens to have one of those names is not a legacy block.
  assert.deepEqual(
    context.legacyOutputColumns(['First Name', 'Last Name', 'Company Name', 'Email Address']),
    {}
  );
  // Nor is a partial run.
  assert.deepEqual(context.legacyOutputColumns(['VMR Status', 'Email Address', 'Email 2']), {});
});

test('a legacy VMR address column is not read back as an operator-supplied one', () => {
  const context = addon();
  const detected = context.detectHeaders([LEGACY_HEADER]);

  assert.equal(detected.fields.email, undefined, 'a generated address is not an assertion');
  assert.equal(detected.fields.first_name, 0);
  assert.equal(detected.fields.company_name, 2);
});

test('an operator email column on a legacy sheet is still mapped', () => {
  const context = addon();
  const detected = context.detectHeaders([LEGACY_HEADER.concat(['Work Email'])]);

  assert.equal(detected.fields.email, LEGACY_HEADER.length);
});

test('a legacy sheet keeps its row keys and gains the new columns', () => {
  const context = addon();
  const detected = context.detectHeaders([LEGACY_HEADER]);
  const plan = context.planOutputColumns(LEGACY_HEADER, { mapping: detected.fields });

  // Everything that carries state stays exactly where the old client put it.
  assert.equal(plan.columns[context.ROW_KEY_COLUMN], 3);
  assert.equal(plan.columns['VMR Status'], 4);
  assert.equal(plan.columns['VMR Campaign Contact ID'], 16);
  assert.ok(!plan.create.some((entry) => entry.name === context.ROW_KEY_COLUMN));

  // The new columns are appended; nothing is renamed and nothing is reused.
  assert.deepEqual(
    plan.create.map((entry) => entry.name),
    [
      'VMR Email Address',
      'VMR Email 1',
      'VMR Email 2',
      'VMR Email 3',
      'VMR Email 4',
      'VMR Email 5',
      'VMR Email 6',
      'VMR Email 7',
    ]
  );
  for (const entry of plan.create) {
    assert.ok(entry.column >= LEGACY_HEADER.length, entry.name + ' overwrote an existing column');
  }
});

test('a refresh recognises an already-submitted sheet by its state columns alone', () => {
  const context = addon();

  assert.equal(context.hasSubmittedColumns(LEGACY_HEADER), true);
  assert.equal(context.hasSubmittedColumns(COLLIDING_HEADER), false);
  // A sheet holding only half of the pair has not completed a submission.
  assert.equal(context.hasSubmittedColumns(['First Name', 'VMR Row Key']), false);
});

// ---------------------------------------------------------------------------
// 16. Duplicates and ordering
// ---------------------------------------------------------------------------

test('a duplicated VMR column resolves to the first one, deterministically', () => {
  const context = addon();
  const header = ['VMR Status', 'First Name', 'Last Name', 'Company Name', 'VMR Status'];
  const plan = context.planOutputColumns(header, { mapping: {} });

  assert.equal(plan.columns['VMR Status'], 0);
  assert.deepEqual(plan, context.planOutputColumns(header.slice(), { mapping: {} }));
});

test('a duplicated operator Email Address column is neither mapped twice nor written', () => {
  const context = addon();
  const header = ['First Name', 'Last Name', 'Company Name', 'Email Address', 'Email Address'];
  const state = round(context, header, [
    ['Ada', 'Lovelace', 'Kiln Systems', 'ada@kiln.example', 'ada.alt@kiln.example'],
  ]);

  assert.equal(state.detected.fields.email, 3, 'the leftmost wins');
  assert.equal(state.payloads[0].email, 'ada@kiln.example');

  const { after } = write(context, state, [
    { client_row_id: 'k1', status: 'processing', email_address: null },
  ]);
  assert.equal(after[1][3], 'ada@kiln.example');
  assert.equal(after[1][4], 'ada.alt@kiln.example');
});

test('sorting a colliding sheet between submit and refresh still maps by row key', () => {
  const context = addon();
  const state = round(context, COLLIDING_HEADER, [
    COLLIDING_ROW,
    ['Grace', 'Hopper', 'Kiln Systems', 'grace@kiln.example', 'kiln.example', 'draft a', 'draft b'],
  ]);
  // The operator sorts; keys travel with their rows.
  state.grid = [state.grid[0], state.grid[2], state.grid[1]];

  const { after } = write(context, state, [
    { client_row_id: 'k1', status: 'ready', email_address: 'ada@vmr.example', messages: [] },
    { client_row_id: 'k2', status: 'processing', email_address: null },
  ]);

  const address = state.plan.columns['VMR Email Address'];
  assert.equal(after[1][0], 'Grace');
  assert.equal(after[1][address], '');
  assert.equal(after[2][0], 'Ada');
  assert.equal(after[2][address], 'ada@vmr.example');
  // And both operators' own addresses are untouched.
  assert.equal(after[1][state.detected.fields.email], 'grace@kiln.example');
  assert.equal(after[2][state.detected.fields.email], 'ada@kiln.example');
});
