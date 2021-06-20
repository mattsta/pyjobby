pyjobby: a python+postgres persistent job queue
===============================================

You'd think the world would have enough job servers by now, right? Yet, I couldn't find one to fit my needs of having a persistent job queue with python classes as workers without needing to install tons of other weird things.

So, in January 2021 I wrote `pyjobby` and this is all about it.

Goals
------

- simplicity
    - entire job system is under 1,000 lines in one file: [`pyjobby/pj.py`](pyjobby/pj.py)
    - job workers are any python class inheriting from `pyjobby.pj.Job`
         - includes automatic logging of failures and backoff retries
- modernness
    - python 3.9 minimum
    - passes mypy strict
- outsourced persistence
    - use standard postgres for durable job storage instead of jank custom non-durable weird memory queue servers
    - use standard postgres to record job state transitions as jobs advance through the work queue (including logging of full exception stack traces to DB for future debugging)
    - fully distributed job servers can be run from the same postgres database for moderate scalability needs
- logical extensibility
    - jobs each have a userid field for tracking which jobs belong to which customers
    - full multiprocessing by default (and worker jobs can further spawn threadpools/workers on their own)
    - custom priority levels so more important jobs can jump the queue
    - custom capability pinning so jobs can run on workers with specific resources
    - jobs can be queued to wait on another job to complete before running (column: `waitfor_job`, query: `enqueue-next-self-finished`)
    - jobs can be queued to wait for *multiple* other jobs to *all* complete before running (column: `waitfor_group`, query: `enqueue-next-if-peer-group-is-finished`)
    - jobs can have specific minimum start times (useful for cron-like applications, but used internally for automatic backoff retries if job errors out)
    - jobs can have a "future singleton deadline" where only one future job for one task is allowed to be queued at a time (example: every time user uploads a file, schedule an update of their billing details at midnight, but multiple uploads would still only trigger one billing detail update (which itself would query the actual user usage when it runs)) (column: `deadline_key`, enforced by partial unique index)
    - optionally listens for web connections to run worker job classes via web request directly with no queue entry required

Terminology:

- **job system (concept)** - the collection of ideas behind creating jobs and running jobs
- **job system (runtime)** - the `pj` script which spawns `N` workers to select **job (storage)** and run them as **job (runtime)**
- **job (storage)** - one database row describing a python class to run with potential restrictions (priority, minimum start time, needs worker with specific capabilities, only run after other jobs complete)
- **job (runtime)** - when a **job (storage)** is selected to run and becomes active on a worker
- **worker** - one process responsible for taking jobs from the database, updating state transitions for **job (storage)** entries, and running them as **job (runtime)**
- **queue** - a column index in the jorb table for selecting subsets of **job (storage)** to run on a worker
- **capability** - a string value set on a **job (storage)** row also needing to match exactly one of the capability strings provided by `pj` on launch. by default, each worker advertises its own hostname as capability `f"hostname:{platform.node()}"`
- **run_after** - a minimum start time for the job to run
- **priority** - numeric values allowing **job (storage)** added later in a queue to run before other previously queued jobs. lower numbers are higher selection priority
- **deadline key** - a unique constraint on `(deadline_key, state==queued)` per queue. **deadline key** is a string guaranteed to be unique for all **job (storage)** rows in a queue where the job state is only **queued**. allows you to request the same job multiple times in a queue, but the server will only schedule one instance of the job per queue. after a deadline key job runs (i.e. state becomes anything other than **queued**), only then can a redundant **deadline key** be added
- **run_group** - multiple tasks may be assigned the same `run_group` value if you would like to run other jobs only when *all* jobs in a group move to a *finished* state
- **waitfor_group** - jobs in *waiting* state with a `waitfor_group` value will run **only** when *all* job rows with the matching `run_group` have moved to a *finished* state
- **waitfor_job** - same as **waitfor_group** except only waits on a specific `id` to become *finished* before running


Current Limitations:

- no web console for viewing state/health of the entire job system, canceling jobs, custom adding jobs, etc.
- python / postgres / web / command line only
- no client library, so you need to manipulate DB tables yourself
- every job state change hits the DB as a committed update (WAL pollution, extra vacuums needed for high volume servers, etc)
- currently we aren't reclaiming jobs a worker started but didn't complete due to the worker exiting/crashing (fix is simple: just query for our own running jobs on startup, which means they got abandoned before the server exited cleanly, so need to re-run immediately)
- we're using the reliable-but-not-highest-performing `FOR UPDATE SKIPPED LOCK` atomic update primitive
    - we've avoided using the postgres pub/sub notify interface with in-memory tables for job updates because of [the added complexity a few other projects use](https://github.com/que-rb/que/blob/master/lib/que/migrations/4/up.sql) for higher performance, but we prefer a simpler approach at our current scale.
- lacking documentation, examples, samples, and automated db setup without doing everything manually

Job Selection:

- Using the `pj` script, on startup `--workers` numbers of completely independent workers are forked using `multiprocessing.Process` (defaults to number of cores on the system)
- If web endpoints are enabled, each worker also opens a web server for requests.
    - Under linux, each web server on each worker process can receive queries due to in-kernel TCP port load balancing.
    - On other platforms, only one of the workers will receive all web requests.
- Each worker polls the job database at 5 to 6 second intervals (it's 5 seconds plus plus a random delay between 0 and 1 millisecond so all the workers don't poll at exactly the same time on startup; see: `checkInterval` member of `pyjobby.pj.JobSystem`, not exposed as a command line param yet)
- If a worker finds a job, it claims the job, runs it, completes it, then immediately checks the job database for more jobs without entering the delay loop again.
    - see query `claim` for logic behind next job selection based on: matching server capability, allowed server priority, highest job priority (lower number is higher priority), scheduled run time, and current job state.
- If a worker doesn't find an eligible job, it returns to the 'sleep 5-6 seconds' request loop.


Installing
----------

No package, but this repo is installable.

```haskell
poetry add git+https://github.com/mattsta/pyjobby.git#main
```

Then you should be able to run `poetry run pj --help` to see the help output:

```haskell
> poetry run pj --help
Usage: pj [OPTIONS]

Options:
  --queue TEXT       Queue to process (can be multiple)  [default: default]
  --cap TEXT         Capabilities for this server  [default: ]
  --workers INTEGER  Worker count  [default: 4]
  --path TEXT        extra job class paths (can be multiple)  [default: .]
  -v                 show version then exit
  -c, --config TEXT  config file path  [default: ./pyjobby.conf.py]
  --help             Show this message and exit.
```

The postgres DB schema is available as dump at [`priv/schema.sql`](priv/schema.sql) and also as a [full SQLAlchemy class in `priv/schema.py` (has some included docs)](priv/schema.py).

There's no automated (or even helpful) install directions for the schema other than those two reference points for now.


Sample Config
-------------

Here's a sample default config file using a postgres socket in /tmp as well as allowing one job class to accept input from both a http web server and a unix socket web server:

```python
db_params = {
    "database": "kudzu",
    "user": "kudzu",
    "password": "",
    "host": "/tmp",
    "port": "5432",
}

web_listen = {
    "sites": [{"host": "127.0.0.1", "port": 6661}, {"path": "/tmp/pj.socket"}],
    "paths": set(["job.image.thumbnails.Thumbnails"]),
}
```

A Sample Job
------------

An example echo job, for example saved as `job/echo.py` from the viewpoint of where you ran the job system's `pj` startup script:

```python
import pyjobby
from dataclasses import dataclass
import sys

@dataclass
class EchoJob(pyjobby.pj.Job):
    def task(self, foo=3, **kwargs):
        # each Job instance also has access to a globally shared
        # cache on the worker, so you can store any persistent state,
        # credentials, or stats across all workers.
        count = self.s.cache.get("echo-count", 0)
        self.s.cache["echo-count"] = count + 1

        print("[echo] Got foo!!!!", foo, sys.path, kwargs, count)
```


Then the job would be executed by adding to the DB as:

```python
addJob(
    db,
    kwargs=dict(
        jobKey="mah key!",
        namespace=config.namespace,
        filepaths=fileReport,
    ),
    job_class="job.echo.EchoJob",
)
```

Jobs only need to inherit from `pyjobb.pj.Job` then override the `task()` method. The `task()` method will have the `kwargs` from the job row applied as arguments when run.

Job overrides of the `task()` method may be regular python methods, async methods, or even an async generator.

Sample Web Request
------------------

When receiving web requests, job classes use an extra `web()` method to read the aiohttp request directly, so the web request input/serialization format can be defined per job class.

The web request path is just the full job class path (as defined in your config) as seen from the root of the listening job server, for example:

```haskell
> curl -H "application/json" -d '[{"abc": 3}]' http://localhost:6661/job.image.thumbnails.Thumbnails
```

Example of job reading an aiohttp JSON post request where the JSON string is then parsed and applied as `kwargs` into the actual job implementation:
```python
async def web(self, request):
    # read JSON post body
    got = await request.json(loads=orjson.loads)

    await self.efficientThumbnails(**got)

    return web.Response(text="jobby job done done!")
```

Example of a simple job submitter
---------------------------------

(if you are using the SQLAlchemy schema from [`priv/schema.py`](priv/schema.py))

```python
# ============================================================================
# Job Submission
# ============================================================================
def addJob(db: Session, **kwargs) -> None:
    """ Add Jorb description to DB for immediate scheduling """
    j = table.Jorb(**kwargs)
    db.add(j)
    db.commit()
```

Example of job dependencies
----------------------------

This example would, if all the job classes were present, take a list of uploaded files (from `fileReport`), create 4 jobs to run concurrently (upload to origin, hash, extract exif, upload to origin cache), then after those 4 jobs are *all* complete, the final 7 echo test jobs then run (again, all concurrently if the job servers have enough free workers).

Also note how `capability` is defined to record the current host. The job server by default advertises its own hostname as a capability (as output by `platform.node()`), so we have the ability to say "jobs created on server X must be run by the job server on server X." In this example the uploaded files are on the server creating the newly enqueued jobs, so image processing must also be on the same server (since this server is the only place the files exist before being uploaded to replicated storage).

```python
def registerUploadForProcessing(user, fileReport, db):
    groupId = secrets.randbits(63)
    prio = user.priority # paid users get higher priority jobs than free users

    for _ in range(7):
        # NOTE: delayed 'waitfor_group' jobs must be created with a 'waiting' state
        #       so they aren't run immediately when submitted.
        addJob(
            db,
            kwargs=dict(
                jobKey="mah key!",
                namespace=config.namespace,
                filepaths=fileReport,
            ),
            state="waiting",
            job_class="job.echo.EchoJob",
            waitfor_group=groupId,
            prio=prio,
            uid=user.id,
            capability=f"host:{config.hostname}",
        )

    # TODO: technically all these jobs should be submitted in one transaction
    #       so they only become visible all at the same time (so waitfor_group
    #       will always see all of them at once instead of seeing them one-by-one
    #       as they get populated)
    #       Or, I guess more obviously, only insert the waitfor_ after all the jobs
    #       have already been added.
    for klass in [
        "job.file.upload.B2Origin",
        "job.image.hashes.Hashes",
        "job.image.exif.EXIF",
        "job.file.ring.RingUploader",
    ]:
        # Note: this job is pinned to capability of the current hostname because
        #       THIS hostname has the uploaded files, so the job system needs
        #       to also run ONLY ON THIS hostname to access the files that
        #       were uploaded...
        addJob(
            db,
            kwargs=dict(
                namespace=config.namespace,
                filepaths=fileReport,
            ),
            job_class=klass,
            prio=prio,
            uid=user.id,
            run_group=groupId,
            capability=f"host:{config.hostname}",
        )
```
