/**
 * Byte-aware chunk planning for one logical contact-capture push.
 *
 * ONE OPERATOR SAVE IS NOT ONE HTTP REQUEST. The operator reviews up to
 * `LIMITS.MAX_RECORDS_PER_BATCH` people and presses Save once; this module
 * decides how that reviewed set is divided into bounded requests, each of which
 * satisfies the committed contract on its own.
 *
 * Chunking is a TRANSPORT concern and nothing else. It never rewrites, trims,
 * reorders or drops a field of a capture: a chunk contains whole captures,
 * verbatim, in the order they were reviewed. What the backend receives across
 * the chunks of one push is exactly what a single oversized request would have
 * carried, which is what makes the division invisible in the data.
 *
 * TWO CEILINGS, BOTH ENFORCED
 * ---------------------------
 * A record count alone is not a size. A Sales Navigator row serializes to about
 * 2.9 KB while a person-profile capture carries an 8 KB `about_text` plus its
 * raw snapshot, so "100 records" is a wildly different number of bytes in the
 * two workflows. Every chunk therefore closes when EITHER
 *
 *   contacts  would exceed `maxContacts`, or
 *   bytes     would exceed `maxBytes` (envelope included)
 *
 * whichever happens first.
 *
 * THE PATHOLOGICAL RECORD
 * -----------------------
 * A single capture can be larger than a whole chunk's byte budget. It is NOT
 * silently dropped and NOT allowed to become a chunk that fails and retries for
 * ever: it travels alone, in a chunk of its own, up to `recordMaxBytes` — the
 * ceiling one request can carry at all. A record above even that is refused
 * here, deterministically, at plan time, and reported as an oversized record so
 * the operator is told which one and why. Planning refuses; it never loops.
 *
 * Pure and dependency-free: the caller supplies the measuring function, so this
 * is unit-testable without a browser and cannot drift from the serializer that
 * actually builds the request.
 *
 * UMD module -> Node CommonJS + self.SNCapture.chunking
 */
(function (root, factory) {
  const g = typeof self !== "undefined" ? self : root;
  const isNode = typeof module !== "undefined" && module.exports;
  const mod = factory(isNode ? require("./constants.js") : g.SNCapture.constants);
  if (isNode) module.exports = mod;
  g.SNCapture = Object.assign(g.SNCapture || {}, { chunking: mod });
})(typeof globalThis !== "undefined" ? globalThis : this, function (constants) {
  "use strict";

  const { PUSH, LIMITS } = constants;

  /** Why a record could not be placed in any chunk. Stable, PII-free codes. */
  const OVERSIZED_RECORD = "record_too_large";

  /**
   * Plan the chunks for one reviewed set.
   *
   * @param {object[]} contacts captures in reviewed order, already built
   * @param {object} options
   *   measure:        (capture) => serialized bytes of that capture, comma included
   *   envelopeBytes:  bytes the submission costs with an empty `contacts` array
   *   maxContacts:    records per chunk ceiling
   *   maxBytes:       serialized bytes per chunk ceiling (envelope included)
   *   recordMaxBytes: largest a single record may be and still be sendable
   * @returns {{chunks: Array<{index:number, indexes:number[], contactCount:number, bytes:number, solo:boolean}>,
   *            oversized: Array<{position:number, bytes:number, code:string}>,
   *            totalContacts:number, plannedContacts:number}}
   *
   * Chunks carry the reviewed-set POSITIONS they contain rather than a range.
   * A refused record leaves a hole in the sequence, and a range across a hole
   * would silently claim a record the chunk does not have.
   */
  function planChunks(contacts, options) {
    const list = Array.isArray(contacts) ? contacts : [];
    const opts = options || {};
    const measure = typeof opts.measure === "function" ? opts.measure : defaultMeasure;
    const envelope = Number.isFinite(opts.envelopeBytes) ? opts.envelopeBytes : 0;
    const maxContacts = positive(opts.maxContacts, PUSH.CHUNK_MAX_CONTACTS);
    const maxBytes = positive(opts.maxBytes, PUSH.CHUNK_MAX_BYTES);
    const recordMaxBytes = positive(opts.recordMaxBytes, PUSH.RECORD_MAX_BYTES);

    const chunks = [];
    const oversized = [];
    let current = null;

    const close = () => {
      if (current && current.contactCount > 0) chunks.push(current);
      current = null;
    };

    for (let i = 0; i < list.length; i += 1) {
      const bytes = measure(list[i]);

      // Refused before it can become a chunk. `envelope + bytes` is what the
      // request would actually weigh, so the refusal is about the request the
      // extension would have to send, not about the record in isolation.
      if (envelope + bytes > recordMaxBytes) {
        oversized.push({ position: i, bytes, code: OVERSIZED_RECORD });
        continue;
      }

      // Larger than a shared chunk's budget but sendable alone: give it one.
      if (envelope + bytes > maxBytes) {
        close();
        chunks.push({
          index: chunks.length,
          indexes: [i],
          contactCount: 1,
          bytes: envelope + bytes,
          solo: true,
        });
        continue;
      }

      if (
        current &&
        (current.contactCount >= maxContacts || current.bytes + bytes > maxBytes)
      ) {
        close();
      }
      if (!current) {
        current = {
          index: chunks.length,
          indexes: [],
          contactCount: 0,
          bytes: envelope,
          solo: false,
        };
      }
      current.indexes.push(i);
      current.contactCount += 1;
      current.bytes += bytes;
    }
    close();

    // A solo chunk is appended mid-stream, so indexes are renumbered once at the
    // end rather than trusted from the order they were created in.
    chunks.forEach((chunk, index) => {
      chunk.index = index;
    });

    const plannedContacts = chunks.reduce((sum, c) => sum + c.contactCount, 0);
    return {
      chunks,
      oversized,
      totalContacts: list.length,
      plannedContacts,
    };
  }

  /**
   * Whether a reviewed set may be pushed at all.
   *
   * The refusal names the real limit. "Payload too large" was the wrong sentence
   * for a count problem and the wrong sentence for a byte problem too: it told
   * the operator to capture fewer records without saying how many fewer.
   */
  function checkPushSize(count, max) {
    const ceiling = positive(max, LIMITS.MAX_RECORDS_PER_BATCH);
    if (!Number.isFinite(count) || count <= 0) {
      return { ok: false, code: "empty_batch", limit: ceiling, count: 0 };
    }
    if (count > ceiling) {
      return { ok: false, code: "push_limit_exceeded", limit: ceiling, count };
    }
    return { ok: true, code: null, limit: ceiling, count };
  }

  function positive(value, fallback) {
    return Number.isFinite(value) && value > 0 ? value : fallback;
  }

  function defaultMeasure(capture) {
    const json = JSON.stringify(capture);
    return typeof TextEncoder !== "undefined"
      ? new TextEncoder().encode(json).length + 1
      : Buffer.byteLength(json, "utf8") + 1;
  }

  return { planChunks, checkPushSize, OVERSIZED_RECORD };
});
