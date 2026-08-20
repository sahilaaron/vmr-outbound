/**
 * The durable push job: what one operator Save becomes once it leaves the panel.
 *
 * WHY THIS EXISTS
 * ---------------
 * A save used to be a promise. The side panel called the worker, awaited one
 * `fetch`, and painted whatever came back — so the operation's lifetime was the
 * panel's lifetime, the Sales Navigator tab's lifetime, and the service worker's
 * uninterrupted attention, all at once. At ten contacts nobody notices. At
 * several thousand, every one of those is a way to lose the work.
 *
 * A job is the same operation written down instead of held in memory. It records
 * what was reviewed, how it was divided, which pieces the backend has already
 * accepted, and what to do next — and it is rewritten to `chrome.storage.local`
 * after every state transition. Manifest V3 may suspend the service worker at
 * any moment; when it comes back, the job is the whole of what it needs to
 * continue, and it continues from the first unfinished chunk rather than from
 * the beginning.
 *
 * WHAT MAY BE PLANNED, AND WHAT MAY NEVER BE PLANNED AGAIN
 * --------------------------------------------------------
 * A `client_capture_id` is unique across the backend's whole capture table, not
 * per submission. Once the backend has committed one it belongs to that
 * submission for ever, and any LATER submission carrying it is refused with
 * `client_capture_id_conflict` — a 409 no retry can clear.
 *
 * The first version of this module planned every Save from the whole reviewed
 * set, so excluding a row after a successful push, or capturing a hundred more
 * people, re-planned people the backend already held into new chunks under new
 * submission ids. That wedged the operation permanently.
 *
 * The DELIVERY LEDGER is the fix. It records, per `client_capture_id`, whether
 * that person has ever left the browser:
 *
 *   absent    never transmitted -> the ONLY kind a new chunk may contain
 *   accepted  the backend confirmed it -> saved, never offered again
 *   in doubt  transmitted, no success seen -> only its own frozen chunk, under
 *             its own original submission id, may ever carry it again
 *   terminal  transmitted and given up on -> reported, never re-planned
 *
 * "In doubt" is not pessimism. A request that times out is indistinguishable
 * from a request that committed and lost its response, so anything that reached
 * the network is treated as possibly-owned by the backend. That is what makes
 * re-planning safe to forbid and replay safe to allow.
 *
 * IDENTIFIERS, AND WHY THEY ARE MINTED ONCE
 * -----------------------------------------
 * Every chunk carries its own `clientSubmissionId`, minted when the job is
 * planned and never re-minted. That is the whole idempotency story: a retry
 * after a timeout re-sends the SAME id, and the backend either commits it once
 * or replays the response it already produced. Minting a fresh id for a retry —
 * the obvious-looking thing to do when a request "failed" — is exactly how a
 * commit whose response was lost becomes a duplicate person.
 *
 * The per-contact `client_capture_id` values were minted at capture time and are
 * untouched here, and chunk boundaries are fixed at plan time, so a given person
 * always travels in the same chunk under the same two ids no matter how often
 * the push is resumed.
 *
 * WHAT IS NOT IN A JOB
 * --------------------
 * No access token, no refresh token, no credential of any kind. Authorization is
 * resolved per request from the account link, exactly as a one-shot save always
 * did; a job that outlives a token must get a new token, not carry an old one.
 *
 * Pure over plain objects — no storage, no clock, no `chrome` — so the whole
 * lifecycle is unit-testable, and so the caller cannot accidentally make a
 * transition that is not written down.
 *
 * UMD module -> Node CommonJS + self.SNCapture.pushJob
 */
(function (root, factory) {
  const g = typeof self !== "undefined" ? self : root;
  const isNode = typeof module !== "undefined" && module.exports;
  const mod = factory(isNode ? require("./constants.js") : g.SNCapture.constants);
  if (isNode) module.exports = mod;
  g.SNCapture = Object.assign(g.SNCapture || {}, { pushJob: mod });
})(typeof globalThis !== "undefined" ? globalThis : this, function (constants) {
  "use strict";

  const { PUSH, PUSH_STORAGE } = constants;

  const JOB_VERSION = 1;

  /** Job-level status. Each one is a truthful statement about right now. */
  const STATUS = {
    /** Chunks are planned and stored; nothing has been sent yet. */
    PREPARING: "preparing",
    /** At least one chunk is being delivered. */
    RUNNING: "running",
    /** A chunk failed recoverably and is waiting out its backoff. */
    RETRYING: "retrying",
    /** Every chunk was accepted. */
    COMPLETED: "completed",
    /** Every chunk was attempted; some are permanently or repeatedly failed. */
    COMPLETED_WITH_FAILURES: "completed_with_failures",
    /**
     * The operator stopped it.
     *
     * Absorbing: nothing moves a cancelled job back to running, including a
     * response that arrives from a request already in flight when Cancel was
     * pressed. That response is still recorded truthfully — the contacts in it
     * really were saved — but the job stays cancelled.
     */
    CANCELLED: "cancelled",
  };

  const CHUNK_STATUS = {
    PENDING: "pending",
    ACCEPTED: "accepted",
    FAILED: "failed",
    /** Never attempted again because the operator stopped the push. */
    CANCELLED: "cancelled",
  };

  /**
   * Where one chunk's captures live while they wait to be sent.
   *
   * Keyed by the chunk's own idempotency key, not by its job. A chunk that a
   * later Save carries forward keeps its submission id — that IS its identity to
   * the backend — so it keeps its storage key and nothing has to be copied.
   */
  function chunkKey(clientSubmissionId) {
    return `${PUSH_STORAGE.CHUNK_PREFIX}${clientSubmissionId}`;
  }

  /** Every chunk key a job could still own, so a finished job leaves nothing. */
  function allChunkKeys(job) {
    if (!job || !Array.isArray(job.chunks)) return [];
    return job.chunks.map((c) => chunkKey(c.clientSubmissionId));
  }

  /**
   * Chunk keys that must SURVIVE, because the job may still need to send them.
   *
   * Everything else under the chunk prefix is reclaimable: an accepted chunk has
   * done its job, and a chunk belonging to no current job can never be sent by
   * anything. This is the whole ownership rule the storage sweep applies.
   */
  function liveChunkKeys(job) {
    if (!job || !Array.isArray(job.chunks)) return [];
    return job.chunks
      .filter((c) => c.status === CHUNK_STATUS.PENDING || c.status === CHUNK_STATUS.FAILED)
      .map((c) => chunkKey(c.clientSubmissionId));
  }

  /**
   * Build the durable job record.
   *
   * @param {object} args
   *   jobId, logicalSubmissionId, createdAt: caller-supplied (no clock here)
   *   plan: the `planChunks` result
   *   mintId: () => string, called once per chunk
   *   campaignId, captureMode, submittedAt, metadata, extensionVersion
   */
  function createJob(args) {
    const plan = args.plan || { chunks: [], oversized: [], totalContacts: 0 };
    // Undelivered chunks inherited from the previous job come FIRST: they are
    // the older work, they already have their submission ids and their payloads
    // are already on disk, and re-minting either would be the LP-001 bug in a
    // new costume. Attempt counters are reset — pressing Save is the operator
    // asking for another go — but nothing else about them changes.
    const carried = (args.carried || []).map((c) =>
      Object.assign({}, c, {
        status: CHUNK_STATUS.PENDING,
        attempts: 0,
        nextAttemptAt: null,
        carried: true,
      })
    );
    const planned = plan.chunks.map((c) => ({
      index: 0,
      // Minted ONCE. See the module docstring: re-minting on retry is how a lost
      // response turns into a second copy of a person.
      clientSubmissionId: args.mintId(),
      contactCount: c.contactCount,
      bytes: c.bytes,
      solo: c.solo === true,
      status: CHUNK_STATUS.PENDING,
      attempts: 0,
      lastErrorCode: null,
      nextAttemptAt: null,
      retryable: true,
      carried: false,
    }));
    const chunks = carried.concat(planned);
    chunks.forEach((c, i) => {
      c.index = i;
    });
    return {
      v: JOB_VERSION,
      jobId: args.jobId,
      // Operator-facing identity for the whole Save. Deliberately local: the
      // contract's envelope is `additionalProperties: false`, and a logical id
      // is not something the backend has any use for.
      logicalSubmissionId: args.logicalSubmissionId,
      createdAt: args.createdAt,
      updatedAt: args.createdAt,
      status: chunks.length ? STATUS.PREPARING : STATUS.COMPLETED_WITH_FAILURES,
      totalContacts:
        plan.totalContacts + carried.reduce((sum, c) => sum + c.contactCount, 0),
      plannedContacts:
        (plan.plannedContacts != null ? plan.plannedContacts : 0) +
        carried.reduce((sum, c) => sum + c.contactCount, 0),
      totalChunks: chunks.length,
      chunks,
      // Frozen at push time. A campaign chosen after the push started belongs to
      // the next push, not to this one — a chunk that filed into a different
      // campaign from its siblings would be one Save with two meanings.
      campaignId: args.campaignId || null,
      captureMode: args.captureMode,
      submittedAt: args.submittedAt,
      metadata: args.metadata || { labels: [], note: null },
      extensionVersion: args.extensionVersion || null,
      counts: {},
      results: [],
      resultsSeen: 0,
      failures: [],
      // Records refused at plan time for being unsendable on their own. They are
      // named so the operator learns which of their captures did not travel,
      // rather than discovering a silent shortfall in the totals.
      oversized: (plan.oversized || []).map((o) => ({
        position: o.position,
        bytes: o.bytes,
        code: o.code,
      })),
      workbenchUrl: null,
    };
  }

  /**
   * The chunks a NEW job should inherit: everything still owed to the backend.
   *
   * A chunk parked as unretryable is deliberately NOT carried — retrying an
   * identical body against a contract that refused it would fail identically —
   * but its contacts stay in the ledger, so they are reported rather than
   * quietly re-planned under a new id.
   */
  function carryableChunks(job) {
    if (!job || !Array.isArray(job.chunks)) return [];
    return job.chunks.filter(
      (c) =>
        (c.status === CHUNK_STATUS.PENDING || c.status === CHUNK_STATUS.FAILED) &&
        c.retryable !== false
    );
  }

  function acceptedChunks(job) {
    return job.chunks.filter((c) => c.status === CHUNK_STATUS.ACCEPTED);
  }

  function contactsAccepted(job) {
    return acceptedChunks(job).reduce((sum, c) => sum + c.contactCount, 0);
  }

  function isUnfinished(job) {
    return (
      !!job &&
      Array.isArray(job.chunks) &&
      job.chunks.some((c) => c.status === CHUNK_STATUS.PENDING)
    );
  }

  /**
   * The next chunk to attempt, or why there is nothing to do.
   *
   * Chunks are attempted in order, but a chunk waiting out its backoff does not
   * hold up the ones behind it: partial failure must not become total stall.
   */
  function nextChunk(job, now) {
    if (!job || !Array.isArray(job.chunks)) return { chunk: null, reason: "no_job" };
    let waiting = null;
    for (const chunk of job.chunks) {
      if (chunk.status !== CHUNK_STATUS.PENDING) continue;
      if (chunk.nextAttemptAt && chunk.nextAttemptAt > now) {
        if (waiting === null || chunk.nextAttemptAt < waiting) waiting = chunk.nextAttemptAt;
        continue;
      }
      return { chunk, reason: null };
    }
    if (waiting !== null) return { chunk: null, reason: "waiting", waitUntil: waiting };
    return { chunk: null, reason: "done" };
  }

  /**
   * Record that a chunk is about to be attempted.
   *
   * The attempt is counted and persisted BEFORE the request goes out, on
   * purpose: a worker that dies mid-request must come back having spent an
   * attempt, or a crash inside the request becomes an unbounded loop that never
   * records anything.
   */
  function markAttempt(job, index, now) {
    const chunk = job.chunks[index];
    if (!chunk) return job;
    chunk.attempts += 1;
    chunk.nextAttemptAt = null;
    job.status = STATUS.RUNNING;
    job.updatedAt = now;
    return job;
  }

  /**
   * Fold one accepted chunk's response into the job.
   *
   * Counts are summed across chunks so the totals describe the whole logical
   * push. Detail entries are bounded — see `PUSH.MAX_RETAINED_RESULTS` — and the
   * number the backend actually returned is kept separately, because "500
   * detailed results retained" and "500 contacts processed" are not the same
   * sentence and the panel must never print the second when it means the first.
   */
  function markAccepted(job, index, result, now) {
    const chunk = job.chunks[index];
    if (!chunk) return job;
    chunk.status = CHUNK_STATUS.ACCEPTED;
    chunk.lastErrorCode = null;
    chunk.nextAttemptAt = null;

    const counts = (result && result.counts) || {};
    for (const [key, value] of Object.entries(counts)) {
      if (!Number.isFinite(value)) continue;
      job.counts[key] = (job.counts[key] || 0) + value;
    }
    const details = (result && result.results) || [];
    job.resultsSeen += details.length;
    for (const detail of details) {
      if (job.results.length >= PUSH.MAX_RETAINED_RESULTS) break;
      job.results.push(detail);
    }
    if (!job.workbenchUrl && result && result.workbenchUrl) {
      job.workbenchUrl = result.workbenchUrl;
    }
    job.updatedAt = now;
    return settle(job, now);
  }

  /**
   * Record a failed attempt.
   *
   * A recoverable failure keeps the chunk PENDING with a backoff, so it is
   * retried with the same idempotency key. An unrecoverable one — or a
   * recoverable one that has used its attempts — parks the chunk as FAILED. In
   * neither case does anything already accepted change: chunk 9 failing must
   * leave chunks 1-8 saved and chunks 10+ still deliverable.
   */
  function markFailed(job, index, failure, now) {
    const chunk = job.chunks[index];
    if (!chunk) return job;
    const f = failure || {};
    chunk.lastErrorCode = f.code || "unknown";
    const retryable = f.retryable !== false;
    const exhausted = chunk.attempts >= PUSH.MAX_ATTEMPTS;
    if (retryable && !exhausted) {
      chunk.status = CHUNK_STATUS.PENDING;
      chunk.retryable = true;
      const step = Math.min(chunk.attempts - 1, PUSH.RETRY_BACKOFF_MS.length - 1);
      chunk.nextAttemptAt = now + PUSH.RETRY_BACKOFF_MS[Math.max(0, step)];
    } else {
      chunk.status = CHUNK_STATUS.FAILED;
      chunk.retryable = retryable;
      chunk.nextAttemptAt = null;
    }
    recordFailure(job, {
      chunk: chunk.index,
      contactCount: chunk.contactCount,
      code: chunk.lastErrorCode,
      attempts: chunk.attempts,
      status: chunk.status,
    });
    job.updatedAt = now;
    return settle(job, now);
  }

  /**
   * Re-arm every failed chunk for another pass.
   *
   * The operator's "Retry" is not a new push: the same chunks keep the same
   * submission ids, so retrying after a partial failure can only ever fill the
   * gaps — it cannot re-save anybody who is already saved.
   */
  function retryFailed(job, now) {
    let armed = 0;
    for (const chunk of job.chunks) {
      if (chunk.status !== CHUNK_STATUS.FAILED) continue;
      if (chunk.retryable === false) continue;
      chunk.status = CHUNK_STATUS.PENDING;
      chunk.attempts = 0;
      chunk.nextAttemptAt = null;
      armed += 1;
    }
    if (armed) {
      job.status = STATUS.RUNNING;
      job.updatedAt = now;
    }
    return { job, armed };
  }

  function recordFailure(job, entry) {
    const existing = job.failures.findIndex((f) => f.chunk === entry.chunk);
    if (existing >= 0) {
      job.failures[existing] = entry;
      return;
    }
    if (job.failures.length >= PUSH.MAX_RETAINED_FAILURES) return;
    job.failures.push(entry);
  }

  /** Move the job to its correct status given the state of its chunks. */
  function settle(job, now) {
    // Cancellation is the operator's decision and outranks chunk state. A
    // response arriving from a request that was already in flight is still
    // recorded — those contacts really were saved — but it cannot un-cancel.
    if (job.status === STATUS.CANCELLED) return job;
    const pending = job.chunks.filter((c) => c.status === CHUNK_STATUS.PENDING);
    const failed = job.chunks.filter((c) => c.status === CHUNK_STATUS.FAILED);
    if (pending.length) {
      const waiting = pending.every((c) => c.nextAttemptAt && c.nextAttemptAt > now);
      job.status = waiting ? STATUS.RETRYING : STATUS.RUNNING;
      return job;
    }
    job.status =
      failed.length || job.oversized.length
        ? STATUS.COMPLETED_WITH_FAILURES
        : STATUS.COMPLETED;
    return job;
  }

  function isTerminal(job) {
    return (
      !!job &&
      (job.status === STATUS.COMPLETED ||
        job.status === STATUS.COMPLETED_WITH_FAILURES ||
        job.status === STATUS.CANCELLED)
    );
  }

  /**
   * Stop the push.
   *
   * NOT a rollback. Contacts the backend already accepted stay accepted — this
   * cannot and must not reach across and undo a server-side commit. What it does
   * is stop offering the rest: every chunk still owed becomes CANCELLED, which
   * is terminal and is never retried by an alarm, a resume, or a restart.
   *
   * Returns the counts the operator has to be told, because "cancelled" without
   * "and here is what did and did not happen" is not an answer.
   */
  function cancel(job, now) {
    if (!job) return { job, accepted: 0, notSent: 0, transmitted: 0 };
    let notSent = 0;
    let transmitted = 0;
    for (const chunk of job.chunks) {
      if (chunk.status === CHUNK_STATUS.ACCEPTED) continue;
      if (chunk.attempts > 0) transmitted += chunk.contactCount;
      notSent += chunk.contactCount;
      chunk.status = CHUNK_STATUS.CANCELLED;
      chunk.nextAttemptAt = null;
    }
    job.status = STATUS.CANCELLED;
    job.cancelledAt = now;
    job.updatedAt = now;
    return { job, accepted: contactsAccepted(job), notSent, transmitted };
  }

  /**
   * What the panel is told. Truthful by construction: every number is derived
   * from chunk state rather than assumed from "the push started".
   */
  function jobView(job) {
    if (!job) return null;
    const accepted = contactsAccepted(job);
    const failedChunks = job.chunks.filter((c) => c.status === CHUNK_STATUS.FAILED);
    const failedContacts = failedChunks.reduce((sum, c) => sum + c.contactCount, 0);
    const pendingChunks = job.chunks.filter((c) => c.status === CHUNK_STATUS.PENDING);
    const cancelledChunks = job.chunks.filter((c) => c.status === CHUNK_STATUS.CANCELLED);
    const cancelledContacts = cancelledChunks.reduce((sum, c) => sum + c.contactCount, 0);
    return {
      jobId: job.jobId,
      logicalSubmissionId: job.logicalSubmissionId,
      status: job.status,
      createdAt: job.createdAt,
      updatedAt: job.updatedAt,
      totalContacts: job.totalContacts,
      contactsAccepted: accepted,
      contactsFailed: failedContacts,
      contactsPending: Math.max(
        0,
        job.plannedContacts - accepted - failedContacts - cancelledContacts
      ),
      contactsCancelled: cancelledContacts,
      totalChunks: job.totalChunks,
      completedChunks:
        job.chunks.length - pendingChunks.length - failedChunks.length - cancelledChunks.length,
      pendingChunks: pendingChunks.length,
      failedChunks: failedChunks.length,
      cancelledChunks: cancelledChunks.length,
      retryableChunks: failedChunks.filter((c) => c.retryable !== false).length,
      campaignId: job.campaignId,
      counts: Object.assign({}, job.counts),
      results: job.results.slice(),
      // The two numbers the outcome card must never conflate.
      resultsSeen: job.resultsSeen,
      resultsRetained: job.results.length,
      resultsTruncated: job.resultsSeen > job.results.length,
      failures: job.failures.slice(),
      oversized: job.oversized.slice(),
      workbenchUrl: job.workbenchUrl,
      nextAttemptAt: pendingChunks.reduce(
        (soonest, c) =>
          c.nextAttemptAt && (soonest === null || c.nextAttemptAt < soonest)
            ? c.nextAttemptAt
            : soonest,
        null
      ),
    };
  }


  // ---- the delivery ledger ----------------------------------------------------
  //
  // One record per captured person, keyed by `client_capture_id`, answering the
  // only question that matters when a Save is planned: has this person ever left
  // the browser? See the module docstring for why "ever left" and not "was
  // definitely saved" is the right question.
  //
  // Pure over a plain object, like everything else here, and small enough to
  // rewrite after every state transition: 5,000 entries is a few hundred KB, and
  // it is cleared with the reviewed set it describes.

  const LEDGER_VERSION = 1;

  function emptyLedger() {
    return { v: LEDGER_VERSION, entries: {} };
  }

  /** Tolerate a ledger written by an older version by starting clean. */
  function readLedger(raw) {
    if (!raw || raw.v !== LEDGER_VERSION || !raw.entries) return emptyLedger();
    return { v: LEDGER_VERSION, entries: Object.assign({}, raw.entries) };
  }

  function deliveryState(ledger, captureId) {
    return (ledger && ledger.entries && ledger.entries[captureId]) || null;
  }

  /** True when this person has never been transmitted. */
  function isPlannable(ledger, captureId) {
    return deliveryState(ledger, captureId) === null;
  }

  function markDelivery(ledger, captureIds, state) {
    const next = readLedger(ledger);
    for (const id of captureIds || []) {
      if (typeof id !== "string" || !id) continue;
      // ACCEPTED is final: a later in-doubt marking must never overwrite proof.
      if (next.entries[id] === PUSH.DELIVERY.ACCEPTED) continue;
      next.entries[id] = state;
    }
    return next;
  }

  /**
   * Close the book on everything that was transmitted and never confirmed.
   *
   * Used when a job is cancelled or dismissed: those people are no longer going
   * to be retried by anything, so they stop being "in doubt, pending a retry"
   * and become "transmitted, unconfirmed, not retried" — which is what the
   * operator is told. They are still never re-planned: the backend may hold
   * them, and only their own frozen chunk could ever have proved it.
   */
  function finaliseInDoubt(ledger) {
    const next = readLedger(ledger);
    for (const [id, state] of Object.entries(next.entries)) {
      if (state === PUSH.DELIVERY.IN_DOUBT) next.entries[id] = PUSH.DELIVERY.TERMINAL;
    }
    return next;
  }

  /** How many people are in each delivery state, for truthful reporting. */
  function ledgerCounts(ledger, captureIds) {
    const counts = { accepted: 0, inDoubt: 0, terminal: 0, unsent: 0 };
    for (const id of captureIds || []) {
      switch (deliveryState(ledger, id)) {
        case PUSH.DELIVERY.ACCEPTED:
          counts.accepted += 1;
          break;
        case PUSH.DELIVERY.IN_DOUBT:
          counts.inDoubt += 1;
          break;
        case PUSH.DELIVERY.TERMINAL:
          counts.terminal += 1;
          break;
        default:
          counts.unsent += 1;
      }
    }
    return counts;
  }

  return {
    JOB_VERSION,
    STATUS,
    CHUNK_STATUS,
    LEDGER_VERSION,
    chunkKey,
    allChunkKeys,
    liveChunkKeys,
    carryableChunks,
    createJob,
    cancel,
    emptyLedger,
    readLedger,
    deliveryState,
    isPlannable,
    markDelivery,
    finaliseInDoubt,
    ledgerCounts,
    nextChunk,
    markAttempt,
    markAccepted,
    markFailed,
    retryFailed,
    settle,
    isTerminal,
    isUnfinished,
    contactsAccepted,
    jobView,
  };
});
