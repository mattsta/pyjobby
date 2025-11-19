-- Migration 008: Add DAG (Directed Acyclic Graph) Support
--
-- Purpose: Enable complex job dependency graphs where jobs can depend on
--          multiple upstream jobs, enabling parallel execution with
--          synchronization points.
--
-- Features:
-- - jorb_dag table for tracking DAG workflows
-- - dag_id column in jorb for DAG membership
-- - jorb_dependencies table for explicit dependencies (optional)
-- - Views and functions for DAG monitoring

BEGIN;

-- Create DAG tracking table
CREATE TABLE IF NOT EXISTS jorb_dag (
    id BIGSERIAL PRIMARY KEY,
    name TEXT,  -- Optional DAG name for debugging/monitoring
    created TIMESTAMPTZ DEFAULT NOW(),
    completed TIMESTAMPTZ,
    metadata JSONB DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS jorb_dag_name_idx ON jorb_dag(name) WHERE name IS NOT NULL;
CREATE INDEX IF NOT EXISTS jorb_dag_created_idx ON jorb_dag(created);

COMMENT ON TABLE jorb_dag IS
    'DAG (Directed Acyclic Graph) workflow tracking. '
    'Groups related jobs into a single workflow for monitoring.';

COMMENT ON COLUMN jorb_dag.name IS 'Optional human-readable DAG name';
COMMENT ON COLUMN jorb_dag.metadata IS 'DAG metadata (total_nodes, description, etc.)';

-- Add DAG membership to jobs
ALTER TABLE jorb ADD COLUMN IF NOT EXISTS dag_id BIGINT REFERENCES jorb_dag(id) ON DELETE CASCADE;

CREATE INDEX IF NOT EXISTS jorb_dag_id_idx ON jorb(dag_id) WHERE dag_id IS NOT NULL;

COMMENT ON COLUMN jorb.dag_id IS
    'DAG workflow ID this job belongs to (NULL = not part of a DAG)';

-- Create explicit dependencies table (optional - alternative to waitfor_job/waitfor_group)
-- This provides more flexible dependency tracking for complex DAGs
CREATE TABLE IF NOT EXISTS jorb_dependencies (
    job_id BIGINT REFERENCES jorb(id) ON DELETE CASCADE,
    depends_on_job_id BIGINT REFERENCES jorb(id) ON DELETE CASCADE,
    PRIMARY KEY (job_id, depends_on_job_id),
    CHECK (job_id != depends_on_job_id)  -- Prevent self-dependency
);

CREATE INDEX IF NOT EXISTS jorb_dependencies_job_idx ON jorb_dependencies(job_id);
CREATE INDEX IF NOT EXISTS jorb_dependencies_depends_idx ON jorb_dependencies(depends_on_job_id);

COMMENT ON TABLE jorb_dependencies IS
    'Explicit job dependencies for DAG support. '
    'Optional - can also use waitfor_job/waitfor_group columns.';

-- View for DAG status monitoring
CREATE OR REPLACE VIEW jorb_dag_status AS
SELECT
    d.id as dag_id,
    d.name as dag_name,
    d.created,
    d.completed,
    COUNT(*) as total_jobs,
    COUNT(*) FILTER (WHERE j.state = 'finished') as finished_jobs,
    COUNT(*) FILTER (WHERE j.state = 'running') as running_jobs,
    COUNT(*) FILTER (WHERE j.state = 'queued') as queued_jobs,
    COUNT(*) FILTER (WHERE j.state = 'crashed') as crashed_jobs,
    COUNT(*) FILTER (WHERE j.state = 'cancelled') as cancelled_jobs,
    CASE
        WHEN COUNT(*) FILTER (WHERE j.state = 'crashed') > 0
            THEN 'failed'
        WHEN COUNT(*) FILTER (WHERE j.state NOT IN ('finished', 'crashed', 'cancelled')) = 0
            THEN 'complete'
        WHEN COUNT(*) FILTER (WHERE j.state = 'running') > 0
            THEN 'running'
        ELSE 'queued'
    END as dag_state,
    ROUND(
        100.0 * COUNT(*) FILTER (WHERE j.state = 'finished') / NULLIF(COUNT(*), 0),
        2
    ) as completion_percentage
FROM jorb_dag d
LEFT JOIN jorb j ON j.dag_id = d.id
GROUP BY d.id, d.name, d.created, d.completed;

COMMENT ON VIEW jorb_dag_status IS
    'DAG execution status summary. '
    'Shows job counts by state and overall DAG state.';

-- View for DAG execution timeline
CREATE OR REPLACE VIEW jorb_dag_timeline AS
SELECT
    d.id as dag_id,
    d.name as dag_name,
    j.id as job_id,
    j.job_class,
    j.state,
    j.created as job_created,
    j.started as job_started,
    j.finished as job_finished,
    EXTRACT(EPOCH FROM (j.finished - j.started)) as duration_seconds,
    j.waitfor_job,
    j.waitfor_group,
    j.run_group
FROM jorb_dag d
JOIN jorb j ON j.dag_id = d.id
ORDER BY d.id, j.created;

COMMENT ON VIEW jorb_dag_timeline IS
    'Detailed timeline of job execution within DAGs. '
    'Useful for performance analysis and debugging.';

-- Function to get DAG dependency graph
CREATE OR REPLACE FUNCTION get_dag_dependencies(dag_id_param BIGINT)
RETURNS TABLE(
    job_id BIGINT,
    job_class TEXT,
    depends_on BIGINT[]
) AS $$
BEGIN
    RETURN QUERY
    WITH job_deps AS (
        -- Get dependencies from jorb_dependencies table
        SELECT
            jd.job_id,
            array_agg(jd.depends_on_job_id ORDER BY jd.depends_on_job_id) as deps
        FROM jorb_dependencies jd
        JOIN jorb j ON j.id = jd.job_id
        WHERE j.dag_id = dag_id_param
        GROUP BY jd.job_id

        UNION ALL

        -- Get dependencies from waitfor_job
        SELECT
            j.id as job_id,
            ARRAY[j.waitfor_job] as deps
        FROM jorb j
        WHERE j.dag_id = dag_id_param
          AND j.waitfor_job IS NOT NULL
    )
    SELECT
        j.id,
        j.job_class,
        COALESCE(jd.deps, ARRAY[]::BIGINT[])
    FROM jorb j
    LEFT JOIN job_deps jd ON jd.job_id = j.id
    WHERE j.dag_id = dag_id_param
    ORDER BY j.id;
END;
$$ LANGUAGE plpgsql;

COMMENT ON FUNCTION get_dag_dependencies IS
    'Get dependency graph for a DAG. '
    'Combines jorb_dependencies table and waitfor_job/waitfor_group columns.';

-- Function to validate DAG (check for cycles)
CREATE OR REPLACE FUNCTION validate_dag_acyclic(dag_id_param BIGINT)
RETURNS BOOLEAN AS $$
DECLARE
    has_cycle BOOLEAN;
BEGIN
    -- Build all edges (from both sources)
    WITH RECURSIVE all_edges AS (
        -- Edges from jorb_dependencies
        SELECT job_id as from_job, depends_on_job_id as to_job
        FROM jorb_dependencies jd
        WHERE EXISTS (SELECT 1 FROM jorb j WHERE j.id = jd.job_id AND j.dag_id = dag_id_param)

        UNION ALL

        -- Edges from waitfor_job
        SELECT id as from_job, waitfor_job as to_job
        FROM jorb
        WHERE dag_id = dag_id_param AND waitfor_job IS NOT NULL
    ),
    -- Traverse from each node to build paths (without following cycles)
    dag_traverse AS (
        -- Start from each node in the DAG
        SELECT id as start_node, id as current_node, ARRAY[]::BIGINT[] as path, 0 as depth
        FROM jorb
        WHERE dag_id = dag_id_param

        UNION ALL

        -- Follow outgoing edges (don't revisit current node)
        SELECT dt.start_node, e.to_job, dt.path || dt.current_node, dt.depth + 1
        FROM dag_traverse dt
        JOIN all_edges e ON e.from_job = dt.current_node
        WHERE dt.depth < 100  -- Prevent infinite recursion
          AND NOT (dt.current_node = ANY(dt.path))  -- Don't revisit current node
    )
    -- A cycle exists if any edge points to a node already in the path
    SELECT EXISTS(
        SELECT 1 FROM dag_traverse dt
        JOIN all_edges e ON e.from_job = dt.current_node
        WHERE e.to_job = ANY(dt.path || dt.current_node)
    ) INTO has_cycle;

    RETURN NOT has_cycle;
END;
$$ LANGUAGE plpgsql;

COMMENT ON FUNCTION validate_dag_acyclic IS
    'Validate that a DAG has no cycles (is acyclic). '
    'Returns true if valid, false if cycle detected.';

-- Function to auto-complete DAG when all jobs finish
CREATE OR REPLACE FUNCTION auto_complete_dag()
RETURNS TRIGGER AS $$
BEGIN
    -- If this job is part of a DAG and it just finished
    IF NEW.dag_id IS NOT NULL AND NEW.state IN ('finished', 'crashed', 'cancelled')
        AND (OLD.state IS NULL OR OLD.state NOT IN ('finished', 'crashed', 'cancelled'))
    THEN
        -- Check if all jobs in the DAG are complete
        UPDATE jorb_dag
        SET completed = NOW()
        WHERE id = NEW.dag_id
          AND completed IS NULL
          AND NOT EXISTS (
              SELECT 1 FROM jorb
              WHERE dag_id = NEW.dag_id
                AND state NOT IN ('finished', 'crashed', 'cancelled')
          );
    END IF;

    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- Create trigger to auto-complete DAGs
DROP TRIGGER IF EXISTS auto_complete_dag_trigger ON jorb;
CREATE TRIGGER auto_complete_dag_trigger
    AFTER UPDATE OF state ON jorb
    FOR EACH ROW
    EXECUTE FUNCTION auto_complete_dag();

COMMENT ON FUNCTION auto_complete_dag IS
    'Automatically mark DAG as completed when all jobs finish. '
    'Triggered on job state changes.';

COMMIT;
