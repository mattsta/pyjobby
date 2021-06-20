--
-- PostgreSQL database dump
--

-- Dumped from database version 13.1
-- Dumped by pg_dump version 13.1

SET statement_timeout = 0;
SET lock_timeout = 0;
SET idle_in_transaction_session_timeout = 0;
SET client_encoding = 'UTF8';
SET standard_conforming_strings = on;
SELECT pg_catalog.set_config('search_path', '', false);
SET check_function_bodies = false;
SET xmloption = content;
SET client_min_messages = warning;
SET row_security = off;

SET default_tablespace = '';

SET default_table_access_method = heap;

--
-- Name: jorbstate; Type: TYPE; Schema: public; Owner: kudzu
--

CREATE TYPE public.jorbstate AS ENUM (
    'queued',
    'claimed',
    'running',
    'heartbeat',
    'crashed',
    'finished',
    'waiting'
);


-- ALTER TYPE public.jorbstate OWNER TO kudzu;

--
-- Name: jorb; Type: TABLE; Schema: public; Owner: kudzu
--

CREATE TABLE public.jorb (
    queue text DEFAULT 'default'::text NOT NULL,
    capability text,
    prio integer DEFAULT 100 NOT NULL,
    run_after timestamp without time zone DEFAULT timezone('utc'::text, CURRENT_TIMESTAMP) NOT NULL,
    id bigint NOT NULL,
    deadline_key text,
    state public.jorbstate DEFAULT 'queued'::public.jorbstate NOT NULL,
    run_count integer DEFAULT 0 NOT NULL,
    job_class text NOT NULL,
    kwargs json NOT NULL,
    uid integer,
    run_group bigint,
    admin_data json,
    result json,
    error_message text,
    error_backtrace text,
    error_count integer DEFAULT 0 NOT NULL,
    worker_pid integer,
    worker_host text,
    created timestamp without time zone DEFAULT timezone('utc'::text, CURRENT_TIMESTAMP) NOT NULL,
    updated timestamp without time zone DEFAULT timezone('utc'::text, CURRENT_TIMESTAMP) NOT NULL,
    waitfor_group bigint,
    waitfor_job bigint
);


ALTER TABLE public.jorb OWNER TO kudzu;

--
-- Name: COLUMN jorb.queue; Type: COMMENT; Schema: public; Owner: kudzu
--

COMMENT ON COLUMN public.jorb.queue IS 'Specific queue runner';


--
-- Name: COLUMN jorb.capability; Type: COMMENT; Schema: public; Owner: kudzu
--

COMMENT ON COLUMN public.jorb.capability IS 'Job must run in an environment with this capability (or any host if not set)';


--
-- Name: COLUMN jorb.prio; Type: COMMENT; Schema: public; Owner: kudzu
--

COMMENT ON COLUMN public.jorb.prio IS 'Lower number means higher priority';


--
-- Name: COLUMN jorb.run_after; Type: COMMENT; Schema: public; Owner: kudzu
--

COMMENT ON COLUMN public.jorb.run_after IS 'Minimum start time for job';


--
-- Name: COLUMN jorb.id; Type: COMMENT; Schema: public; Owner: kudzu
--

COMMENT ON COLUMN public.jorb.id IS 'Job (jorb) id';


--
-- Name: COLUMN jorb.deadline_key; Type: COMMENT; Schema: public; Owner: kudzu
--

COMMENT ON COLUMN public.jorb.deadline_key IS 'prevents duplicates when scheduling future single-instance jobs';


--
-- Name: COLUMN jorb.run_count; Type: COMMENT; Schema: public; Owner: kudzu
--

COMMENT ON COLUMN public.jorb.run_count IS 'number of times this job has been dequeued to run';


--
-- Name: COLUMN jorb.job_class; Type: COMMENT; Schema: public; Owner: kudzu
--

COMMENT ON COLUMN public.jorb.job_class IS 'Class to launch on worker (must exist on worker already)';


--
-- Name: COLUMN jorb.kwargs; Type: COMMENT; Schema: public; Owner: kudzu
--

COMMENT ON COLUMN public.jorb.kwargs IS 'dict of kwargs for job class constructor';


--
-- Name: COLUMN jorb.uid; Type: COMMENT; Schema: public; Owner: kudzu
--

COMMENT ON COLUMN public.jorb.uid IS 'if job is for user...';


--
-- Name: COLUMN jorb.run_group; Type: COMMENT; Schema: public; Owner: kudzu
--

COMMENT ON COLUMN public.jorb.run_group IS 'random shared ID if job is populated with other jobs (e.g. batch image upload processing)';


--
-- Name: COLUMN jorb.admin_data; Type: COMMENT; Schema: public; Owner: kudzu
--

COMMENT ON COLUMN public.jorb.admin_data IS 'tags, other admin details';


--
-- Name: COLUMN jorb.result; Type: COMMENT; Schema: public; Owner: kudzu
--

COMMENT ON COLUMN public.jorb.result IS 'Any non-NULL result returned by the evaluation function';


--
-- Name: COLUMN jorb.error_message; Type: COMMENT; Schema: public; Owner: kudzu
--

COMMENT ON COLUMN public.jorb.error_message IS 'Most recent error message itself';


--
-- Name: COLUMN jorb.error_backtrace; Type: COMMENT; Schema: public; Owner: kudzu
--

COMMENT ON COLUMN public.jorb.error_backtrace IS 'Most recent full error backtrace';


--
-- Name: COLUMN jorb.error_count; Type: COMMENT; Schema: public; Owner: kudzu
--

COMMENT ON COLUMN public.jorb.error_count IS 'Number of times the job has failed';


--
-- Name: COLUMN jorb.worker_pid; Type: COMMENT; Schema: public; Owner: kudzu
--

COMMENT ON COLUMN public.jorb.worker_pid IS 'worker node/process last evaluated node';


--
-- Name: COLUMN jorb.worker_host; Type: COMMENT; Schema: public; Owner: kudzu
--

COMMENT ON COLUMN public.jorb.worker_host IS 'hostname of worker...';


--
-- Name: COLUMN jorb.waitfor_group; Type: COMMENT; Schema: public; Owner: kudzu
--

COMMENT ON COLUMN public.jorb.waitfor_group IS 'Move this job from ''waiting'' to ''queued'' ONLY when ALL jobs with this ''run_group'' are reported ''finished''';


--
-- Name: COLUMN jorb.waitfor_job; Type: COMMENT; Schema: public; Owner: kudzu
--

COMMENT ON COLUMN public.jorb.waitfor_job IS 'Move this job from ''waiting'' to ''queued'' ONLY when job with this ''id'' are reported ''finished''';


--
-- Name: jorb_id_seq; Type: SEQUENCE; Schema: public; Owner: kudzu
--

ALTER TABLE public.jorb ALTER COLUMN id ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME public.jorb_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: jorb jorb_pkey; Type: CONSTRAINT; Schema: public; Owner: kudzu
--

ALTER TABLE ONLY public.jorb
    ADD CONSTRAINT jorb_pkey PRIMARY KEY (id);


--
-- Name: ix_jorb_uid; Type: INDEX; Schema: public; Owner: kudzu
--

CREATE INDEX ix_jorb_uid ON public.jorb USING btree (uid);


--
-- Name: jorb_deadline_noconflict_idx; Type: INDEX; Schema: public; Owner: kudzu
--

CREATE UNIQUE INDEX jorb_deadline_noconflict_idx ON public.jorb USING btree (deadline_key, queue) WHERE ((state = 'queued'::public.jorbstate) AND (deadline_key IS NOT NULL));


--
-- Name: jorb_poll_idx; Type: INDEX; Schema: public; Owner: kudzu
--

CREATE INDEX jorb_poll_idx ON public.jorb USING btree (queue, capability, prio, run_after) WHERE (state = 'queued'::public.jorbstate);


--
-- Name: jorb_run_group_idx; Type: INDEX; Schema: public; Owner: kudzu
--

CREATE INDEX jorb_run_group_idx ON public.jorb USING btree (run_group) WHERE (run_group IS NOT NULL);


--
-- Name: jorb_waitfor_group_idx; Type: INDEX; Schema: public; Owner: kudzu
--

CREATE INDEX jorb_waitfor_group_idx ON public.jorb USING btree (waitfor_group) WHERE ((waitfor_group IS NOT NULL) AND (state = 'waiting'::public.jorbstate));


--
-- Name: jorb_waitfor_job_idx; Type: INDEX; Schema: public; Owner: kudzu
--

CREATE INDEX jorb_waitfor_job_idx ON public.jorb USING btree (waitfor_job) WHERE ((waitfor_job IS NOT NULL) AND (state = 'waiting'::public.jorbstate));


--
-- Name: jorb jorb_waitfor_job_fkey; Type: FK CONSTRAINT; Schema: public; Owner: kudzu
--

ALTER TABLE ONLY public.jorb
    ADD CONSTRAINT jorb_waitfor_job_fkey FOREIGN KEY (waitfor_job) REFERENCES public.jorb(id);


--
-- PostgreSQL database dump complete
--
