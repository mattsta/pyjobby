"""DXE — the Durable Execution Engine's shared vocabulary.

Jobs gain durable primitives on the Job base class (``await self.step``,
``await self.sleep``, ``await self.set_event``, ``await self.send`` /
``recv``); this module holds the exceptions and SQL those primitives share.

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
RECORD_STEP_SQL = """INSERT INTO jorb_step
            (job_id, step_seq, name, output, error, run_epoch, started, finished)
        SELECT $1, $2, $3, $4, $5, $6, $7, now()
        WHERE EXISTS (SELECT 1 FROM jorb WHERE id = $1 AND run_epoch = $6)
        ON CONFLICT (job_id, step_seq) DO UPDATE
            SET output = EXCLUDED.output,
                error = EXCLUDED.error,
                run_epoch = EXCLUDED.run_epoch,
                started = EXCLUDED.started,
                finished = EXCLUDED.finished
        RETURNING step_seq"""

SET_EVENT_SQL = """INSERT INTO jorb_event (job_id, key, value)
        VALUES ($1, $2, $3)
        ON CONFLICT (job_id, key) DO UPDATE
            SET value = EXCLUDED.value, updated = now()"""

GET_EVENT_SQL = """SELECT value FROM jorb_event WHERE job_id = $1 AND key = $2"""

SEND_SQL = """INSERT INTO jorb_mailbox (dest_job_id, topic, message)
        VALUES ($1, $2, $3) RETURNING id"""

# Consume exactly one pending message (oldest first) for this job/topic.
RECV_SQL = """UPDATE jorb_mailbox
        SET consumed_at = now()
        WHERE id = (
            SELECT id FROM jorb_mailbox
            WHERE dest_job_id = $1
              AND ($2::text IS NULL OR topic = $2)
              AND consumed_at IS NULL
            ORDER BY id
            FOR UPDATE SKIP LOCKED
            LIMIT 1
        )
        RETURNING message"""
