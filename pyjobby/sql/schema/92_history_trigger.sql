-- ============================================================================
-- History recording: one trigger, every writer covered
-- ============================================================================
-- The worker, monitor, scheduler, admin API, and websocket server all mutate
-- jorb; recording transitions here means none of them can forget to.
CREATE FUNCTION record_jorb_history() RETURNS trigger AS $$
BEGIN
    IF TG_OP = 'INSERT' THEN
        INSERT INTO jorb_history (job_id, event, detail)
        VALUES (NEW.id, 'enqueued', jsonb_build_object(
            'queue', NEW.queue, 'job_class', NEW.job_class,
            'state', NEW.state, 'prio', NEW.prio));
    ELSIF OLD.state IS DISTINCT FROM NEW.state THEN
        INSERT INTO jorb_history (job_id, event, detail)
        VALUES (NEW.id, NEW.state::text, jsonb_build_object(
            'from', OLD.state,
            'run_epoch', NEW.run_epoch,
            'run_count', NEW.run_count,
            'error_count', NEW.error_count,
            'worker_host', NEW.worker_host,
            'worker_pid', NEW.worker_pid,
            'error', CASE WHEN NEW.state IN ('queued','crashed')
                          THEN NEW.error_message END));
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER jorb_history_record
    AFTER INSERT OR UPDATE OF state ON jorb
    FOR EACH ROW EXECUTE FUNCTION record_jorb_history();

-- Dashboard schedule execution feed. UNGATED because its consumer is
-- push-only (no polling fallback), and affordable because it fires once per
-- schedule EXECUTION -- cron rate, not job rate -- on jorb_schedule_log
-- rather than on any hot path.
CREATE TRIGGER schedule_executed_notify
    AFTER INSERT ON jorb_schedule_log
    FOR EACH ROW EXECUTE FUNCTION jorb_notify('schedule_executed', 'ungated');
