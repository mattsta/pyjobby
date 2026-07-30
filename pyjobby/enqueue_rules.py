"""What an enqueue will and will not accept, declared in one place.

Every writer in the platform assembles its row through
``JobClient.build_enqueue_row`` -- the plain enqueue, the caller's own
transaction, the batch, ``debounce()``, the DAG, the recurring scheduler --
and each of them has to refuse the same values for the same reasons. The
rules used to live inside ``client.py`` beside the connection handling and
the twenty-odd public verbs, which had two costs: a reader looking for "why
was this refused?" had to find it in five thousand lines, and the modules
that needed one rule (``db.fork_job`` wanting the app_version bound,
``dag`` wanting one refusal string) imported the whole client back --
function-locally, to dodge the cycle that top-level import would have made.

This module is the rules and nothing else: bounds, validators and the refusal
messages, all pure functions over pure data. It imports nothing from the
package, so anything may import it at the top of the file, and the layering
runs one way.

The rules themselves come in three kinds:

* **BOUNDS** on the caller-chosen strings (``MAX_KEY_LENGTH``,
  ``MAX_APP_VERSION_LENGTH``). Each one is a value something INDEXES, GROUPS
  BY or compares for EQUALITY on the claim path, so an unbounded value is an
  unbounded cost paid forever by a reader that has no caller left to tell.
* **MUTUAL EXCLUSIONS** between the enqueue-side keys. Each key answers "what
  happens to a duplicate?" and they answer it differently, so a row carrying
  two of them would have to do two contradictory things at once -- and which
  one it did would depend on which index it collided with first.
* **COMBINATIONS THAT WOULD SILENTLY DO NOTHING**, which is the subtler
  family and the one that keeps growing. A ``debounce_key`` or a
  ``deadline_key`` on a job inserted 'waiting' is held by no index and
  cleared by the wake, so the caller asks for collapsing and gets every
  duplicate, with no error anywhere. Refused, because silence in the
  direction of duplicate work is the expensive kind.

``client`` re-exports every name here at its old path, so nothing outside the
package has to know the split happened.
"""

from __future__ import annotations

from typing import Any, Final


class SpeculativeEnqueueExhausted(RuntimeError):
    """A speculative enqueue's whole retry budget was consumed by other writers.

    ``identity_key`` and ``debounce_key`` are written with the same shape:
    ``INSERT ... ON CONFLICT DO NOTHING`` against a caller-chosen key, re-run
    on a fresh snapshot when it comes back empty. Empty means another
    transaction committed the key AFTER this statement's snapshot was taken,
    so the statement could neither insert over the row nor see it -- and the
    fix is a new snapshot, not a longer statement. One retry is normally the
    whole of it.

    Reaching the end of the budget means one of two things, and the message
    says both because they need opposite responses. Either an unbroken stream
    of writers is claiming this one key (rare, real, and a sign the key is not
    as specific as its author thought), or the call is running at REPEATABLE
    READ or higher -- where a retry REUSES the transaction's snapshot, so the
    loop can never see the row and burning the budget was inevitable.

    A named type rather than a bare ``RuntimeError`` because it is the one
    outcome of these two verbs a caller can sensibly act on (back off, or fix
    the isolation level), and because it carries ``kind`` and ``key`` so a
    handler does not have to parse the sentence to find out which key lost.
    Subclasses ``RuntimeError``, which is what both loops raised before it
    existed, so every existing ``except RuntimeError`` still catches it.
    """

    def __init__(self, kind: str, key: str, attempts: int, message: str) -> None:
        super().__init__(message)
        #: ``"identity_key"`` or ``"debounce_key"`` -- which verb lost.
        self.kind = kind
        #: The caller's key, so a handler can log or re-key without parsing.
        self.key = key
        #: How many fresh snapshots were taken before giving up.
        self.attempts = attempts


# What a tag value may be. Tags exist to be FILTERED on, and filtering goes
# through `tags @> '{"key": value}'` against a GIN index, so a value has to be
# something a caller can write down in a query -- and, one layer out, in a
# `pj-admin jobs list --tag key=value` argument. Containment against a nested
# object or an array is a different question with different (surprising)
# semantics, so those are refused at the door instead of being accepted and
# then silently unfilterable.
_TAG_VALUE_TYPES = (str, int, float, bool, type(None))

# What `on_timeout` may say. The worker asks `on_timeout == 'retry'` and
# treats everything else as terminal (pj.py `_handle_failure`), so an
# unrecognized value is not ignored -- it dead-letters the job on its first
# overrun. Checked at enqueue, where the caller is still there to be told.
_ON_TIMEOUT_POLICIES = frozenset({"retry", "fail"})

# The priority ceiling a worker claims under, and the default for `pj
# --max-prio`. `claim_jorb()` takes only jobs whose `prio <= the claiming
# worker's ceiling`, so a job above every live worker's ceiling is never
# claimed, never fails, never reaches the DLQ and never shows up in
# `doctor`: it is simply `queued` forever. The number lives HERE, on the
# enqueue side, because this is the only place a caller can still be told --
# and `pj` imports it for `JobSystem.prio` and its own flag default, so the
# two halves of the contract cannot drift apart.
DEFAULT_PRIO_CEILING: Final = 1000

#: Why a batch cannot carry identity keys. A batch is ONE multi-row INSERT
#: whose contract is "the ids, in the order given" -- and the identified
#: write resolves each conflict into an id to hand back, which a single
#: RETURNING cannot do for the rows it did not insert. Accepting the option
#: and silently dropping collided rows would break that contract in the way
#: hardest to notice: a shorter list, misaligned with the input. So it is
#: refused at the door, and the caller loops enqueue_identified().
_NO_BATCH_IDENTITY: Final = (
    "identity_key is not a batch option: a batch is one INSERT returning one "
    "id per row IN ORDER, and an identity that already exists has no row in "
    "it to return. Enqueue identified jobs one at a time with "
    "enqueue_identified(), which tells you which ones were already there."
)

#: Why a batch cannot carry debounce keys either, and it is a different
#: reason. A batch is a plain multi-row INSERT: it has no bounce statement in
#: front of it, so a key already held would not collapse -- the row would
#: simply violate jorb_debounce_idx and take the whole batch down with it.
#: Every guarantee debounce makes lives in JobClient.debounce()'s
#: bounce-or-insert pair, so the option is refused where it would silently
#: mean nothing.
_NO_BATCH_DEBOUNCE: Final = (
    "debounce_key is not a batch option: collapsing a burst is a "
    "bounce-or-insert pair of statements, and a batch is one INSERT with no "
    "bounce in front of it -- a key already held would fail the batch rather "
    "than collapse into the job holding it. Call debounce() per key."
)

#: Why the three enqueue-side keys cannot be combined. Each one answers "what
#: happens to a DUPLICATE enqueue?" and they answer it differently: an
#: identity_key hands back the existing job untouched, a deadline_key raises
#: and leaves it untouched, a debounce_key moves it later and rewrites its
#: arguments. A row carrying two of them would have to do two of those at
#: once, so the combination is a design error in the caller and is refused
#: loudly rather than resolved by whichever statement happens to run.
_KEYS_CONTRADICT: Final = (
    "debounce_key cannot be combined with {other}: they promise different "
    "things about a duplicate enqueue -- debounce_key defers the existing "
    "job and replaces its kwargs, identity_key returns it untouched, and "
    "deadline_key refuses the duplicate outright. Pick the one whose "
    "promise you want (docs/writing-jobs.md, 'Choosing your dedupe "
    "primitive')."
)

#: The same contradiction between the OTHER two keys, which the comment above
#: promised was mutually exclusive and which nothing enforced. A row carrying
#: both would have the identified statement resolve the conflict by handing the
#: existing job back while jorb_deadline_idx was meanwhile refusing duplicates
#: of the same work outright -- so which promise a caller got depended on which
#: index the row happened to collide with first.
_IDENTITY_AND_DEADLINE: Final = (
    "identity_key cannot be combined with deadline_key: they promise opposite "
    "things about a duplicate enqueue -- identity_key hands back the existing "
    "job (at most once, for the life of the row), deadline_key raises and then "
    "RE-ARMS the moment the job is claimed, so tomorrow's submission is a new "
    "job. Those cannot both be true of one row. Pick the one whose promise you "
    "want (docs/writing-jobs.md, 'Choosing your dedupe primitive')."
)

#: Why an identified enqueue cannot also carry a dependency edge.
#:
#: An identified enqueue's whole contract is that it may return a job it did
#: NOT create. That job has whatever dependency the enqueue which really made
#: it asked for -- a different upstream, a different group, or none at all, and
#: it may have finished months ago. So the caller's `waitfor_job=X` is silently
#: not applied: nothing raises, nothing waits, and the ordering the caller
#: asked for simply does not exist. Refused, because the failure is invisible
#: at the call site and shows up as work that ran too early.
_NO_IDENTITY_WAITFOR: Final = (
    "identity_key cannot be combined with waitfor_job/waitfor_group: an "
    "identified enqueue may return a job it did not create, and that job "
    "already has whatever dependency (or none) the enqueue that really made it "
    "asked for -- so this dependency would silently not be applied and the "
    "work would run unordered. Give the identity to the job that does the "
    "work and let an unidentified waiter depend on it, or key the identity to "
    "include the upstream."
)

#: Why a DAG node cannot carry an identity_key.
#:
#: A DAG node is enqueued and then WIRED: `execute()` rewrites dag_id and
#: run_group on the ids it just got back. An identity that already existed
#: hands back somebody else's job, so the wiring rewrites a PRE-EXISTING row --
#: taking it out of the DAG it belongs to and into this one, mid-flight, with
#: its old DAG left reporting a member it no longer has. Observed, not
#: theorised. There is nothing to resolve here: a graph is a set of jobs
#: created together, and a node that might already exist is not one of them.
_NO_DAG_IDENTITY: Final = (
    "identity_key is not a DAG node option: a DAG enqueues its nodes and then "
    "stamps dag_id and run_group onto the ids it got back, and an identity "
    "that already exists hands back a job this DAG did not create -- so the "
    "stamp would STEAL a live job out of its own DAG and rewire it into this "
    "one. Enqueue the identified job on its own and have the DAG depend on it."
)

#: Why the plain enqueue paths refuse a debounce_key, and it is the batch's
#: reason (see _NO_BATCH_DEBOUNCE) reached by a different door: enqueue() and
#: enqueue_in_transaction() run the plain INSERT, with no bounce statement in
#: front of it. A key already held therefore does not collapse -- it raises a
#: unique violation, which in the outbox case aborts the CALLER's transaction
#: and takes their application write with it -- and a key not yet held silently
#: writes a row with no ``debounce_deadline``, an uncapped collapse window that
#: nothing will ever clamp and that later bounces will defer forever.
#:
#: One constant for both because they are one statement: enqueue() IS
#: enqueue_in_transaction() on a pooled connection. Every guarantee the option
#: implies lives in JobClient.debounce()'s bounce-or-insert pair, which is what
#: the schema's own COMMENT on jorb.debounce_key has always said ("Set only by
#: JobClient.debounce()").
_NO_OUTBOX_DEBOUNCE: Final = (
    "debounce_key is not an enqueue() / enqueue_in_transaction() option: "
    "collapsing a burst is a bounce-or-insert pair of statements and these "
    "paths run the plain INSERT -- a key already held would raise instead of "
    "collapsing (aborting the caller's transaction, in the outbox case), and a "
    "key not yet held would open a collapse window with no cap to clamp it. "
    "Call debounce(key=..., period=..., cap=...), which owns that pair."
)

#: Why a debounced job cannot also wait on something. `waitfor_job` /
#: `waitfor_group` insert the row as 'waiting', and jorb_debounce_idx covers
#: QUEUED rows only -- so the key would not be held, no duplicate would ever
#: find the row to collapse onto, and every call in the burst would write
#: another job. Refused rather than silently degrading to no debouncing at all.
_NO_DEBOUNCE_WAITFOR: Final = (
    "debounce_key cannot be combined with waitfor_job/waitfor_group: a "
    "dependent job is inserted 'waiting', and the collapse window is held by "
    "a QUEUED row -- so nothing would ever collapse and every call would "
    "write another job. Debounce the work that runs after the wait instead."
)

#: Why a WAITING job cannot hold a deadline_key either, and it is the same
#: shape one index over: jorb_deadline_idx is partial on ``state = 'queued'``,
#: so a row inserted 'waiting' is outside it and refuses nothing. Worse than
#: inert -- the wake CLEARS deadline_key on the way into 'queued'
#: (db.WAKE_CLEARS_KEYS, and it must, because several waiters of one upstream
#: may legally hold the same key and the wake is ONE statement over all of
#: them). So the key is dropped at the exact moment the row would first have
#: entered the index: the collapse window the caller asked for never opens at
#: any point in the row's life, and every duplicate becomes its own job. That
#: is silence in the direction that costs money -- duplicate work -- so it is
#: refused beside its debounce twin rather than accepted and ignored.
_NO_DEADLINE_WAITFOR: Final = (
    "deadline_key cannot be combined with waitfor_job/waitfor_group: a "
    "dependent job is inserted 'waiting', which is outside the unique index "
    "that refuses duplicates, and the wake CLEARS deadline_key on the way "
    "into 'queued' (several waiters of one upstream may legally share a key, "
    "so the wake has to). The key would therefore never collapse anything and "
    "every duplicate would run. Put the deadline_key on the job that does the "
    "work and let an unidentified waiter depend on it."
)


#: Longest ``app_version`` an enqueue accepts.
#:
#: A version string is a build identifier -- a tag, a git sha, a release date,
#: at worst all three -- and it is compared for EQUALITY by every claim on the
#: queue and carried in operator-facing messages that have to stay one line.
#: 128 characters is past every real one and short enough that neither is a
#: problem. Bounded at the door for the same reason ``partition_key`` is: past
#: the enqueue there is no caller left to tell.
MAX_APP_VERSION_LENGTH: Final = 128

#: Why an empty ``app_version`` is refused rather than stored.
#:
#: NULL is how a job says "not pinned", and it is the default. An empty string
#: is a DIFFERENT value that no worker can ever advertise (`pj --app-version
#: ""` is the same as passing nothing), so a row carrying one is pinned to a
#: version that cannot exist -- unclaimable forever, and reported as wanting
#: version ''. It is almost always a variable that came back empty: an unset
#: ``$GIT_SHA``, a build stamp the CI step did not write. Refused here, where
#: the caller is still around to hear about it.
_EMPTY_APP_VERSION: Final = (
    "app_version is empty: NULL/None is how a job says it is not pinned to a "
    "code version (and is the default), while '' would pin it to a version no "
    "worker can advertise -- the job would sit 'queued' forever. This is "
    "usually an unset build variable; omit the argument to enqueue unpinned "
    "work."
)


def validate_app_version(app_version: str | None) -> str | None:
    """Check an ``app_version`` and return it, or None for unpinned work.

    One home for the three ways a version pin goes wrong before it is written
    -- the wrong TYPE, empty (a build variable that came back blank, pinning
    the job to a version nothing can advertise) and unbounded (a string in the
    claim's equality test and in every message about the job) -- so the enqueue
    paths and ``update_job_app_version`` refuse the same values with the same
    words.

    THE TYPE CHECK IS A REFUSAL, NOT A COERCION, and the two surfaces have to
    agree on that. ``app_version = 20260728`` in a TOML file is an integer, and
    it is the natural way to write a date-stamped version without thinking
    about quotes. Coerced to ``"20260728"`` here, the job is pinned to a string
    whose only claim of being right is that ``str()`` happened to produce it;
    refused, the caller sees the missing quotes at the one moment they can fix
    them. ``pj``'s launcher-side ``resolve_app_version`` refuses the same input
    for the same reason -- a fleet advertising a coerced version and a client
    stamping an uncoerced one is the pin failing in the direction it exists to
    prevent.
    """
    if app_version is None:
        return None
    if not isinstance(app_version, str):
        raise ValueError(
            f"app_version must be a string, got {type(app_version).__name__} "
            f"({app_version!r}): it names a BUILD and is compared for EQUALITY "
            f"against what a worker advertises, so a value that is not already "
            f"the string both halves will use is refused rather than coerced "
            f"into one. In a TOML config this is almost always missing quotes."
        )
    if not app_version.strip():
        raise ValueError(_EMPTY_APP_VERSION)
    if len(app_version) > MAX_APP_VERSION_LENGTH:
        raise ValueError(
            f"app_version is {len(app_version)} characters, above the "
            f"{MAX_APP_VERSION_LENGTH} the platform accepts: it names a BUILD "
            f"(a tag, a sha, a release stamp), is compared for equality by "
            f"every claim on the queue, and is printed in the messages that "
            f"say why a job is not running"
        )
    return app_version


#: Longest any caller-chosen key an enqueue accepts may be, and the shortest.
#:
#: One bound for all four (deadline_key, identity_key, debounce_key,
#: partition_key) because they are the same KIND of thing: a name the caller
#: chose, stored in a column something INDEXES or GROUPS BY, never a payload.
#: partition_key documented the reasoning first (MAX_PARTITION_KEY_LENGTH,
#: which this unifies) and the argument transfers unchanged: an unbounded key
#: is an unbounded string in a btree the enqueue path writes and the claim path
#: reads. 256 characters is far past every real one -- an order id, a tenant, a
#: date-stamped digest name, a ULID -- and short enough that a saturated queue's
#: worth of them is still small.
MAX_KEY_LENGTH: Final = 256

#: Longest ``partition_key`` an enqueue accepts.
#:
#: A partition key is a GROUPING KEY read inside the serialised claim
#: section, not a payload: on a queue with ``partition_limits`` every
#: saturated lane's key is carried in an array that the claim's per-row test
#: probes, so an unbounded key would put an unbounded string into the one
#: critical section that sets a capped queue's whole ceiling. Refused at the
#: door, where the caller can still be told, rather than accepted and paid for
#: on every claim forever.
#:
#: The SAME bound as every other caller-chosen key, and named separately only
#: because the name is public API; :data:`MAX_KEY_LENGTH` is where the number
#: and the reasoning live.
MAX_PARTITION_KEY_LENGTH: Final = MAX_KEY_LENGTH


def validate_key(name: str, value: str | None) -> str | None:
    """Check one caller-chosen key column and return it, or None if unset.

    THE one validator for deadline_key, identity_key, debounce_key and
    partition_key, so no key can be refused on one path and accepted on
    another. None means "not using this feature" and is always fine; anything
    else has to be a name, which means non-empty and bounded.

    An EMPTY key is refused rather than stored because it is not the same thing
    as no key at all and behaves nothing like it: `''` is a real value, so it
    takes a slot in that column's unique index and every OTHER caller who
    passed an empty key collides with it -- unrelated jobs deduplicating
    against each other, or (for partition_key) sharing one fair-share lane
    while the NULL lane sits beside them. It is almost always a variable that
    came back blank: an f-string over a missing id, a config value the
    deployment did not set. Refused here, where the caller is still around to
    hear about it.
    """
    if value is None:
        return None
    if not value.strip():
        raise ValueError(
            f"{name} is empty: None is how a job says it is not using this "
            f"feature (and is the default), while '' is a real key -- it takes "
            f"a slot in that column's index, so every other caller who passed "
            f"an empty {name} "
            + (
                "would share ONE fair-share lane with this job, and a lane "
                "shared by everyone who forgot the key is not a lane."
                if name == "partition_key"
                else "would collide with this job."
            )
            + f" This is usually an f-string over a value that was missing; "
            f"omit the argument instead."
        )
    if len(value) > MAX_KEY_LENGTH:
        raise ValueError(
            f"{name} is {len(value)} characters, above the {MAX_KEY_LENGTH} the "
            f"platform accepts: it is a NAME the caller chose, stored in a "
            f"column the enqueue path indexes and the claim path reads, not a "
            f"payload — key it to an id, a tenant or a date stamp rather than "
            f"to the data itself"
        )
    return value


def validate_priority(priority: int, ceiling: int = DEFAULT_PRIO_CEILING) -> int:
    """Refuse a priority no worker at `ceiling` could ever claim.

    The ordering is inverted from the intuition -- LOWER is MORE urgent --
    so "low priority, whenever you get to it" is written as a big number by
    everyone who has not read the schema, and a big number is not slow: it
    is *unclaimable*, permanently, with no signal anywhere.

    This is deliberately checked against a number the client was TOLD rather
    than one it can observe: the ceiling belongs to the worker fleet
    (``pj --max-prio``) and nothing about it is visible from a connection.
    A deployment that raises it says so once, when it builds the client
    (``JobClient(pool, prio_ceiling=N)``), which is where deployment facts
    already live. The asymmetry is what settles it: a wrong refusal is loud,
    immediate and a one-line fix at the call site, while a wrong acceptance
    is a job that is silently never run.
    """
    if priority > ceiling:
        raise ValueError(
            f"priority {priority} is above the worker priority ceiling "
            f"({ceiling}): workers claim only jobs with prio <= their "
            f"ceiling, so this job would sit 'queued' forever -- no error, "
            f"no retry, no DLQ. LOWER numbers are MORE urgent, so "
            f"least-urgent work wants a priority just UNDER the ceiling "
            f"(e.g. {ceiling - 100}), not a large one. If this deployment "
            f"really runs its workers with `pj --max-prio {priority}` (or "
            f"higher), declare it once: JobClient(pool, "
            f"prio_ceiling={priority})."
        )
    return priority


def validate_tags(tags: dict[str, Any] | None) -> dict[str, Any]:
    """Check caller-supplied tags and return a copy safe to store.

    Copied rather than used in place for the same reason admin_data is: the
    row we build must not be a live view of a dict the caller still holds.
    """
    if not tags:
        return {}
    if not isinstance(tags, dict):
        raise ValueError(f"tags must be a dict, got {type(tags).__name__}")
    for key, value in tags.items():
        if not isinstance(key, str) or not key:
            raise ValueError(f"tag keys must be non-empty strings, got {key!r}")
        if not isinstance(value, _TAG_VALUE_TYPES):
            raise ValueError(
                f"tag {key!r} has value of type {type(value).__name__}; tag "
                "values must be a string, number, boolean or None (nested "
                "objects and arrays cannot be filtered with --tag key=value)"
            )
    return dict(tags)
