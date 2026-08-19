/**
 * The thin layer between `SpreadsheetApp` and the pure functions in Contract.js.
 *
 * Everything here is I/O: read the used range into an array, hand it to a pure
 * function, write the answer back. No decision is made in this file, which is
 * why it is short and why the tests live next door rather than here.
 *
 * Reads and writes are batched deliberately. A per-cell `setValue` in a loop is
 * the classic Apps Script performance trap, and on a sheet of a few hundred rows
 * it is the difference between a second and a timeout.
 */

/** The whole used grid of a sheet as a plain array, padded to a fixed width. */
function readGrid(sheet, extraColumns) {
  var lastRow = sheet.getLastRow();
  var lastColumn = sheet.getLastColumn();
  if (lastRow < 1 || lastColumn < 1) {
    return [];
  }
  var width = lastColumn + (extraColumns || 0);
  var values = sheet.getRange(1, 1, lastRow, lastColumn).getValues();
  for (var index = 0; index < values.length; index += 1) {
    while (values[index].length < width) {
      values[index].push('');
    }
  }
  return values;
}

/**
 * Create the VMR columns a plan asks for, and hide the row-key column.
 *
 * The key column is hidden rather than protected. Protecting it would make the
 * sheet feel owned by the add-on; hiding it keeps it out of the way while
 * leaving the operator entirely free to delete it, which costs them nothing
 * worse than a fresh set of keys next time.
 *
 * The label goes in the detected header row, which is not always row 1. Writing
 * it into row 1 as well — as this did until the header row index was passed in —
 * left a second copy of every VMR heading above a sheet whose real header sits
 * under a title line, in cells the operator may have been using for that title.
 */
function createOutputColumns(sheet, plan, headerRowIndex) {
  if (!plan.create.length) {
    return;
  }
  var needed = plan.totalColumns - sheet.getMaxColumns();
  if (needed > 0) {
    sheet.insertColumnsAfter(sheet.getMaxColumns(), needed);
  }
  var headerRow = (headerRowIndex || 0) + 1;
  for (var index = 0; index < plan.create.length; index += 1) {
    var column = plan.create[index];
    sheet.getRange(headerRow, column.column + 1).setValue(column.name);
  }
  var keyColumn = plan.columns[ROW_KEY_COLUMN];
  if (keyColumn !== undefined) {
    sheet.hideColumns(keyColumn + 1);
  }
}

/** Write the newly minted row keys, one batched write per contiguous run. */
function writeRowKeys(sheet, keyColumn, assignments) {
  for (var index = 0; index < assignments.length; index += 1) {
    var assignment = assignments[index];
    sheet.getRange(assignment.rowIndex + 1, keyColumn + 1).setValue(assignment.key);
  }
}

/**
 * Apply result edits, one batched write per row.
 *
 * Per row rather than per cell, and never one write across the whole range: the
 * VMR columns are not necessarily contiguous (an operator may have moved one),
 * and a single range write would clobber whatever sits between them — which
 * would be the operator's own data.
 */
function writeResultEdits(sheet, edits) {
  for (var index = 0; index < edits.length; index += 1) {
    var edit = edits[index];
    for (var column in edit.cells) {
      if (Object.prototype.hasOwnProperty.call(edit.cells, column)) {
        sheet.getRange(edit.rowIndex + 1, Number(column) + 1).setValue(edit.cells[column]);
      }
    }
  }
}

/** The zero-based row indexes the operator currently has selected. */
function selectedRowIndexes(sheet, firstDataRow) {
  var ranges = sheet.getActiveRangeList
    ? sheet.getActiveRangeList().getRanges()
    : [sheet.getActiveRange()];
  var seen = {};
  var rows = [];
  for (var index = 0; index < ranges.length; index += 1) {
    var range = ranges[index];
    if (!range) {
      continue;
    }
    var start = range.getRow() - 1;
    for (var offset = 0; offset < range.getNumRows(); offset += 1) {
      var rowIndex = start + offset;
      if (rowIndex < firstDataRow || seen[rowIndex]) {
        continue;
      }
      seen[rowIndex] = true;
      rows.push(rowIndex);
    }
  }
  rows.sort(function (left, right) {
    return left - right;
  });
  return rows;
}

/** The submission identifiers already recorded in the sheet, for a refresh. */
function knownSubmissions(values, columns, firstDataRow) {
  var idColumn = columns['VMR Campaign Contact ID'];
  var keyColumn = columns[ROW_KEY_COLUMN];
  var statusColumn = columns['VMR Status'];
  var pairs = [];
  for (var rowIndex = firstDataRow; rowIndex < values.length; rowIndex += 1) {
    var row = values[rowIndex] || [];
    var submissionId = String(row[idColumn] || '').trim();
    var key = String(row[keyColumn] || '').trim();
    if (!submissionId || !key) {
      continue;
    }
    // A finished row is not asked about again. Refreshing a sheet whose rows are
    // mostly Ready should cost one small request, not a full re-read.
    if (String(row[statusColumn] || '').trim() === STATUS_LABELS.ready) {
      continue;
    }
    pairs.push({ submissionId: submissionId, clientRowId: key });
  }
  return pairs;
}
