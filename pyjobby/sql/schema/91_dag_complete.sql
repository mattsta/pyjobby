-- ============================================================================
-- DAG completion: stamped when a DAG's last job reaches a terminal state
-- ============================================================================
-- Recorded by trigger (like history) so every writer — worker, monitor,
-- admin API — keeps jorb_dag.completed truthful without having to know
-- about DAGs.
CREATE FUNCTION complete_jorb_dag() RETURNS trigger AS $$
BEGIN
    IF NEW.dag_id IS NOT NULL THEN
        UPDATE jorb_dag d
        SET completed = now()
        WHERE d.id = NEW.dag_id
          AND d.completed IS NULL
          AND NOT EXISTS (
              SELECT 1 FROM jorb j
              WHERE j.dag_id = NEW.dag_id
                AND j.state NOT IN ('finished', 'crashed', 'cancelled')
          );
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER jorb_dag_complete
    AFTER UPDATE OF state ON jorb
    FOR EACH ROW
    WHEN (NEW.state IN ('finished', 'crashed', 'cancelled')
          AND NEW.dag_id IS NOT NULL)
    EXECUTE FUNCTION complete_jorb_dag();

