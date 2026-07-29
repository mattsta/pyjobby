"""Schema-drift symptoms and their documented remedies, end to end.

TROUBLESHOOTING.md § "The schema is missing or stale" makes a chain of
falsifiable claims: doctor detects each drift shape, names it, stops the
report, exits 1, and the remedy it prints actually repairs the condition
(or honestly refuses). Every test here induces the drift for real and runs
the documented commands the way an operator would.
"""

from __future__ import annotations

import pytest

from tests.ops.conftest import run_sql
from tests.schema_fixtures import dsn_from

pytestmark = [pytest.mark.ops, pytest.mark.slow]


class TestEmptyDatabase:
    async def test_doctor_fails_names_remedy_and_stops(self, admin, scratch):
        params = await scratch.create(install=None)
        report = admin("doctor", dsn=dsn_from(params))
        assert report.returncode == 1
        assert "PASS database: connected" in report.stdout
        assert (
            "FAIL schema: base schema not installed (run: pj-admin db migrate)"
            in report.stdout
        )
        # "Both checks stop the report": nothing below schema is printed.
        assert "notify-queue" not in report.stdout
        assert "workers" not in report.stdout

    async def test_migrate_installs_and_doctor_passes(self, admin, scratch):
        params = await scratch.create(install=None)
        dsn = dsn_from(params)
        migrate = admin("db", "migrate", dsn=dsn)
        assert migrate.returncode == 0
        assert "Installed base schema" in migrate.stdout

        report = admin("doctor", dsn=dsn)
        # "WARN workers" alone must not change the exit code: FAIL is the
        # only thing that does.
        assert report.returncode == 0
        assert "PASS schema: installed, migrations current (baseline)" in report.stdout
        assert "PASS triggers: all schema triggers present (7)" in report.stdout
        assert "WARN workers: no live workers seen in last 60s" in report.stdout


class TestRepairableDrift:
    """A dropped trigger or index heals through the command doctor names."""

    async def test_dropped_index_fail_names_it_and_migrate_recreates_it(
        self, admin, scratch
    ):
        params = await scratch.create()
        dsn = dsn_from(params)
        await run_sql(params, "DROP INDEX jorb_dag_retention_idx")

        report = admin("doctor", dsn=dsn)
        assert report.returncode == 1
        assert (
            "FAIL schema: installed, but 1 object(s) this release needs are "
            "missing: index jorb_dag_retention_idx (run: pj-admin db migrate)"
            in report.stdout
        )

        status = admin("db", "status", dsn=dsn)
        assert "Missing objects:       1" in status.stdout
        assert "index jorb_dag_retention_idx" in status.stdout

        migrate = admin("db", "migrate", dsn=dsn)
        assert migrate.returncode == 0
        assert (
            "Recreated missing index jorb_dag_retention_idx from the base schema"
            in migrate.stdout
        )
        assert admin("doctor", dsn=dsn).returncode == 0

    async def test_dropped_trigger_fail_names_it_and_migrate_recreates_it(
        self, admin, scratch
    ):
        params = await scratch.create()
        dsn = dsn_from(params)
        await run_sql(params, "DROP TRIGGER jorb_history_record ON jorb")

        report = admin("doctor", dsn=dsn)
        assert report.returncode == 1
        # The schema shape check must PASS -- a dropped trigger is reported
        # as the specific thing it is, on its own line.
        assert "PASS schema:" in report.stdout
        assert (
            "FAIL triggers: missing triggers: jorb_history_record "
            "(run: pj-admin db migrate)" in report.stdout
        )

        migrate = admin("db", "migrate", dsn=dsn)
        assert migrate.returncode == 0
        assert (
            "Recreated missing trigger jorb_history_record from the base schema"
            in migrate.stdout
        )
        assert admin("doctor", dsn=dsn).returncode == 0


class TestDeepDrift:
    """A missing column cannot be healed; every surface says so honestly."""

    async def test_doctor_prescribes_recreate_not_migrate(self, admin, scratch):
        params = await scratch.create()
        dsn = dsn_from(params)
        await run_sql(params, "ALTER TABLE jorb_worker DROP COLUMN job_threads")

        report = admin("doctor", dsn=dsn)
        assert report.returncode == 1
        assert "column jorb_worker.job_threads" in report.stdout
        # doctor must NOT send the operator to a command that will refuse.
        assert "(run: pj-admin db migrate)" not in report.stdout
        assert "recreate the database or reconcile by hand" in report.stdout

        migrate = admin("db", "migrate", dsn=dsn)
        assert migrate.returncode == 1
        assert "No pending migration repairs this" in migrate.stderr + migrate.stdout

    async def test_admin_command_turns_missing_table_into_remedy(self, admin, scratch):
        params = await scratch.create()
        await run_sql(params, "DROP TABLE jorb_worker CASCADE")

        listing = admin("workers", "list", dsn=dsn_from(params))
        assert listing.returncode == 1
        out = listing.stdout + listing.stderr
        assert (
            "The database schema is missing or out of date: "
            'relation "jorb_worker" does not exist' in out
        )
        assert "Install or upgrade it with `pj-admin db migrate`" in out
        assert "Traceback" not in out


class TestConfigErrors:
    async def test_nonexistent_config_file_is_a_loud_structured_error(self, admin):
        report = admin("-c", "/nonexistent.toml", "doctor", dsn=None)
        assert report.returncode == 1
        out = report.stdout + report.stderr
        assert "Could not load config file: /nonexistent.toml" in out
        assert "'/nonexistent.toml' doesn't exist" in out
        assert (
            "Use --config to point at a pyjobby conf file, "
            "or --dsn to connect directly." in out
        )
        assert "FAIL config: unusable" in out
