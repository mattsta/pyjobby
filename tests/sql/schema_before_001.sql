-- ============================================================================
-- FROZEN HISTORICAL SCHEMA -- the shape pyjobby installed BEFORE migration 001
-- ============================================================================
-- This is not a second copy of pyjobby/sql/schema.sql to keep in step with it.
-- It is a photograph of a schema that was really deployed (dumped from a
-- database installed by an older release), and it must NEVER be edited again:
-- its whole purpose is to be the "before" side of the upgrade test in
-- tests/test_migrations.py, which installs it, runs the shipped migrations,
-- and then requires the result to be catalog-identical to a fresh install of
-- the current schema.sql. Editing it to match a later schema would silently
-- delete the only coverage the upgrade path has.
--
-- A future cycle that adds migration 002 does NOT add a new file here: this
-- one is still the oldest supported shape, and running 001 + 002 over it is
-- exactly the upgrade a real operator on that release performs.
-- ============================================================================
CREATE TYPE public.jorbstate AS ENUM (
    'queued',
    'claimed',
    'running',
    'waiting',
    'finished',
    'crashed',
    'cancelled'
);
CREATE FUNCTION public.complete_jorb_dag() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
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
$$;
CREATE FUNCTION public.notify_job_state_change() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
    PERFORM pg_notify('job_state_change', json_build_object(
        'id', NEW.id,
        'queue', NEW.queue,
        'job_class', NEW.job_class,
        'old_state', OLD.state,
        'new_state', NEW.state,
        'error_count', NEW.error_count
    )::text);
    RETURN NEW;
END;
$$;
CREATE FUNCTION public.notify_jorb_cancel() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
    PERFORM pg_notify('jorb_cancel', NEW.id::text);
    RETURN NEW;
END;
$$;
CREATE FUNCTION public.notify_jorb_done() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
    PERFORM pg_notify('jorb_done',
        json_build_object('id', NEW.id, 'state', NEW.state)::text);
    RETURN NEW;
END;
$$;
CREATE FUNCTION public.notify_jorb_enqueued() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
    PERFORM pg_notify('jorb_enqueued', NEW.queue);
    RETURN NEW;
END;
$$;
CREATE FUNCTION public.notify_jorb_event() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
    PERFORM pg_notify('jorb_event',
        json_build_object('job_id', NEW.job_id, 'key', NEW.key)::text);
    RETURN NEW;
END;
$$;
CREATE FUNCTION public.notify_jorb_mailbox() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
    PERFORM pg_notify('jorb_mailbox',
        json_build_object('dest', NEW.dest_job_id, 'topic', NEW.topic)::text);
    RETURN NEW;
END;
$$;
CREATE FUNCTION public.notify_schedule_executed() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
    PERFORM pg_notify('schedule_executed', json_build_object(
        'schedule_id', NEW.schedule_id,
        'schedule_name', NEW.schedule_name,
        'result', NEW.result,
        'job_id', NEW.job_id
    )::text);
    RETURN NEW;
END;
$$;
CREATE FUNCTION public.record_jorb_history() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
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
$$;
CREATE TABLE public.jorb (
    id bigint NOT NULL,
    queue text DEFAULT 'default'::text NOT NULL,
    capability text,
    prio integer DEFAULT 100 NOT NULL,
    state public.jorbstate DEFAULT 'queued'::public.jorbstate NOT NULL,
    job_class text NOT NULL,
    kwargs jsonb DEFAULT '{}'::jsonb NOT NULL,
    admin_data jsonb DEFAULT '{}'::jsonb NOT NULL,
    result jsonb,
    uid bigint,
    run_group bigint,
    waitfor_group bigint,
    waitfor_job bigint,
    dag_id bigint,
    deadline_key text,
    run_count integer DEFAULT 0 NOT NULL,
    error_count integer DEFAULT 0 NOT NULL,
    error_message text,
    error_backtrace text,
    run_epoch integer DEFAULT 0 NOT NULL,
    cancel_requested boolean DEFAULT false NOT NULL,
    claimed_by bigint,
    worker_pid integer,
    worker_host text,
    created timestamp with time zone DEFAULT now() NOT NULL,
    updated timestamp with time zone DEFAULT now() NOT NULL,
    run_after timestamp with time zone DEFAULT now() NOT NULL,
    started timestamp with time zone,
    finished timestamp with time zone,
    timeout_at timestamp with time zone
);
CREATE TABLE public.jorb_dag (
    id bigint NOT NULL,
    name text,
    created timestamp with time zone DEFAULT now() NOT NULL,
    completed timestamp with time zone,
    metadata jsonb DEFAULT '{}'::jsonb NOT NULL
);
ALTER TABLE public.jorb_dag ALTER COLUMN id ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME public.jorb_dag_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);
CREATE VIEW public.jorb_dag_status AS
SELECT
    NULL::bigint AS dag_id,
    NULL::text AS name,
    NULL::timestamp with time zone AS created,
    NULL::timestamp with time zone AS completed,
    NULL::bigint AS total_jobs,
    NULL::bigint AS finished_jobs,
    NULL::bigint AS crashed_jobs,
    NULL::bigint AS cancelled_jobs,
    NULL::bigint AS pending_jobs;
CREATE VIEW public.jorb_dag_timeline AS
 SELECT dag_id,
    id AS job_id,
    job_class,
    state,
    started,
    finished,
    EXTRACT(epoch FROM (finished - started)) AS duration_seconds
   FROM public.jorb j
  WHERE (dag_id IS NOT NULL);
CREATE TABLE public.jorb_dependencies (
    job_id bigint NOT NULL,
    depends_on bigint NOT NULL
);
CREATE TABLE public.jorb_event (
    job_id bigint NOT NULL,
    key text NOT NULL,
    value jsonb NOT NULL,
    updated timestamp with time zone DEFAULT now() NOT NULL
);
CREATE TABLE public.jorb_history (
    id bigint NOT NULL,
    job_id bigint NOT NULL,
    at timestamp with time zone DEFAULT now() NOT NULL,
    event text NOT NULL,
    detail jsonb DEFAULT '{}'::jsonb NOT NULL
);
ALTER TABLE public.jorb_history ALTER COLUMN id ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME public.jorb_history_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);
ALTER TABLE public.jorb ALTER COLUMN id ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME public.jorb_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);
CREATE TABLE public.jorb_mailbox (
    id bigint NOT NULL,
    dest_job_id bigint NOT NULL,
    topic text,
    message jsonb NOT NULL,
    created timestamp with time zone DEFAULT now() NOT NULL,
    consumed_at timestamp with time zone
);
ALTER TABLE public.jorb_mailbox ALTER COLUMN id ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME public.jorb_mailbox_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);
CREATE TABLE public.jorb_queue (
    name text NOT NULL,
    paused boolean DEFAULT false NOT NULL,
    max_concurrency integer,
    rate_limit integer,
    rate_period_seconds double precision DEFAULT 60 NOT NULL,
    created timestamp with time zone DEFAULT now() NOT NULL,
    updated timestamp with time zone DEFAULT now() NOT NULL
);
CREATE TABLE public.jorb_schedule (
    id bigint NOT NULL,
    name text NOT NULL,
    description text,
    job_class text NOT NULL,
    kwargs jsonb DEFAULT '{}'::jsonb NOT NULL,
    queue text DEFAULT 'default'::text NOT NULL,
    prio integer DEFAULT 100 NOT NULL,
    capability text,
    cron_expr text NOT NULL,
    timezone text DEFAULT 'UTC'::text NOT NULL,
    enabled boolean DEFAULT true NOT NULL,
    max_concurrent_jobs integer DEFAULT 1 NOT NULL,
    jitter_seconds integer DEFAULT 0 NOT NULL,
    backpressure_threshold integer DEFAULT 1000,
    circuit_breaker_threshold integer DEFAULT 5 NOT NULL,
    consecutive_failures integer DEFAULT 0 NOT NULL,
    next_run timestamp with time zone NOT NULL,
    last_run timestamp with time zone,
    last_success timestamp with time zone,
    last_failure timestamp with time zone,
    run_count bigint DEFAULT 0 NOT NULL,
    success_count bigint DEFAULT 0 NOT NULL,
    failure_count bigint DEFAULT 0 NOT NULL,
    skip_count bigint DEFAULT 0 NOT NULL,
    created timestamp with time zone DEFAULT now() NOT NULL,
    updated timestamp with time zone DEFAULT now() NOT NULL,
    created_by text
);
ALTER TABLE public.jorb_schedule ALTER COLUMN id ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME public.jorb_schedule_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);
CREATE TABLE public.jorb_schedule_log (
    id bigint NOT NULL,
    schedule_id bigint NOT NULL,
    schedule_name text NOT NULL,
    scheduled_time timestamp with time zone NOT NULL,
    actual_time timestamp with time zone DEFAULT now() NOT NULL,
    result text NOT NULL,
    skip_reason text,
    job_id bigint,
    error_message text,
    duration_ms integer,
    queue_depth_at_run integer,
    concurrent_jobs_at_run integer,
    jitter_applied_seconds integer
);
ALTER TABLE public.jorb_schedule_log ALTER COLUMN id ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME public.jorb_schedule_log_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);
CREATE TABLE public.jorb_step (
    job_id bigint NOT NULL,
    step_seq integer NOT NULL,
    name text NOT NULL,
    output jsonb,
    error text,
    run_epoch integer NOT NULL,
    started timestamp with time zone DEFAULT now() NOT NULL,
    finished timestamp with time zone
);
CREATE TABLE public.jorb_worker (
    id bigint NOT NULL,
    host text NOT NULL,
    pid integer NOT NULL,
    queue text NOT NULL,
    capabilities text[] DEFAULT '{}'::text[] NOT NULL,
    version text,
    started timestamp with time zone DEFAULT now() NOT NULL,
    last_seen timestamp with time zone DEFAULT now() NOT NULL,
    shutdown_at timestamp with time zone
);
ALTER TABLE public.jorb_worker ALTER COLUMN id ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME public.jorb_worker_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);
ALTER TABLE ONLY public.jorb_dag
    ADD CONSTRAINT jorb_dag_pkey PRIMARY KEY (id);
ALTER TABLE ONLY public.jorb_dependencies
    ADD CONSTRAINT jorb_dependencies_pkey PRIMARY KEY (job_id, depends_on);
ALTER TABLE ONLY public.jorb_event
    ADD CONSTRAINT jorb_event_pkey PRIMARY KEY (job_id, key);
ALTER TABLE ONLY public.jorb_history
    ADD CONSTRAINT jorb_history_pkey PRIMARY KEY (id);
ALTER TABLE ONLY public.jorb_mailbox
    ADD CONSTRAINT jorb_mailbox_pkey PRIMARY KEY (id);
ALTER TABLE ONLY public.jorb
    ADD CONSTRAINT jorb_pkey PRIMARY KEY (id);
ALTER TABLE ONLY public.jorb_queue
    ADD CONSTRAINT jorb_queue_pkey PRIMARY KEY (name);
ALTER TABLE ONLY public.jorb_schedule_log
    ADD CONSTRAINT jorb_schedule_log_pkey PRIMARY KEY (id);
ALTER TABLE ONLY public.jorb_schedule
    ADD CONSTRAINT jorb_schedule_name_key UNIQUE (name);
ALTER TABLE ONLY public.jorb_schedule
    ADD CONSTRAINT jorb_schedule_pkey PRIMARY KEY (id);
ALTER TABLE ONLY public.jorb_step
    ADD CONSTRAINT jorb_step_pkey PRIMARY KEY (job_id, step_seq);
ALTER TABLE ONLY public.jorb_worker
    ADD CONSTRAINT jorb_worker_pkey PRIMARY KEY (id);
CREATE INDEX jorb_claim_idx ON public.jorb USING btree (queue, prio, run_after) WHERE (state = 'queued'::public.jorbstate);
CREATE INDEX jorb_dag_idx ON public.jorb USING btree (dag_id) WHERE (dag_id IS NOT NULL);
CREATE UNIQUE INDEX jorb_deadline_idx ON public.jorb USING btree (deadline_key, queue) WHERE ((state = 'queued'::public.jorbstate) AND (deadline_key IS NOT NULL));
CREATE INDEX jorb_history_job_idx ON public.jorb_history USING btree (job_id, id);
CREATE INDEX jorb_inflight_idx ON public.jorb USING btree (state, updated) WHERE (state = ANY (ARRAY['claimed'::public.jorbstate, 'running'::public.jorbstate]));
CREATE INDEX jorb_mailbox_pending_idx ON public.jorb_mailbox USING btree (dest_job_id, topic, id) WHERE (consumed_at IS NULL);
CREATE INDEX jorb_run_group_idx ON public.jorb USING btree (run_group);
CREATE INDEX jorb_schedule_due_idx ON public.jorb_schedule USING btree (next_run) WHERE enabled;
CREATE INDEX jorb_schedule_log_idx ON public.jorb_schedule_log USING btree (schedule_id, id);
CREATE INDEX jorb_started_idx ON public.jorb USING btree (queue, started) WHERE (started IS NOT NULL);
CREATE INDEX jorb_timeout_idx ON public.jorb USING btree (timeout_at) WHERE ((state = 'running'::public.jorbstate) AND (timeout_at IS NOT NULL));
CREATE INDEX jorb_uid_idx ON public.jorb USING btree (uid);
CREATE INDEX jorb_waitfor_group_idx ON public.jorb USING btree (waitfor_group) WHERE (state = 'waiting'::public.jorbstate);
CREATE INDEX jorb_waitfor_job_idx ON public.jorb USING btree (waitfor_job) WHERE (state = 'waiting'::public.jorbstate);
CREATE INDEX jorb_worker_live_idx ON public.jorb_worker USING btree (last_seen) WHERE (shutdown_at IS NULL);
CREATE OR REPLACE VIEW public.jorb_dag_status AS
 SELECT d.id AS dag_id,
    d.name,
    d.created,
    d.completed,
    count(j.id) AS total_jobs,
    count(j.id) FILTER (WHERE (j.state = 'finished'::public.jorbstate)) AS finished_jobs,
    count(j.id) FILTER (WHERE (j.state = 'crashed'::public.jorbstate)) AS crashed_jobs,
    count(j.id) FILTER (WHERE (j.state = 'cancelled'::public.jorbstate)) AS cancelled_jobs,
    count(j.id) FILTER (WHERE (j.state = ANY (ARRAY['queued'::public.jorbstate, 'claimed'::public.jorbstate, 'running'::public.jorbstate, 'waiting'::public.jorbstate]))) AS pending_jobs
   FROM (public.jorb_dag d
     LEFT JOIN public.jorb j ON ((j.dag_id = d.id)))
  GROUP BY d.id;
CREATE TRIGGER job_state_change_notify AFTER UPDATE OF state ON public.jorb FOR EACH ROW WHEN ((old.state IS DISTINCT FROM new.state)) EXECUTE FUNCTION public.notify_job_state_change();
CREATE TRIGGER jorb_cancel_notify AFTER UPDATE OF cancel_requested ON public.jorb FOR EACH ROW WHEN ((new.cancel_requested AND (new.state = 'running'::public.jorbstate))) EXECUTE FUNCTION public.notify_jorb_cancel();
CREATE TRIGGER jorb_dag_complete AFTER UPDATE OF state ON public.jorb FOR EACH ROW WHEN (((new.state = ANY (ARRAY['finished'::public.jorbstate, 'crashed'::public.jorbstate, 'cancelled'::public.jorbstate])) AND (new.dag_id IS NOT NULL))) EXECUTE FUNCTION public.complete_jorb_dag();
CREATE TRIGGER jorb_done_notify AFTER UPDATE OF state ON public.jorb FOR EACH ROW WHEN ((new.state = ANY (ARRAY['finished'::public.jorbstate, 'crashed'::public.jorbstate, 'cancelled'::public.jorbstate]))) EXECUTE FUNCTION public.notify_jorb_done();
CREATE TRIGGER jorb_enqueued_notify AFTER INSERT OR UPDATE OF state ON public.jorb FOR EACH ROW WHEN ((new.state = 'queued'::public.jorbstate)) EXECUTE FUNCTION public.notify_jorb_enqueued();
CREATE TRIGGER jorb_event_notify AFTER INSERT OR UPDATE ON public.jorb_event FOR EACH ROW EXECUTE FUNCTION public.notify_jorb_event();
CREATE TRIGGER jorb_history_record AFTER INSERT OR UPDATE OF state ON public.jorb FOR EACH ROW EXECUTE FUNCTION public.record_jorb_history();
CREATE TRIGGER jorb_mailbox_notify AFTER INSERT ON public.jorb_mailbox FOR EACH ROW EXECUTE FUNCTION public.notify_jorb_mailbox();
CREATE TRIGGER schedule_executed_notify AFTER INSERT ON public.jorb_schedule_log FOR EACH ROW EXECUTE FUNCTION public.notify_schedule_executed();
ALTER TABLE ONLY public.jorb
    ADD CONSTRAINT jorb_dag_fk FOREIGN KEY (dag_id) REFERENCES public.jorb_dag(id) ON DELETE SET NULL;
ALTER TABLE ONLY public.jorb_dependencies
    ADD CONSTRAINT jorb_dependencies_depends_on_fkey FOREIGN KEY (depends_on) REFERENCES public.jorb(id) ON DELETE CASCADE;
ALTER TABLE ONLY public.jorb_dependencies
    ADD CONSTRAINT jorb_dependencies_job_id_fkey FOREIGN KEY (job_id) REFERENCES public.jorb(id) ON DELETE CASCADE;
ALTER TABLE ONLY public.jorb_event
    ADD CONSTRAINT jorb_event_job_id_fkey FOREIGN KEY (job_id) REFERENCES public.jorb(id) ON DELETE CASCADE;
ALTER TABLE ONLY public.jorb_mailbox
    ADD CONSTRAINT jorb_mailbox_dest_job_id_fkey FOREIGN KEY (dest_job_id) REFERENCES public.jorb(id) ON DELETE CASCADE;
ALTER TABLE ONLY public.jorb_schedule_log
    ADD CONSTRAINT jorb_schedule_log_schedule_id_fkey FOREIGN KEY (schedule_id) REFERENCES public.jorb_schedule(id) ON DELETE CASCADE;
ALTER TABLE ONLY public.jorb_step
    ADD CONSTRAINT jorb_step_job_id_fkey FOREIGN KEY (job_id) REFERENCES public.jorb(id) ON DELETE CASCADE;
