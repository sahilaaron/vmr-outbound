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
  };

  const CHUNK_STATUS = {
    PENDING: "pending",
    ACCEPTED: "accepted",
    FAILED: "failed",
  };

  /** Where one chunk's captures live while they wait to be sent. */
  function chunkKey(jobId, index) {
    return `${PUSH_STORAGE.CHUNK_PREFIX}${jobId}:${index}`;
  }

  /** Every chunk key a job could still own, so a finished job leaves nothing. */
  function allChunkKeys(job) {
    if (!job || !Array.isArray(job.chunks)) return [];
    return job.chunks.map((c) => chunkKey(job.jobId, c.index));
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
    const chunks = plan.chunks.map((c) => ({
      index: c.index,
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
    }));
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
      totalContacts: plan.totalContacts,
      plannedContacts: plan.plannedContacts != null ? plan.plannedContacts : 0,
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
      (job.status === STATUS.COMPLETED || job.status === STATUS.COMPLETED_WITH_FAILURES)
    );
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
    return {
      jobId: job.jobId,
      logicalSubmissionId: job.logicalSubmissionId,
      status: job.status,
      createdAt: job.createdAt,
      updatedAt: job.updatedAt,
      totalContacts: job.totalContacts,
      contactsAccepted: accepted,
      contactsFailed: failedContacts,
      contactsPending: Math.max(0, job.plannedContacts - accepted - failedContacts),
      totalChunks: job.totalChunks,
      completedChunks: job.chunks.length - pendingChunks.length - failedChunks.length,
      pendingChunks: pendingChunks.length,
      failedChunks: failedChunks.length,
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

  return {
    JOB_VERSION,
    STATUS,
    CHUNK_STATUS,
    chunkKey,
    allChunkKeys,
    createJob,
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
