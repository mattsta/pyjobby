-- ============================================================================
-- Notifications: one mechanism, one gate, one place to change it
-- ============================================================================
-- WHY NOTIFY IS EXPENSIVE. Committing a transaction that issued a NOTIFY
-- takes a GLOBAL exclusive lock, held until that commit completes (and
-- fsyncs). Notifications must be delivered in commit order, and commit order
-- is not established until commits finish -- so PostgreSQL serialises every
-- NOTIFY-bearing commit against every other one, defeating group commit.
-- Measured on this schema at 16 concurrent connections, one transaction per
-- job (the production enqueue shape): 12,873 jobs/s as shipped versus
-- 37,803 jobs/s with the NOTIFY triggers off. Two thirds of concurrent write
-- throughput, spent on notifications. It is invisible to a serial benchmark
-- (nothing to serialise against) and invisible to a bulk insert (one lock
-- amortised over the batch).
--
-- The lock is per COMMIT, not per notification: a transaction that notifies
-- three times pays exactly what a transaction that notifies once pays. So
-- trimming channels buys nothing. The only thing that helps is NOT NOTIFYING
-- -- and almost every notification this schema used to send was unnecessary,
-- because a notification exists to wake a consumer and under load the
-- consumers are never asleep.
--
-- THE POLICY (uniform, every channel): a notification is emitted only when a
-- consumer has REGISTERED DEMAND for its topic, and demand is registered
-- BEFORE that consumer's last look at the underlying state. The cost then
-- scales inversely with load, which is exactly right: when the system is
-- busy nobody is parked and the notification is pure overhead, paid at the
-- global commit lock precisely in the regime where it hurts most; when the
-- system is idle, latency matters but volume is low and the lock is free.
--
-- THE MECHANISM (uniform): every channel goes through jorb_notify() below.
-- The channel's topic, its demand kind and its payload are declared once, in
-- that one function, so a change to the gate or the payload convention is
-- made once instead of five times.
--
-- THE DEMAND STORAGE (deliberately NOT uniform): each channel uses the
-- cheapest correct signal for its own shape. Uniform policy, not uniform
-- cost -- an expensive lookup forced onto the write path in the name of
-- symmetry would give back what the gate just won.
--
--   channel            demand kind    what "somebody is waiting" means
--   ------------------ -------------- --------------------------------------
--   jorb_enqueued      idle_worker    a worker on that queue is parked
--   jorb_done          row_local      jorb.awaited on the very row changing
--   jorb_event         job_awaited    jorb.awaited on the publishing job
--   jorb_stream        job_awaited    jorb.awaited on the streaming job
--   jorb_cancel        row_local      the job is actually running
--   schedule_executed  ungated        (see below)
--
-- WHEN A CHANNEL MUST NOT BE GATED. A gate trades a notification for the
-- consumer's polling fallback. A consumer with NO fallback cannot pay that:
-- a skipped notification is an event it never learns about, not an event it
-- learns about late. schedule_executed feeds the push-only websocket
-- dashboard, so it stays ungated on purpose -- and it costs nothing, because
-- it fires once per schedule EXECUTION (cron rate, not job rate) on
-- jorb_schedule_log rather than on any hot path.
--
-- THE CHANNEL THAT WAS DELETED RATHER THAN GATED. job_state_change fired on
-- EVERY state transition (queued->claimed->running->finished, four per job),
-- ungated, and pushed each one to the websocket dashboard. Once every other
-- channel was gated it was the entire remaining bill: the commit lock is
-- taken per COMMIT, so one ungated channel costs exactly what five cost,
-- and deleting it is worth 2.6-2.9x on the completion path (measured across
-- runs in tests/test_notify_gating.py, which rebuilds the deleted trigger so
-- the "before" number stays measurable).
--
-- It could not be gated, because its consumer was push-only: a gate would
-- have DROPPED dashboard events, not delayed them. So the consumer got a
-- different mechanism instead. At the reference workload the channel was
-- ~830 individual transitions per second, which no dashboard renders and no
-- human reads, so pyjobby/websocket_server.py now POLLS aggregates on an
-- interval -- one index-backed query per second, shared by every connected
-- dashboard, and none at all while nobody is subscribed. A bounded,
-- predictable cost replaced one that scaled with job throughput. A client
-- that needs a specific job watches THAT job, which rides jorb_done and is
-- gated on jorb.awaited like any other waiter.
--
-- A CHANNEL WITH NO CONSUMER AT ALL. jorb_mailbox is gone. Nothing ever
-- LISTENed on it: Job.recv() polls jorb_mailbox directly every 250 ms (see
-- pyjobby/pj.py). It was a NOTIFY, and therefore a global commit lock, on
-- every durable message send, delivered to nobody.
CREATE FUNCTION jorb_notify() RETURNS trigger AS $$
DECLARE
    channel CONSTANT TEXT := TG_ARGV[0];
    demand  CONSTANT TEXT := TG_ARGV[1];
    topic   TEXT;
    wanted  BOOLEAN;
    payload TEXT;
BEGIN
    -- 1. The topic: the finest-grained thing a consumer can register demand
    --    for. One column read, and nothing else, because everything after
    --    this point is work the write path may turn out not to need.
    CASE channel
        WHEN 'jorb_enqueued'     THEN topic := NEW.queue;
        WHEN 'jorb_done'         THEN topic := NEW.id::TEXT;
        WHEN 'jorb_cancel'       THEN topic := NEW.id::TEXT;
        WHEN 'jorb_event'        THEN topic := NEW.job_id::TEXT;
        WHEN 'jorb_stream'       THEN topic := NEW.job_id::TEXT;
        WHEN 'schedule_executed' THEN topic := NEW.schedule_id::TEXT;
        ELSE RAISE EXCEPTION 'jorb_notify: unknown channel %', channel;
    END CASE;

    -- 2. The gate. Every demand predicate in the system is enumerated here.
    CASE demand
        -- The trigger's WHEN clause IS the gate: the demand signal lives on
        -- the very row being written, so the executor evaluates it for free
        -- and this function is not even reached when nobody is waiting.
        WHEN 'row_local' THEN
            wanted := TRUE;
        -- No gate, and that is a decision, not an oversight: this channel's
        -- consumer has no polling fallback.
        WHEN 'ungated' THEN
            wanted := TRUE;
        -- Some worker on this queue is parked on the wakeup. Indexed by
        -- jorb_worker_idle_idx, over a table with one row per worker
        -- process, whose idle subset is empty exactly when we are busy.
        WHEN 'idle_worker' THEN
            wanted := EXISTS (SELECT 1 FROM jorb_worker w
                               WHERE w.queue = topic
                                 AND w.idle
                                 AND w.shutdown_at IS NULL);
        -- A client is (or was) blocked waiting on this job. One primary-key
        -- probe on jorb.
        WHEN 'job_awaited' THEN
            wanted := EXISTS (SELECT 1 FROM jorb j
                               WHERE j.id = topic::BIGINT AND j.awaited);
        ELSE RAISE EXCEPTION 'jorb_notify: unknown demand kind %', demand;
    END CASE;

    IF NOT wanted THEN
        RETURN NULL;
    END IF;

    -- 3. The payload. Built only once the gate has said somebody wants it.
    CASE channel
        WHEN 'jorb_enqueued' THEN
            payload := NEW.queue;
        WHEN 'jorb_cancel' THEN
            payload := NEW.id::TEXT;
        WHEN 'jorb_done' THEN
            payload := json_build_object(
                'id', NEW.id, 'state', NEW.state)::TEXT;
        WHEN 'jorb_event' THEN
            payload := json_build_object(
                'job_id', NEW.job_id, 'key', NEW.key)::TEXT;
        WHEN 'jorb_stream' THEN
            payload := json_build_object(
                'job_id', NEW.job_id, 'key', NEW.key)::TEXT;
        WHEN 'schedule_executed' THEN
            payload := json_build_object(
                'schedule_id', NEW.schedule_id,
                'schedule_name', NEW.schedule_name,
                'result', NEW.result,
                'job_id', NEW.job_id)::TEXT;
    END CASE;

    PERFORM pg_notify(channel, payload);
    RETURN NULL;
END;
$$ LANGUAGE plpgsql;

COMMENT ON FUNCTION jorb_notify IS 'The one notification trigger. TG_ARGV = (channel, demand kind); topic, gate and payload for every channel are declared in its body.';

-- ----------------------------------------------------------------------------
-- jorb_enqueued -- wake a parked worker when work arrives on its queue
-- ----------------------------------------------------------------------------
-- WHY IT IS DEFERRED. This is a CONSTRAINT TRIGGER so the gate is evaluated
-- at COMMIT rather than at the end of the INSERT. The demand signal lives on
-- another table (jorb_worker), so unlike jorb_done there is no row lock to
-- order the two writers, and an ordinary AFTER trigger would decide whether
-- to notify from a snapshot taken when the statement ran -- possibly long
-- before the insert became visible. enqueue_in_transaction(), enqueue_batch()
-- and DAG creation all insert inside a transaction that stays open, so
-- "decide at INSERT, deliver at COMMIT" would drop the wakeup for every job
-- enqueued transactionally while a worker parked. Deferring makes the
-- decision and the delivery the same instant.
--
-- WHY NO WAKEUP IS LOST (the ordering argument; the worker's half is in
-- JobSystem.run/_set_idle):
--
--   The worker publishes idle = TRUE and only THEN makes its last claim
--   attempt, so for any inserted job J and any worker W:
--
--     * J's gate runs after W's idle=TRUE commits  ->  the EXISTS sees W,
--       the notification is emitted, W wakes.
--     * J commits before W's last claim takes its snapshot  ->  W claims J
--       and never parks in the first place.
--
--   Those two cases overlap and cover everything except one window: J's gate
--   ran (at J's commit) before W published idle, AND J was still not visible
--   when W's claim took its snapshot microseconds later. That is the gap
--   between J's WAL flush completing and J leaving the proc array, and it is
--   covered by the worker's unconditional poll every checkInterval (5s
--   default). Reversing the worker's order -- claim first, then publish idle
--   -- would open that window to the whole width of a claim round trip, on
--   every park, which is the mistake this comment exists to prevent.
CREATE CONSTRAINT TRIGGER jorb_enqueued_notify
    AFTER INSERT OR UPDATE OF state ON jorb
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW WHEN (NEW.state = 'queued')
    EXECUTE FUNCTION jorb_notify('jorb_enqueued', 'idle_worker');

-- ----------------------------------------------------------------------------
-- jorb_done -- tell a wait_for_result() caller its job reached a terminal state
-- ----------------------------------------------------------------------------
-- WHY NO WAKEUP IS LOST, and why this one needs no deferral: the demand
-- signal (jorb.awaited) is on the SAME ROW as the state change, so the two
-- writers take the same row lock and PostgreSQL orders them for us.
--
--   * waiter's UPDATE awaited=TRUE commits first -> the worker's terminal
--     UPDATE either saw it already, or blocked on the row lock and then
--     re-evaluated against the newest version, where awaited is TRUE. The
--     WHEN clause fires, the waiter is notified.
--   * terminal UPDATE commits first -> the waiter's UPDATE necessarily
--     commits after it, so the waiter's very next state read (its first
--     check, which always runs before it waits) sees the terminal state and
--     it never waits at all.
--
-- No third case exists: one of the two commits first. The client's 2s
-- fallback poll (JobClient._LISTEN_POLL_INTERVAL) is a safety net that this
-- argument does not need.
CREATE TRIGGER jorb_done_notify
    AFTER UPDATE OF state ON jorb
    FOR EACH ROW WHEN (NEW.state IN ('finished', 'crashed', 'cancelled')
                       AND NEW.awaited)
    EXECUTE FUNCTION jorb_notify('jorb_done', 'row_local');

-- ----------------------------------------------------------------------------
-- jorb_cancel -- tell the executing worker a running job should stop
-- ----------------------------------------------------------------------------
-- Already demand-gated, and always was: nobody listens for the cancellation
-- of a job that is not running (a queued job is cancelled by the same
-- statement that requests it), so `NEW.state = 'running'` IS the demand
-- signal, evaluated row-locally by the executor. It is not gated any
-- further, because the worker's cancellation listener has no polling
-- fallback -- a skipped jorb_cancel is a cancellation that never happens,
-- not one that happens late. It costs nothing to leave alone: it fires at
-- operator rate, not at job rate.
CREATE TRIGGER jorb_cancel_notify
    AFTER UPDATE OF cancel_requested ON jorb
    FOR EACH ROW WHEN (NEW.cancel_requested AND NEW.state = 'running')
    EXECUTE FUNCTION jorb_notify('jorb_cancel', 'row_local');

-- ----------------------------------------------------------------------------
-- jorb_event -- wake get_event() waiters when a job publishes a key
-- ----------------------------------------------------------------------------
-- The demand signal is jorb.awaited, one primary-key probe away, because the
-- waiter registers before the key exists: get_event() commonly waits for a
-- key the job has not published yet, so there is no jorb_event row to hang a
-- row-local flag on. That means no row lock orders the two writers, so the
-- ordering argument is the weaker one: a client that sets awaited while a
-- set_event() is mid-commit can miss that one notification and learns about
-- the key from JobClient's 2s fallback poll instead. Bounded latency on a
-- race, never a lost value -- the event itself is durable in jorb_event.
CREATE TRIGGER jorb_event_notify
    AFTER INSERT OR UPDATE ON jorb_event
    FOR EACH ROW EXECUTE FUNCTION jorb_notify('jorb_event', 'job_awaited');

-- ----------------------------------------------------------------------------
-- jorb_stream -- wake read_stream() readers when a job appends
-- ----------------------------------------------------------------------------
-- INSERT only: a stream row is never updated, and end of stream is a row of
-- its own rather than a flag flipped on an existing one, so there is exactly
-- one edge per thing a reader can learn.
--
-- Gated on jorb.awaited, like jorb_event and for the same reason: a reader
-- commonly starts before the first row exists, so there is no stream row to
-- hang a row-local flag on and the demand latch has to live on the job. Same
-- consequence, too -- a reader that registers while an append is mid-commit
-- can miss that one notification and learns about the row from the client's
-- 2-second fallback poll. Bounded latency on a race, never a lost value: the
-- rows are durable and the reader re-reads from its own offset.
--
-- The latch is per JOB, not per key. A job that streams while ANY client
-- awaits it (a wait_for_result caller, an event waiter) pays a notification
-- per append. That is the price of a demand signal cheap enough to evaluate
-- on the write path -- one primary-key probe -- and it is bounded by the
-- appends a job actually makes, which is the job's own choice.
CREATE TRIGGER jorb_stream_notify
    AFTER INSERT ON jorb_stream
    FOR EACH ROW EXECUTE FUNCTION jorb_notify('jorb_stream', 'job_awaited');

-- ----------------------------------------------------------------------------
-- (deleted) job_state_change -- the dashboard firehose
-- ----------------------------------------------------------------------------
-- Deliberately absent, and NOT to be re-added. It fired on every state
-- transition and was the last ungated NOTIFY in the system, which made it
-- the whole remaining commit-lock bill: with it gone, the CLAIM and COMPLETE
-- paths stop paying the lock the ENQUEUE path already stopped paying. See
-- "THE CHANNEL THAT WAS DELETED RATHER THAN GATED" above for why deletion
-- rather than a gate, and where its consumer gets its data now.

