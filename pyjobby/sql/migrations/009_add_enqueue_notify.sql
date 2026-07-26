-- Migration 009: Reliable enqueue notification for worker wakeup
--
-- Workers LISTEN on 'jorb_enqueued' and wake immediately when a job becomes
-- claimable in their queue, instead of discovering it on the next poll tick
-- (median enqueue->start latency drops from ~half the poll interval to
-- milliseconds; polling remains as a fallback for run_after-delayed jobs).
--
-- Unlike the dashboard triggers from migration 004 (which are sampled and
-- lossy by design), this trigger fires on EVERY transition into 'queued':
-- inserts, retries, requeues, and dependency wakeups. The payload is just
-- the queue name.

CREATE OR REPLACE FUNCTION notify_jorb_enqueued() RETURNS trigger AS $$
BEGIN
    PERFORM pg_notify('jorb_enqueued', NEW.queue);
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS jorb_enqueued_notify ON jorb;
CREATE TRIGGER jorb_enqueued_notify
    AFTER INSERT OR UPDATE OF state ON jorb
    FOR EACH ROW
    WHEN (NEW.state = 'queued')
    EXECUTE FUNCTION notify_jorb_enqueued();

COMMENT ON FUNCTION notify_jorb_enqueued IS
    'Wakes idle workers listening on the jorb_enqueued channel; payload is the queue name.';
