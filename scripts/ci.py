#!/usr/bin/env python3
"""GitHub CI results, fetched once and parsed into stable structures.

The fix loop's whole interface (via ./scripts/ci.sh, or directly):

    ci.py status    [RUN_ID]            verdict, jobs, failed steps
    ci.py failures  [RUN_ID] [--json]   parsed failure evidence per failed step
    ci.py failures  --full  [RUN_ID]    raw logs of failed steps, unfiltered
    ci.py watch     [RUN_ID]            block until done, then `status`
    ci.py rerun     [RUN_ID]            rerun failed jobs

Without RUN_ID every command uses the newest run of the current branch.
Everything goes through `gh` (already authenticated) exactly twice per
invocation at most: one JSON view of the run, one download of its log
archive. The archive is the ground truth the parsing works from — one
file per step, named "<job>/<step number>_<step name>.txt" — so failures
are attributed to real steps rather than scraped out of interleaved text.

`failures` extracts, per failed step:
  * pytest: the `short test summary info` block (every FAILED/ERROR line
    with its reason), plus per-test `_ _ _` failure sections' assert tails;
  * mypy / ruff: their diagnostic lines;
  * generic: `##[error]` annotations and a noise-filtered tail (service
    container chatter — postgres checkpoints, docker teardown — removed).

--json prints the same structure machine-readably, stable keys:
  {run, url, sha, conclusion, jobs: [{name, conclusion, failed_steps:
   [{step, signatures: [...], tail: [...]}]}]}
"""

from __future__ import annotations

import io
import json
import re
import subprocess
import sys
import zipfile
from dataclasses import dataclass, field

TAIL_LINES = 30

#: Log lines that are environment chatter, never failure evidence.
NOISE = re.compile(
    r"postgres.* (LOG|STATEMENT|DETAIL|HINT):"
    r"|checkpoint (starting|complete):"
    r"|##\[(group|endgroup|command)\]"
    r"|/usr/bin/docker"
    r"|Stop and remove container|Remove container network|Cleaning up orphan"
    r"|terminating autovacuum"
)

#: One line of failure evidence, by tool.
SIGNATURE = re.compile(
    r"^(FAILED|ERROR) tests?/"  # pytest short-summary entries
    r"|^E\s+\S"  # pytest assertion detail
    r"|\.py:\d+: (error|AssertionError)"  # mypy / traceback anchors
    r"|error\[[a-z-]+\]"  # ruff diagnostics
    r"|would reformat"
    r"|##\[error\]"
)

#: The timestamp every archive log line starts with.
TS = re.compile(r"^\S+ ")


def gh(*args: str, binary: bool = False) -> bytes | str:
    """The single choke point every GitHub interaction goes through."""
    result = subprocess.run(["gh", *args], capture_output=True, timeout=300)
    if result.returncode != 0:
        sys.stderr.write(result.stderr.decode(errors="replace"))
        raise SystemExit(f"gh {' '.join(args[:3])} ... exited {result.returncode}")
    return result.stdout if binary else result.stdout.decode(errors="replace")


@dataclass
class FailedStep:
    step: str
    signatures: list[str] = field(default_factory=list)
    tail: list[str] = field(default_factory=list)


@dataclass
class Job:
    name: str
    conclusion: str
    failed_steps: list[FailedStep] = field(default_factory=list)


@dataclass
class Run:
    id: int
    url: str
    sha: str
    title: str
    status: str
    conclusion: str
    jobs: list[Job]

    @property
    def failed_jobs(self) -> list[Job]:
        return [j for j in self.jobs if j.conclusion == "failure"]


def current_branch() -> str:
    return subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def resolve_run_id(argv: list[str]) -> int:
    for arg in argv:
        if arg.isdigit():
            return int(arg)
    listing = json.loads(
        gh(
            "run",
            "list",
            "--branch",
            current_branch(),
            "--limit",
            "1",
            "--json",
            "databaseId",
        )
    )
    if not listing:
        raise SystemExit(f"no CI runs found for branch {current_branch()!r}")
    return int(listing[0]["databaseId"])


def fetch_run(run_id: int) -> tuple[Run, dict[str, list[str]]]:
    """One JSON view + one log archive → (parsed run, failed-step logs).

    The archive holds ONE log per job ("<n>_<job name>.txt" — the per-step
    files GitHub used to ship are gone), so failed-step extraction joins by
    TIME instead: the view gives each failed step's startedAt/completedAt,
    and every archive line starts with a timestamp. Second-resolution
    slicing is exact enough — a boundary second can carry a stray line
    from the neighboring step, never a missing one.
    """
    view = json.loads(
        gh(
            "run",
            "view",
            str(run_id),
            "--json",
            "displayTitle,headSha,status,conclusion,url,jobs",
        )
    )
    failed_steps: list[tuple[str, str, str, str]] = []  # job, step, start, end
    jobs: list[Job] = []
    for job in view["jobs"]:
        jobs.append(Job(job["name"], job.get("conclusion") or job["status"]))
        for step in job.get("steps") or []:
            if step.get("conclusion") == "failure":
                failed_steps.append(
                    (
                        job["name"],
                        step["name"],
                        (step.get("startedAt") or "")[:19],
                        (step.get("completedAt") or "9999")[:19],
                    )
                )

    logs: dict[str, list[str]] = {}
    if failed_steps:
        archive = zipfile.ZipFile(
            io.BytesIO(
                gh(
                    "api",
                    f"repos/{{owner}}/{{repo}}/actions/runs/{run_id}/logs",
                    binary=True,
                )
            )
        )
        job_logs: dict[str, list[str]] = {}
        for name in archive.namelist():
            match = re.fullmatch(r"\d+_(.+)\.txt", name)
            if match:
                job_logs[match.group(1)] = (
                    archive.read(name).decode(errors="replace").splitlines()
                )

        for job_name, step_name, started, ended in failed_steps:
            window = [
                TS.sub("", line)
                for line in job_logs.get(job_name, [])
                if started <= line[:19] <= ended
            ]
            logs[f"{job_name}\t{step_name}"] = window

    run = Run(
        run_id,
        view["url"],
        view["headSha"][:9],
        view["displayTitle"],
        view["status"],
        view.get("conclusion") or "-",
        jobs,
    )
    for key, lines in logs.items():
        job_name, step_name = key.split("\t", 1)
        job = next(j for j in run.jobs if j.name == job_name)
        job.failed_steps.append(parse_step(step_name, lines))
    return run, logs


def parse_step(step_name: str, lines: list[str]) -> FailedStep:
    """Failure evidence for one step: signatures plus a de-noised tail."""
    parsed = FailedStep(step_name)
    in_summary = False
    for line in lines:
        if "short test summary info" in line:
            in_summary = True
            continue
        if in_summary and line.startswith("="):
            in_summary = False
        if (
            in_summary
            and line.strip()
            or SIGNATURE.search(line)
            and not NOISE.search(line)
        ):
            parsed.signatures.append(line.rstrip())

    clean = [ln.rstrip() for ln in lines if ln.strip() and not NOISE.search(ln)]
    parsed.tail = clean[-TAIL_LINES:]
    return parsed


def print_status(run: Run) -> None:
    print(f"run   {run.url}")
    print(f"title {run.title} ({run.sha})")
    print(f"state {run.status} / {run.conclusion}")
    for job in run.jobs:
        print(f"  [{job.conclusion}] {job.name}")
        for step in job.failed_steps:
            print(f"      FAILED step: {step.step}")


def print_failures(run: Run) -> None:
    print_status(run)
    for job in run.failed_jobs:
        for step in job.failed_steps:
            print(f"\n===== {job.name} :: {step.step} =====")
            if step.signatures:
                print(f"  -- evidence ({len(step.signatures)} line(s)) --")
                for line in step.signatures:
                    print(f"    {line}")
            print("  -- tail (noise-filtered) --")
            for line in step.tail:
                print(f"    {line}")


def main(argv: list[str]) -> int:
    command = argv[0] if argv and not argv[0].isdigit() else "status"
    rest = argv[1:] if argv and argv[0] == command else argv
    run_id = resolve_run_id(rest)

    if command == "status":
        run, _ = fetch_run(run_id)
        print_status(run)
        return 0 if run.conclusion in ("success", "-") else 1
    if command == "failures":
        run, logs = fetch_run(run_id)
        if "--full" in rest:
            for key, lines in logs.items():
                print(f"===== {key.replace(chr(9), ' :: ')} =====")
                print("\n".join(lines))
        elif "--json" in rest:
            print(
                json.dumps(
                    {
                        "run": run.id,
                        "url": run.url,
                        "sha": run.sha,
                        "conclusion": run.conclusion,
                        "jobs": [
                            {
                                "name": j.name,
                                "conclusion": j.conclusion,
                                "failed_steps": [vars(s) for s in j.failed_steps],
                            }
                            for j in run.jobs
                        ],
                    },
                    indent=2,
                )
            )
        else:
            print_failures(run)
        return 0 if run.conclusion in ("success", "-") else 1
    if command == "watch":
        code = subprocess.run(["gh", "run", "watch", str(run_id), "--exit-status"])
        run, _ = fetch_run(run_id)
        print()
        print_status(run)
        return code.returncode
    if command == "rerun":
        gh("run", "rerun", str(run_id), "--failed")
        print(f"rerunning failed jobs of run {run_id};")
        print(f"follow with: ./scripts/ci.sh watch {run_id}")
        return 0

    sys.stderr.write(__doc__ or "")
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
