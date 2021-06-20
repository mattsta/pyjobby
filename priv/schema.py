""" Sample sqlalchemy schema for the pyjobby schema.

This can be used as a reference for including in your
own application endpoints as the source of truth for
pyjobby to query for jobs."""

import enum

from sqlalchemy.ext.declarative import declared_attr
from sqlalchemy import (
    Boolean,
    LargeBinary,
    Column,
    Integer,
    BigInteger,
    String,
    Text,
    UnicodeText,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    UniqueConstraint,
    func,
    Index,
    inspect,
    and_,
    or_
)

from sqlalchemy.dialects.postgresql import (
    ARRAY,
    BIGINT,
    BIT,
    BOOLEAN,
    BYTEA,
    CHAR,
    CIDR,
    DATE,
    DOUBLE_PRECISION,
    ENUM,
    FLOAT,
    HSTORE,
    INET,
    INTEGER,
    INTERVAL,
    JSON,
    JSONB,
    MACADDR,
    MONEY,
    NUMERIC,
    OID,
    REAL,
    SMALLINT,
    TEXT,
    TIME,
    TIMESTAMP,
    UUID,
    VARCHAR,
    INT4RANGE,
    INT8RANGE,
    NUMRANGE,
    DATERANGE,
    TSRANGE,
    TSTZRANGE,
    TSVECTOR,
)

# ============================================================================
# Common Helpers
# ============================================================================
# postgres CURRENT_TIMESTAMP is local timezone unless we specify otherwise
@compiles(utcnow, "postgresql")
def pg_utcnow(element, compiler, **kw):
    return "TIMEZONE('utc', CURRENT_TIMESTAMP)"


class Timestamps:
    """Default timestamp values for all rows.

    Note: we mark these columns as not-load-by-default because we probably
          don't care about them on every query.
    """

    @declared_attr
    def created(self):
        return deferred(Column(DateTime, server_default=utcnow(), nullable=False))

    # Note: 'onupdate' call is only for the sqlalchemy side, not the postgres side,
    #       so any updates outside of the sqlalchemy model won't update 'updated'
    @declared_attr
    def updated(self):
        return deferred(
            Column(DateTime, server_default=utcnow(), onupdate=utcnow(), nullable=False)
        )


# ============================================================================
# Jorbs
# ============================================================================
class JorbState(str, enum.Enum):
    """ State machine flow for job status """

    waiting = "waiting"
    queued = "queued"
    claimed = "claimed"
    running = "running"
    heartbeat = "heartbeat"
    crashed = "crashed"
    finished = "finished"


# (we disable formatting for the sqlalchemy classes becase it's easier
# to read one long line per column instead of having each column split
# into 3-5 lines based on parameter count)
# fmt: off
class Jorb(Timestamps, Base):
    # For now, we are running a scaled down queue system where the readers guarantee
    # to not conflict their writes by requesting FOR UPDATE SKIP LOCKED when selecting
    # a row for processing.
    # Ref: https://www.2ndquadrant.com/en/blog/what-is-select-skip-locked-for-in-postgresql-9-5/
    # Ref: (triggers and partial indexes) https://github.com/que-rb/que/blob/master/lib/que/migrations/4/up.sql
    # Ref: https://www.postgresql.org/docs/current/indexes-partial.html
    # Ref: https://cargopantsprogramming.com/post/queues-in-postgres/
    # Ref: https://stackoverflow.com/questions/43320065/how-to-implement-a-distributed-job-queue-using-postgresql
    # Ref: (repeating jobs) https://github.com/hlascelles/que-scheduler

    # primary lookup key order: queue, capability, priority, run_after time, then sequential ID
    queue = Column(Text, nullable=False, server_default="default", comment="Specific queue runner")
    capability = Column(Text, nullable=True, comment="Job must run in an environment with this capability (or any host if not set)")
    prio = Column(Integer, nullable=False, server_default="100", comment="Lower number means higher priority")
    run_after = Column(DateTime, nullable=False, server_default=utcnow(), comment="Minimum start time for job")
    id = Column(BigInteger, primary_key=True, comment="Job (jorb) id")

    # deadline_key singleton future runner invariant is maintained by (deadline_key, queue) unique index defined below
    deadline_key = Column(Text, nullable=True, comment="prevents duplicates when scheduling future single-instance jobs")

    # Update during job runs:
    state = Column(ENUM(JorbState), nullable=False, server_default="queued")
    run_count = Column(Integer, nullable=False, server_default="0", comment="number of times this job has been dequeued to run")

    # Defines class instantiation
    job_class = Column(Text, nullable=False, comment="Class to launch on worker (must exist on worker already)")
    kwargs = Column(JSON, nullable=False, comment="dict of kwargs for job class constructor")

    # Defines additional job accounting metadata for reports/debugging/introspection
    uid = Column(Integer, index=True, nullable=True, comment="if job is for user...")
    run_group = Column(BigInteger, nullable=True, comment="random shared ID if job is populated with other jobs (e.g. batch image upload processing)")

    # Waiting...
    # Indexes on these columns are defined in __table_args__ below
    # Note: waitfor_group is not a FK because multiple rows have the same run_group and FK must be unique
    # Also note: DO NOT set 'run_group' for your 'waitfor_group' to be the run_group you are
    #            waiting on, because then obviously the waitfor will never run since it is part
    #            of the group already, but isn't scheduled itself, so it can never complete to trigger
    #            the running of the waitfor (itself) by itself.
    # Also for future reference: the 'state' of a 'waitfor_*' should be 'waiting' and not 'queued' when you
    #                            insert the job (because 'queued' will run immediately, and 'waiting' will
    #                            hold back until picked up by its 'waitfor_' condition)
    waitfor_group = Column(BigInteger, nullable=True, comment="Move this job from 'waiting' to 'queued' ONLY when ALL jobs with this 'run_group' are reported 'finished'")

    # waitfor_job can have the same run_group as the parent job since "group-ness" doesn't impact whether the job runs or not.
    waitfor_job = Column(BigInteger, ForeignKey("jorb.id"), nullable=True, comment="Move this job from 'waiting' to 'queued' ONLY when job with this 'id' are reported 'finished'")

    # admin data...
    admin_data = Column(JSON, nullable=True, comment="tags, other admin details")

    # Update after job runs:
    result = Column(JSON, nullable=True, comment="Any non-NULL result returned by the evaluation function")
    error_message = Column(Text, nullable=True, comment="Most recent error message itself")
    error_backtrace = Column(Text, nullable=True, comment="Most recent full error backtrace")
    error_count = Column(Integer, nullable=False, server_default="0", comment="Number of times the job has failed")

    # Update when job claimed (overwritten if job runs multiple times):
    worker_pid = Column(Integer, nullable=True, comment="worker node/process last evaluated node")
    worker_host = Column(Text, nullable=True, comment="hostname of worker...")

    # Placing table_args after the columns lets us use column declarations in the DDL
    __table_args__ = (
        # Partial index for job selection
        # We also index crashed jobs so we can easily find them to retry again
        Index('jorb_poll_idx', queue, capability, prio, run_after, postgresql_where=or_(state == JorbState.queued, state == JorbState.crashed)),

        # Unique index to prevent multiple jobs being queued for a single deadline_key.
        # 'dealine_key' will use string namespacing to prevent duplicates for the same user
        # (e.g. stripe::uid::XX::update-bytes-used)
        # Also note: this *does* allow a new job to queue while an existing 'deadline_key' job is running,
        # but if the running job tries to re-enqueue itself after another job is added, it'll conflict with
        # the new later job and not run again (obviously jobs of this type shouldn't rely on their input parameters
        # for actions to run, but rather query the state of the world when they run for live actions/updates;
        # e.g. "check current bytes used by user" to query from DB and not place bytesUsed=X in the job params themselves).
        # Also also: since this is a deadline-only index, exclude deadline==NULL rows from the index to avoid unused index bloat
        Index('jorb_deadline_noconflict_idx', deadline_key, queue, unique=True, postgresql_where=and_(state == JorbState.queued, deadline_key != None)),

        # Only index non-null run_group and waiting related entries
        # Note: for the sqlalchemy overrides, != None is correct. These DO NOT WORK if you say 'x is not None'
        Index('jorb_run_group_idx', run_group, unique=False, postgresql_where=run_group != None),
        Index('jorb_waitfor_group_idx', waitfor_group, unique=False, postgresql_where=and_(waitfor_group != None, state == JorbState.waiting)),
        Index('jorb_waitfor_job_idx', waitfor_job, unique=False, postgresql_where=and_(waitfor_job != None, state == JorbState.waiting))
    )

# fmt: on
