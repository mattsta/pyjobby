"""DXE — the Durable Execution Engine's shared vocabulary.

Jobs gain durable primitives on the Job base class (``await self.step``,
``await self.sleep``, ``await self.set_event``, ``await self.send`` /
``recv``, ``await self.stream_write`` / ``stream_close``); this module holds
the exceptions and SQL those primitives share.

Execution model:

* Each durable operation consumes the next **step sequence number**. On a
  retry/resume, completed steps **fast-forward** (their recorded output is
  returned without re-executing) while failed steps **re-execute** — so a
  job's retry budget applies to the work that actually failed, never to
  work that already succeeded. (This deliberately differs from re-raising
  recorded step errors: a recorded error is observability, not destiny.)
* Checkpoint writes are fenced on ``jorb.run_epoch``: a stale execution
  (superseded by the monitor or an operator requeue) cannot record steps.
* ``await self.transaction(...)`` runs its function on the worker's own
  connection inside one transaction and records the checkpoint there too,
  so the application write and the checkpoint commit or roll back together
  (**exactly-once** for work on that connection). The fence is what makes
  the rollback happen: a superseded execution's checkpoint matches zero
  rows, raises inside the transaction, and takes the write with it.
* Both step primitives accept a **per-step timeout** (``timeout=`` on the
  call, or the job class's ``step_timeout``). Exceeding it raises
  ``StepTimeoutError``, which is recorded as that step's error and then
  takes the job's ordinary retry path — so the next attempt fast-forwards
  the completed prefix and re-runs only the step that hung. The job's own
  deadline is a ceiling: a per-step budget is installed only while it is
  strictly tighter than the job's remaining time.
* A **name mismatch** at a sequence number means the job code took a
  different path than the attempt that recorded the checkpoint —
  nondeterminism the author must fix (usually branching on non-checkpointed
  data such as wall-clock time or randomness).
"""

from __future__ import annotations


class DXEError(RuntimeError):
    """Base class for durable-execution errors."""


class NondeterminismError(DXEError):
    """Replay found a different step name at a recorded sequence number.

    The job code is branching on something that is not checkpointed. Wrap
    the varying computation in ``await self.step(...)`` so replays see the
    recorded value."""


class StaleExecutionError(DXEError):
    """This execution's run_epoch was superseded (the job was requeued by
    the monitor or an operator while we ran); abandon quietly — the newer
    attempt owns the row now."""


class StreamClosedError(Exception):
    """A job appended to a stream it had already closed.

    Deliberately **not** a ``DXEError``: those are control-flow signals that
    bypass checkpoint recording, and this is an ordinary step FAILURE that must
    be recorded like any other -- naming the key and the job is the whole point,
    and the job's retry budget applies to it exactly as to a step that raised.

    Raised by BOTH ``stream_write`` after ``stream_close`` and a second
    ``stream_close`` of the same key. A silently-idempotent close would be the
    friendlier-looking choice and the wrong one: the closing marker is already
    exactly-once per call site (its checkpoint fast-forwards on every replay),
    so a *second* close is not a retry, it is two call sites both believing they
    own the end of the stream. And an append after the marker is worse than a
    caller bug the platform can hide: readers stop at ``closed``, so the row
    lands where nothing will ever read it and the stream silently loses data.
    """

    def __init__(self, job_id: int, key: str, *, closing: bool) -> None:
        did = "close" if closing else "append to"
        super().__init__(
            f"job {job_id} tried to {did} stream {key!r}, which it has already "
            f"closed. A closing marker ends the stream: readers stop there, so "
            f"anything written after it is unreachable. "
            + (
                "stream_close() is exactly-once per call site already (the "
                "checkpoint fast-forwards on replay), so a second close means "
                "two call sites both think they own the end of this stream -- "
                "close it in one place."
                if closing
                else "Write every row before closing, or use a second key for "
                "the output that comes later."
            )
        )
        self.job_id = job_id
        self.key = key
        self.closing = closing


class StepTimeoutError(Exception):
    """A durable step ran longer than its per-step budget.

    Deliberately **not** a ``DXEError``: those are control-flow signals that
    bypass checkpoint recording, and a blown budget is a step *failure* that
    must be recorded — naming the step that hung is half the point of having
    per-step budgets at all.

    Deliberately **not** a ``TimeoutError`` either, and distinct from
    ``JobTimeout``: a step that blew its own budget is an ordinary step
    failure taking the ordinary retry path, not the job running out of the
    time its operator configured.
    """

    def __init__(self, name: str, timeout: float) -> None:
        super().__init__(f"step '{name}' exceeded its {timeout:g}s timeout")
        self.name = name
        self.timeout = timeout


class JobTimeout(Exception):  # noqa: N818 - names the deadline, not a fault kind
    """The job's own in-process deadline expired.

    Raised only by the worker, from the single ``asyncio.timeout`` scope that
    wraps a whole execution — never by job code. That makes "this job ran out
    of its configured time" something the worker *observed*, so the
    ``on_timeout`` policy is applied to exactly the deadline the operator set.

    Job code raises ``TimeoutError`` on its own account all the time (an inner
    ``asyncio.timeout``, an HTTP client's deadline). Telling those two apart
    by comparing clocks against the job's deadline works only while the
    deadline has visible slack around it — which is precisely what a single,
    exact ceiling removes. A distinct type needs no slack.
    """

    def __init__(self, timeout: float) -> None:
        super().__init__(f"Job timed out after {timeout:g}s")
        self.timeout = timeout


class DurableSleep(Exception):  # noqa: N818 - control-flow signal, not an error
    """Internal control-flow signal: the job checkpointed a sleep and
    rescheduled itself; unwind the execution without marking any terminal
    state. Never catch this in job code."""

    def __init__(self, wake_at: object) -> None:
        super().__init__(f"durable sleep until {wake_at}")
        self.wake_at = wake_at


# ---------------------------------------------------------------------------
# SQL used by the primitives (executed through the worker's connection)
# ---------------------------------------------------------------------------

LOAD_STEPS_SQL = """SELECT step_seq, name, output, error
        FROM jorb_step WHERE job_id = $1 ORDER BY step_seq"""

# Success and failure both record the attempt; only rows with error IS NULL
# fast-forward on replay. Fenced: the insert-select no-ops unless our epoch
# still owns the job.
#
# The fence is also what makes ``transaction()`` atomic: run inside the
# caller's transaction, "wrote nothing" means "superseded", the raise rolls
# that transaction back, and the application write goes with it. Exactly-once
# and fencing are therefore one mechanism, not two.
#
# A COMMITTED SUCCESS IS NEVER OVERWRITTEN BY AN ERROR. This closes the
# in-doubt-commit hole: transaction() commits the application write AND this
# success checkpoint together, but if the COMMIT ack is lost the client
# cannot tell it committed, falls into its error path, and reconnects to
# write an error checkpoint at the same seq. Without protection that error
# would clobber the durable success and the retry would re-run fn --
# re-delivering an exactly-once send().
#
# Done with a per-column CASE rather than a WHERE on the DO UPDATE, so the
# statement ALWAYS writes a row when the fence passes and the RETURNING is
# empty ONLY when the epoch fence itself fails -- keeping "returned nothing"
# a clean signal of supersession (a WHERE-guarded update returns empty on
# the keep case too, which _dxe_record would misread as a stale epoch). When
# the existing row is a success (error IS NULL) and the incoming write is an
# error, every column keeps its existing value: the success stands and the
# retry fast-forwards it. Every other case (error->error re-record,
# error->success, first write) takes the incoming value.
RECORD_STEP_SQL = """INSERT INTO jorb_step
            (job_id, step_seq, name, output, error, run_epoch, started, finished)
        SELECT $1, $2, $3, $4, $5, $6, $7, now()
        WHERE EXISTS (SELECT 1 FROM jorb WHERE id = $1 AND run_epoch = $6)
        ON CONFLICT (job_id, step_seq) DO UPDATE
            SET name = CASE WHEN jorb_step.error IS NULL AND EXCLUDED.error IS NOT NULL
                            THEN jorb_step.name ELSE EXCLUDED.name END,
                output = CASE WHEN jorb_step.error IS NULL AND EXCLUDED.error IS NOT NULL
                            THEN jorb_step.output ELSE EXCLUDED.output END,
                error = CASE WHEN jorb_step.error IS NULL AND EXCLUDED.error IS NOT NULL
                            THEN jorb_step.error ELSE EXCLUDED.error END,
                run_epoch = CASE WHEN jorb_step.error IS NULL AND EXCLUDED.error IS NOT NULL
                            THEN jorb_step.run_epoch ELSE EXCLUDED.run_epoch END,
                started = CASE WHEN jorb_step.error IS NULL AND EXCLUDED.error IS NOT NULL
                            THEN jorb_step.started ELSE EXCLUDED.started END,
                finished = CASE WHEN jorb_step.error IS NULL AND EXCLUDED.error IS NOT NULL
                            THEN jorb_step.finished ELSE EXCLUDED.finished END
        RETURNING step_seq"""

# Fenced like every other state-changing write. Without this a superseded
# execution could overwrite the events a live attempt has published -- and an
# event is read by other jobs and by clients, so the stale value does not stay
# inside the zombie.
SET_EVENT_SQL = """INSERT INTO jorb_event (job_id, key, value)
        SELECT $1, $2, $3
        WHERE EXISTS (SELECT 1 FROM jorb WHERE id = $1 AND run_epoch = $4)
        ON CONFLICT (job_id, key) DO UPDATE
            SET value = EXCLUDED.value, updated = now()
        RETURNING key"""

GET_EVENT_SQL = """SELECT value FROM jorb_event WHERE job_id = $1 AND key = $2"""

# Append one row to a job's stream, at the next free position, and return it.
#
# THE POSITION IS ASSIGNED IN THIS STATEMENT, never read back and reused: a
# writer that SELECTed max(seq) and then INSERTed would have to loop until it
# found a free slot, and a loop is the thing a dense sequence must not need.
# COALESCE(max(seq), -1) + 1 makes the first row seq 0 and every later row the
# one after the last, decided by the same snapshot that writes it.
#
# Run through Job.transaction(), so this row and its checkpoint commit
# together: exactly-once per call site, and a replay fast-forwards on the
# recorded seq rather than appending a second copy of the same value.
#
# Fenced on the WRITER's epoch, like every other durable write -- a superseded
# execution's appends must not land in a stream a live attempt is still
# writing, because a reader cannot tell the two writers apart.
#
# THE FENCE TAKES THE JOB ROW'S LOCK, which is what makes it a fence rather
# than a hint. An unlocked `EXISTS (SELECT ... run_epoch = $5)` is evaluated
# against this statement's own snapshot, so a zombie whose statement started
# before the requeue committed reads the OLD epoch, passes, and appends -- and
# the row then sits in the stream forever, indistinguishable to a reader from
# the live attempt's output. The window is real and was reproduced. `FOR SHARE`
# closes it because the epoch bump is an UPDATE and therefore takes the row's
# exclusive lock: the zombie's append blocks instead of proceeding, and when the
# requeue commits, READ COMMITTED re-evaluates this clause against the NEW row
# version (EvalPlanQual), finds the bumped epoch, matches nothing and appends
# nothing. In the other order the requeue is the one that waits, for as long as
# one append transaction takes, and the append it waited for was legitimate.
#
# The lock cannot live in an EXISTS subquery -- a locking clause needs rows it
# can identify with individual table rows -- so the fence is a CTE the INSERT
# selects FROM. MATERIALIZED so the lock is taken once, before the insert, and
# not folded into it.
#
# A CLOSED STREAM REFUSES FURTHER ROWS, loudly. The closing marker is where
# every reader stops, so a row appended after it is a row nothing will ever
# read: silently accepting it turns a caller bug into missing output. `shut`
# reports the marker separately from the fence because "superseded" and "you
# already closed this" need opposite responses from the caller (abandon
# quietly vs. record a step failure), and neither can be inferred from an
# empty result.
#
# Params: $1 job_id, $2 key, $3 value, $4 closed, $5 writer run_epoch.
STREAM_APPEND_SQL = """WITH locked AS MATERIALIZED (
            SELECT id FROM jorb WHERE id = $1 AND run_epoch = $5 FOR SHARE
        ), shut AS (
            SELECT 1 FROM jorb_stream
             WHERE job_id = $1 AND key = $2 AND closed
        ), appended AS (
            INSERT INTO jorb_stream
                (job_id, key, seq, value, closed, run_epoch)
            SELECT $1, $2,
                   COALESCE((SELECT max(s.seq) FROM jorb_stream s
                              WHERE s.job_id = $1 AND s.key = $2), -1) + 1,
                   $3, $4, $5
              FROM locked
             WHERE NOT EXISTS (SELECT 1 FROM shut)
            RETURNING seq
        )
        SELECT (SELECT count(*) FROM locked)::int AS fenced,
               (SELECT count(*) FROM shut)::int   AS already_closed,
               (SELECT seq FROM appended)         AS seq"""

# Discard a job's whole checkpoint log so its step sequence can restart at 1.
#
# Replay is O(steps ever recorded): pj-bench replay measures 0.9 us and 260
# bytes per checkpoint, dead linear, so 100k checkpoints cost 75ms and 26MB
# resident PER JOB, on every wake. A job whose step count tracks work done
# never gets near that; one whose step count tracks elapsed time -- a state
# machine that wakes, finds no mail and sleeps, forever -- gets there on a
# schedule. This is what bounds it.
#
# Written as one statement returning exactly one row because a bare DELETE
# cannot distinguish "nothing to remove" from "superseded", and those need
# opposite responses. `fenced` answers the second question; `removed` the
# first.
# Also drops the `awaited` notification latch. The latch's design ("set
# once, dies with the row") assumes rows die; a compacting job is exactly
# the one that never does, so one wait_for_state ever would make every
# future publish a NOTIFY-bearing commit forever. Clearing it at the turn
# boundary bounds that the same way compaction bounds replay. A wait that
# is IN FLIGHT across the clearing is degraded, not broken: its 2-second
# fallback poll still answers, and every NEW wait registers demand afresh
# before its first check. (Deliberately no waiter-side re-arm: that would
# be a write to the hottest row per fallback beat — the polling the
# demand-gated design exists to avoid.)
COMPACT_STEPS_SQL = """WITH fence AS (
            SELECT 1 FROM jorb WHERE id = $1 AND run_epoch = $2
        ), gone AS (
            DELETE FROM jorb_step
            WHERE job_id = $1 AND EXISTS (SELECT 1 FROM fence)
            RETURNING 1
        ), unlatched AS (
            UPDATE jorb SET awaited = FALSE
            WHERE id = $1 AND run_epoch = $2 AND awaited
            RETURNING 1
        )
        SELECT (SELECT count(*) FROM fence) AS fenced,
               (SELECT count(*) FROM gone) AS removed"""

# Fenced on the SENDER, not the destination: the question is whether this
# execution is still entitled to act. Unfenced, a zombie delivered the message
# and only then raised on its own checkpoint -- the effect escaping while the
# record of it was refused, which is the one ordering a durable mailbox must
# not have.
SEND_SQL = """INSERT INTO jorb_mailbox (dest_job_id, topic, message)
        SELECT $1, $2, $3
        WHERE EXISTS (SELECT 1 FROM jorb WHERE id = $4 AND run_epoch = $5)
        RETURNING id"""

# Consume one pending message (oldest first) AND checkpoint it, atomically.
#
# One statement, because a consumed-but-unrecorded message is a lost message:
# the consume stamps the only copy, and only the checkpoint lets a retry see
# it again. Two commits would leave a crash window between them; a single
# statement has none.
#
# Fenced on the consumer's own epoch, exactly like SEND_SQL is fenced on the
# sender's: a superseded execution must not eat a message the live attempt
# is entitled to. `fenced` is returned separately so the caller can tell
# "superseded" from "mailbox empty" — those need opposite responses.
#
# The `prior` guard makes the statement idempotent: if this (job, seq)
# already has a successful checkpoint — a replay after a commit raced a lost
# connection — nothing is consumed and the recorded answer comes back. The
# recorded answer may itself be NULL (a timed-out recv), which is why
# `replayed` is a separate flag rather than inferred from the output.
#
# Params: $1 dest/consumer job id, $2 step seq, $3 topic, $4 step name,
#         $5 run_epoch, $6 started timestamp.
RECV_SQL = """WITH prior AS (
            SELECT output FROM jorb_step
            WHERE job_id = $1 AND step_seq = $2 AND error IS NULL
        ), fence AS (
            SELECT 1 FROM jorb WHERE id = $1 AND run_epoch = $5
        ), msg AS (
            UPDATE jorb_mailbox
            SET consumed_at = now()
            WHERE id = (
                SELECT id FROM jorb_mailbox
                WHERE dest_job_id = $1
                  AND ($3::text IS NULL OR topic = $3)
                  AND consumed_at IS NULL
                  AND EXISTS (SELECT 1 FROM fence)
                  AND NOT EXISTS (SELECT 1 FROM prior)
                ORDER BY id
                FOR UPDATE SKIP LOCKED
                LIMIT 1
            )
            RETURNING message
        ), step AS (
            INSERT INTO jorb_step
                (job_id, step_seq, name, output, error, run_epoch,
                 started, finished)
            SELECT $1, $2, $4, msg.message, NULL, $5, $6, now() FROM msg
            ON CONFLICT (job_id, step_seq) DO UPDATE
                SET output = EXCLUDED.output,
                    error = NULL,
                    run_epoch = EXCLUDED.run_epoch,
                    started = EXCLUDED.started,
                    finished = EXCLUDED.finished
            RETURNING 1
        )
        SELECT (SELECT count(*) FROM fence) AS fenced,
               (SELECT count(*) FROM prior) AS replayed,
               (SELECT output FROM prior) AS prior_output,
               (SELECT count(*) FROM msg) AS consumed,
               (SELECT message FROM msg) AS message"""
