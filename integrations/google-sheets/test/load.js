'use strict';

/**
 * Load the add-on's `.gs` sources into one sandbox, the way Apps Script does.
 *
 * Apps Script has no modules: every file shares one global scope, and a function
 * defined in `Contract.js` is simply visible from `Sheet.js`. Requiring the files
 * individually would test something with different scoping rules from the thing
 * that ships, so the tests evaluate them together in a `vm` context with the
 * Apps Script globals they touch supplied as fakes.
 */

const fs = require('node:fs');
const path = require('node:path');

const SOURCE_DIR = path.join(__dirname, '..', 'src');
const SOURCE_FILES = ['Contract.js', 'Sheet.js', 'Api.js', 'Menu.js'];

// Top-level `function name(` and `var name =` declarations. Both forms are what
// Apps Script's shared scope is made of, and collecting them is how this loader
// hands the whole surface back without every file needing an export list it does
// not have in production.
const DECLARATION = /^(?:function\s+([A-Za-z_$][\w$]*)|var\s+([A-Za-z_$][\w$]*)\s*=)/gm;

// Deliberately `new Function` rather than `vm.runInNewContext`. A `vm` context is
// a separate realm, so an object literal created inside it has a different
// `Object.prototype` from the test's — and `assert.deepStrictEqual` then fails on
// two structurally identical results for a reason that has nothing to do with the
// add-on. Evaluating in this realm keeps the intrinsics shared, which is what
// makes the assertions mean what they read as.
function loadAddon(globals = {}) {
  const sources = SOURCE_FILES.map((file) =>
    fs.readFileSync(path.join(SOURCE_DIR, file), 'utf8')
  );
  const combined = sources.join('\n;\n');
  const names = new Set();
  for (const match of combined.matchAll(DECLARATION)) {
    names.add(match[1] || match[2]);
  }
  const injected = Object.keys(globals);
  const preamble = injected.map((name) => `var ${name} = __globals[${JSON.stringify(name)}];`);
  const factory = new Function(
    '__globals',
    `${preamble.join('\n')}\n${combined}\n;return { ${[...names].join(', ')} };`
  );
  return factory(globals);
}

/**
 * A spreadsheet that records what was written, with only the surface the add-on
 * actually uses. Deliberately not a general Sheets emulator: a fake that is
 * bigger than the code it stands in for starts having bugs of its own.
 */
class FakeSheet {
  constructor(values, { name = 'Prospects', sheetId = 12345 } = {}) {
    this.values = values.map((row) => row.slice());
    this.name = name;
    this.sheetId = sheetId;
    this.hiddenColumns = [];
    this.activeRanges = [];
    this.maxColumns = Math.max(...values.map((row) => row.length), 1);
  }

  getName() {
    return this.name;
  }

  getSheetId() {
    return this.sheetId;
  }

  getLastRow() {
    return this.values.length;
  }

  getLastColumn() {
    return Math.max(...this.values.map((row) => row.length), 0);
  }

  getMaxColumns() {
    return this.maxColumns;
  }

  insertColumnsAfter(_after, howMany) {
    this.maxColumns += howMany;
    for (const row of this.values) {
      for (let index = 0; index < howMany; index += 1) {
        row.push('');
      }
    }
  }

  hideColumns(column) {
    this.hiddenColumns.push(column);
  }

  getRange(row, column, numRows = 1, numColumns = 1) {
    const sheet = this;
    return {
      getRow: () => row,
      getNumRows: () => numRows,
      getValues() {
        const out = [];
        for (let r = 0; r < numRows; r += 1) {
          const source = sheet.values[row - 1 + r] || [];
          const line = [];
          for (let c = 0; c < numColumns; c += 1) {
            line.push(source[column - 1 + c] === undefined ? '' : source[column - 1 + c]);
          }
          out.push(line);
        }
        return out;
      },
      setValue(value) {
        while (sheet.values.length < row) {
          sheet.values.push([]);
        }
        const target = sheet.values[row - 1];
        while (target.length < column) {
          target.push('');
        }
        target[column - 1] = value;
        sheet.maxColumns = Math.max(sheet.maxColumns, column);
      },
    };
  }

  setSelection(ranges) {
    this.activeRanges = ranges;
  }

  getActiveRangeList() {
    const ranges = this.activeRanges;
    return {
      getRanges: () =>
        ranges.map(([row, numRows]) => ({
          getRow: () => row,
          getNumRows: () => numRows,
        })),
    };
  }
}

module.exports = { loadAddon, FakeSheet };
