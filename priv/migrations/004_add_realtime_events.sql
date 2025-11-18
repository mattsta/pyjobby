-- Migration: Add real-time event notifications
-- Purpose: Enable WebSocket live updates for job state changes and schedule executions
-- Date: 2025-11-18

-- ============================================================================
-- Job State Change Notifications
-- ============================================================================

-- Function to notify on job state changes
CREATE OR REPLACE FUNCTION notify_job_state_change()
RETURNS TRIGGER AS $$
DECLARE
    payload JSON;
BEGIN
    -- Only notify on actual state changes
    IF OLD.state IS DISTINCT FROM NEW.state THEN
        payload := json_build_object(
            'job_id', NEW.id,
            'old_state', OLD.state,
            'new_state', NEW.state,
            'queue', NEW.queue,
            'job_class', NEW.job_class,
            'priority', NEW.prio,
            'timestamp', EXTRACT(EPOCH FROM NOW())::bigint
        );

        PERFORM pg_notify('job_state_change', payload::text);
    END IF;

    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- Trigger on job state update
DROP TRIGGER IF EXISTS job_state_change_trigger ON jorb;

CREATE TRIGGER job_state_change_trigger
AFTER UPDATE OF state ON jorb
FOR EACH ROW
EXECUTE FUNCTION notify_job_state_change();


-- ============================================================================
-- Schedule Execution Notifications
-- ============================================================================

-- Function to notify on schedule executions
CREATE OR REPLACE FUNCTION notify_schedule_executed()
RETURNS TRIGGER AS $$
DECLARE
    payload JSON;
    next_run TIMESTAMPTZ;
BEGIN
    -- Get next run time for schedule
    SELECT jorb_schedule.next_run INTO next_run
    FROM jorb_schedule
    WHERE id = NEW.schedule_id;

    -- Build payload
    payload := json_build_object(
        'schedule_id', NEW.schedule_id,
        'schedule_name', NEW.schedule_name,
        'job_id', NEW.job_id,
        'result', NEW.result,
        'scheduled_time', NEW.scheduled_time,
        'actual_time', NEW.actual_time,
        'next_run', next_run,
        'duration_ms', NEW.duration_ms,
        'error_message', NEW.error_message,
        'timestamp', EXTRACT(EPOCH FROM NOW())::bigint
    );

    PERFORM pg_notify('schedule_executed', payload::text);

    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- Trigger on schedule log insert
DROP TRIGGER IF EXISTS schedule_executed_trigger ON jorb_schedule_log;

CREATE TRIGGER schedule_executed_trigger
AFTER INSERT ON jorb_schedule_log
FOR EACH ROW
EXECUTE FUNCTION notify_schedule_executed();


-- ============================================================================
-- Queue Alert Notifications (Optional)
-- ============================================================================

-- Function to check queue depth and send alerts
CREATE OR REPLACE FUNCTION check_queue_alerts()
RETURNS TRIGGER AS $$
DECLARE
    queue_depth BIGINT;
    payload JSON;
    alert_threshold CONSTANT INTEGER := 1000;
BEGIN
    -- Only check on INSERT (new jobs)
    IF TG_OP = 'INSERT' AND NEW.state = 'queued' THEN
        -- Get current queue depth
        SELECT COUNT(*) INTO queue_depth
        FROM jorb
        WHERE queue = NEW.queue
          AND state = 'queued';

        -- Send alert if over threshold
        IF queue_depth > alert_threshold THEN
            payload := json_build_object(
                'queue', NEW.queue,
                'depth', queue_depth,
                'threshold', alert_threshold,
                'severity', 'warning',
                'message', format('Queue %s has %s jobs (threshold: %s)',
                                 NEW.queue, queue_depth, alert_threshold),
                'timestamp', EXTRACT(EPOCH FROM NOW())::bigint
            );

            PERFORM pg_notify('queue_alert', payload::text);
        END IF;
    END IF;

    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- Trigger for queue alerts (only fires occasionally to avoid spam)
-- Uses WHEN clause to limit checks
DROP TRIGGER IF EXISTS queue_alert_trigger ON jorb;

CREATE TRIGGER queue_alert_trigger
AFTER INSERT ON jorb
FOR EACH ROW
WHEN (NEW.state = 'queued' AND random() < 0.1)  -- Check 10% of inserts
EXECUTE FUNCTION check_queue_alerts();


-- ============================================================================
-- Job Creation Notifications (for live monitoring)
-- ============================================================================

-- Function to notify on job creation
CREATE OR REPLACE FUNCTION notify_job_created()
RETURNS TRIGGER AS $$
DECLARE
    payload JSON;
BEGIN
    -- Only notify for newly created jobs
    IF NEW.state IN ('queued', 'waiting') THEN
        payload := json_build_object(
            'job_id', NEW.id,
            'job_class', NEW.job_class,
            'queue', NEW.queue,
            'state', NEW.state,
            'priority', NEW.prio,
            'run_after', NEW.run_after,
            'capability', NEW.capability,
            'uid', NEW.uid,
            'timestamp', EXTRACT(EPOCH FROM NOW())::bigint
        );

        PERFORM pg_notify('job_state_change', payload::text);
    END IF;

    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- Trigger on job insert (with rate limiting)
DROP TRIGGER IF EXISTS job_created_trigger ON jorb;

CREATE TRIGGER job_created_trigger
AFTER INSERT ON jorb
FOR EACH ROW
WHEN (random() < 0.2)  -- Only notify 20% of new jobs to avoid spam
EXECUTE FUNCTION notify_job_created();


-- ============================================================================
-- Comments and Documentation
-- ============================================================================

COMMENT ON FUNCTION notify_job_state_change() IS
'Sends PostgreSQL NOTIFY when job state changes.
Payload includes job_id, old_state, new_state, queue, job_class.
Consumed by event emitter service for WebSocket broadcasting.';

COMMENT ON FUNCTION notify_schedule_executed() IS
'Sends PostgreSQL NOTIFY when schedule executes.
Payload includes schedule details, result, and next run time.
Consumed by event emitter service for WebSocket broadcasting.';

COMMENT ON FUNCTION check_queue_alerts() IS
'Checks queue depth and sends alert if over threshold.
Only checks 10% of inserts to avoid performance impact.
Threshold is currently hardcoded to 1000 jobs.';

COMMENT ON FUNCTION notify_job_created() IS
'Sends PostgreSQL NOTIFY when job is created.
Only notifies 20% of new jobs to avoid overwhelming clients.
Full job list can be retrieved via REST API.';


-- ============================================================================
-- Health Check: Verify Triggers
-- ============================================================================

-- Query to check if triggers are installed
SELECT
    trigger_name,
    event_manipulation,
    event_object_table,
    action_statement
FROM information_schema.triggers
WHERE trigger_name IN (
    'job_state_change_trigger',
    'schedule_executed_trigger',
    'queue_alert_trigger',
    'job_created_trigger'
)
ORDER BY trigger_name;


-- ============================================================================
-- Testing: Manual NOTIFY
-- ============================================================================

-- You can manually test notifications:
-- SELECT pg_notify('job_state_change', '{"job_id": 123, "old_state": "queued", "new_state": "running"}');

-- Listen for notifications (in psql):
-- LISTEN job_state_change;
-- LISTEN schedule_executed;
-- LISTEN queue_alert;
