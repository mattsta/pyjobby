-- ============================================================================
-- DAG completion: stamped when a DAG's last job reaches a terminal state
-- ============================================================================
-- Recorded by trigger (like history) so every writer — worker, monitor,
-- admin API — keeps jorb_dag.completed truthful without having to know
-- about DAGs.
--
-- THE ROW IS LOCKED BEFORE THE COUNT, and that lock is the whole correctness
-- argument. Without it this is a textbook write skew: two of a DAG's members
-- finish in concurrent READ COMMITTED transactions, each trigger counts the
-- unfinished members against its own snapshot, each sees the OTHER member
-- still running, and NEITHER stamps. Both commit, every member is terminal,
-- and `completed` stays NULL forever -- a DAG that never finishes, in a
-- column an operator reads to decide whether it did. Reproduced by
-- interleaving two finishers.
--
-- The lock serialises the two: the second trigger blocks on `FOR NO KEY
-- UPDATE`, and when the first commits it takes a FRESH snapshot for the count
-- (each statement inside a volatile function does, under READ COMMITTED) and
-- sees the member the first one finished. Exactly one of them stamps.
--
-- FOR NO KEY UPDATE rather than FOR UPDATE: `completed` is not a key column,
-- and the weaker mode does not conflict with the `FOR KEY SHARE` that
-- inserting a job with this dag_id takes through the foreign key. So members
-- finishing never block members being added, and there is no pair of
-- lock modes for the two directions to deadlock on.
--
-- ...AND IT IS UNSTAMPED AGAIN when a member leaves a terminal state. A DAG
-- whose crashed member is retried has pending work again, so a `completed`
-- timestamp on it is not stale, it is WRONG -- `jorb_dag_status` would report
-- the same DAG as completed with pending_jobs > 0, and no later event
-- corrects it (the retried member's own completion re-enters the branch
-- above, which only stamps when nothing is pending). The trigger's WHEN
-- clause therefore covers the edge in both directions, and the function
-- decides which one it is from the count rather than from the state it was
-- handed.
--
-- Still UPDATE-only: a job INSERTED into a DAG cannot reach this, and that is
-- deliberate. DAG construction inserts every member before any of them can
-- finish, so there is nothing to unstamp; and `jorb` INSERT is the hottest
-- write in the system, which is not a path to hang a per-row DAG probe on for
-- a case that does not arise.
CREATE FUNCTION complete_jorb_dag() RETURNS trigger AS $$
DECLARE
    pending BIGINT;
BEGIN
    IF NEW.dag_id IS NULL THEN
        RETURN NEW;
    END IF;

    -- Serialise against every other member of THIS DAG changing at the same
    -- instant; see the argument above. Taken before the count, never after.
    PERFORM 1 FROM jorb_dag WHERE id = NEW.dag_id FOR NO KEY UPDATE;

    SELECT count(*) INTO pending
      FROM jorb j
     WHERE j.dag_id = NEW.dag_id
       AND j.state NOT IN ('finished', 'crashed', 'cancelled');

    IF pending = 0 THEN
        UPDATE jorb_dag SET completed = now()
         WHERE id = NEW.dag_id AND completed IS NULL;
    ELSE
        UPDATE jorb_dag SET completed = NULL
         WHERE id = NEW.dag_id AND completed IS NOT NULL;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

COMMENT ON FUNCTION complete_jorb_dag IS 'Keeps jorb_dag.completed truthful in both directions: stamped when the last member reaches a terminal state, cleared when a member leaves one. Locks the DAG row before counting, which is what stops two concurrent last-finishers each deciding the other is still running.';

CREATE TRIGGER jorb_dag_complete
    AFTER UPDATE OF state ON jorb
    FOR EACH ROW
    WHEN (NEW.dag_id IS NOT NULL
          AND OLD.state IS DISTINCT FROM NEW.state
          AND (NEW.state IN ('finished', 'crashed', 'cancelled')
               OR OLD.state IN ('finished', 'crashed', 'cancelled')))
    EXECUTE FUNCTION complete_jorb_dag();
