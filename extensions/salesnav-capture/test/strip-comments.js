"use strict";
/**
 * Remove comments from JavaScript source, correctly.
 *
 * Several tests assert things about the *code* — that the capture path contains
 * no pagination, that an injection list names a module — and must not be
 * satisfied (or defeated) by prose. Stripping comments with a naive regex is
 * unsafe: this codebase contains match patterns like
 * `"https://www.linkedin.com/sales/*"`, and the `/*` inside that STRING starts
 * a comment as far as `/\/\*[\s\S]*?\*\//` is concerned. When that happened it
 * silently swallowed ~7kB of real code, including the function under test,
 * making the assertion pass or fail for entirely the wrong reason.
 *
 * So this is a small character scanner instead. It tracks string literals,
 * template literals and regex literals, and only treats `//` or a block comment
 * as a comment when it is genuinely in code position.
 *
 * Comments are replaced by a single space so token boundaries survive; newlines
 * inside block comments are preserved so line numbers do not shift.
 */

/** A `/` here starts a regex literal, not a division. */
function regexCanFollow(prevMeaningful) {
  if (prevMeaningful === "") return true;
  return "(,=:[!&|?{};+-*%~^<>".includes(prevMeaningful);
}

function stripComments(source) {
  let out = "";
  let i = 0;
  let prev = ""; // last meaningful (non-space) character emitted
  const n = source.length;

  while (i < n) {
    const c = source[i];
    const next = source[i + 1];

    // --- comments ---
    if (c === "/" && next === "/") {
      while (i < n && source[i] !== "\n") i += 1;
      out += " ";
      continue;
    }
    if (c === "/" && next === "*") {
      i += 2;
      while (i < n && !(source[i] === "*" && source[i + 1] === "/")) {
        if (source[i] === "\n") out += "\n"; // keep line numbering intact
        i += 1;
      }
      i += 2;
      out += " ";
      continue;
    }

    // --- string and template literals ---
    if (c === '"' || c === "'" || c === "`") {
      const quote = c;
      out += c;
      i += 1;
      while (i < n) {
        if (source[i] === "\\") {
          out += source[i] + (source[i + 1] || "");
          i += 2;
          continue;
        }
        out += source[i];
        if (source[i] === quote) {
          i += 1;
          break;
        }
        i += 1;
      }
      prev = quote;
      continue;
    }

    // --- regex literals ---
    if (c === "/" && regexCanFollow(prev)) {
      let j = i + 1;
      let inClass = false;
      let closed = false;
      while (j < n) {
        const ch = source[j];
        if (ch === "\\") {
          j += 2;
          continue;
        }
        if (ch === "\n") break; // unterminated: not a regex after all
        if (ch === "[") inClass = true;
        else if (ch === "]") inClass = false;
        else if (ch === "/" && !inClass) {
          closed = true;
          break;
        }
        j += 1;
      }
      if (closed) {
        out += source.slice(i, j + 1);
        prev = "/";
        i = j + 1;
        continue;
      }
    }

    out += c;
    if (c.trim()) prev = c;
    i += 1;
  }

  return out;
}

module.exports = { stripComments };
