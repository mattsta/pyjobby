"""The config loader reads DATA, never code.

pyjobby config is a TOML file. These tests pin the whole contract:
requested keys come back (case-insensitively) and nothing else; secrets
arrive via explicit ``"${VAR}"`` environment references that fail LOUDLY
when unset; and the failure modes are parse errors at a line — a config
file can no longer execute anything, and a ``.py`` config is refused by
name with the migration hint rather than run.
"""

from __future__ import annotations

import pytest

from pyjobby import db
from pyjobby.configloader import (
    _ASYNCPG_CONNECT_KEYS_FALLBACK,
    ASYNCPG_CONNECT_KEYS,
    KNOWN_TOP_LEVEL_KEYS,
    ConfigError,
    describe_db_target,
    load_config_from_file,
)


def write(tmp_path, text: str, name: str = "pyjobby.toml"):
    path = tmp_path / name
    path.write_text(text)
    return str(path)


class TestLoading:
    def test_requested_keys_come_back_and_nothing_else(self, tmp_path):
        path = write(
            tmp_path,
            """
prio_ceiling = 900

[db_params]
host = "localhost"
port = 5432

[web_listen]
sites = [{ host = "127.0.0.1", port = 8080 }]
paths = []
""",
        )

        cfg = load_config_from_file(path, ["db_params", "prio_ceiling"])

        assert cfg == {
            "prio_ceiling": 900,
            "db_params": {"host": "localhost", "port": 5432},
        }
        assert "web_listen" not in cfg  # known, but not requested

    def test_keys_are_matched_case_insensitively(self, tmp_path):
        path = write(tmp_path, 'DB_PARAMS = { host = "h" }\n')

        cfg = load_config_from_file(path, ["db_params"])

        assert cfg["db_params"] == {"host": "h"}

    def test_absent_keys_are_simply_absent(self, tmp_path):
        """TOML has no null: optional settings are omitted, and callers use
        .get() — a missing web_listen means no web listener."""
        path = write(tmp_path, '[db_params]\nhost = "h"\n')

        cfg = load_config_from_file(path, ["db_params", "web_listen"])

        assert cfg.get("web_listen") is None


class TestUnknownKeys:
    """A key outside KNOWN_TOP_LEVEL_KEYS is refused, not skipped.

    Every daemon asks for the SUBSET of keys it cares about, so a dropped
    unknown key is indistinguishable from a key this process did not want:
    `prio_ceilng = 100` would leave the ceiling at its default in every
    process, forever, with the file sitting there saying otherwise.
    """

    def test_an_unknown_key_is_refused_naming_it_and_the_file(self, tmp_path):
        path = write(tmp_path, '[db_params]\nhost = "h"\n\n[web_lissten]\nx = 1\n')

        with pytest.raises(ConfigError) as excinfo:
            load_config_from_file(path, ["db_params"])

        message = str(excinfo.value)
        assert "web_lissten" in message, "the message must name the typo"
        assert path in message, "the message must name the file"
        for known in KNOWN_TOP_LEVEL_KEYS:
            assert known in message, "the message must list the known keys"

    def test_it_is_refused_even_when_the_caller_did_not_ask_for_it(self, tmp_path):
        """The check is the FILE's, not the request's: pj asks for three keys
        and the scheduler for two, so a typo caught only by whoever happened
        to request that key is a typo caught by nobody."""
        path = write(tmp_path, 'prio_ceilng = 100\n[db_params]\nhost = "h"\n')

        with pytest.raises(ConfigError, match="prio_ceilng"):
            load_config_from_file(path, ["db_params"])

    def test_known_keys_load_in_any_case(self, tmp_path):
        """Key matching is case-insensitive, so the unknown-key check has to
        be too, or DB_PARAMS becomes an error."""
        path = write(tmp_path, 'DB_PARAMS = { host = "h" }\nPRIO_CEILING = 7\n')

        cfg = load_config_from_file(path, ["db_params", "prio_ceiling"])

        assert cfg == {"db_params": {"host": "h"}, "prio_ceiling": 7}

    def test_the_shipped_config_loads_through_the_real_loader(self, monkeypatch):
        """The repo's own pyjobby.toml is the file operators copy, so it is
        checked the way a daemon checks it -- not by re-parsing it here.

        A bare ``tomllib.load`` plus ``set(raw) <= KNOWN_TOP_LEVEL_KEYS``
        passed while the shipped file had ``prio_ceiling`` written UNDER
        ``[db_params]``: TOML puts it inside the table, so the top-level key
        set was ``{"db_params"}`` and the assertion was about nothing. Loading
        it through the loader and asserting WHERE each setting landed is the
        check that would have caught it.
        """
        from pathlib import Path

        shipped = Path(__file__).resolve().parent.parent / "pyjobby.toml"
        monkeypatch.setenv("PYJOBBY_DB_PASSWORD", "from-the-environment")

        cfg = load_config_from_file(
            str(shipped),
            [
                "db_params",
                "prio_ceiling",
                "app_version",
                "liveness_grace_seconds",
                "web_listen",
            ],
        )

        assert cfg["prio_ceiling"] == 1000, (
            "prio_ceiling must be a TOP-LEVEL key; written after the "
            "[db_params] header it belongs to that table and no enqueue "
            "surface ever reads it"
        )
        assert "prio_ceiling" not in cfg["db_params"]
        # ...and the same for the liveness grace, which has five readers and
        # would otherwise be an asyncpg.connect() keyword that does not exist.
        assert cfg["liveness_grace_seconds"] == 60
        assert "liveness_grace_seconds" not in cfg["db_params"]
        assert cfg["db_params"]["database"] == "pyjobby"
        # the secret really is substituted, not left as the literal reference
        assert cfg["db_params"]["password"] == "from-the-environment"

    def test_the_liveness_grace_round_trips_as_a_top_level_key(self, tmp_path):
        """`liveness_grace_seconds` is a config key, not just a monitor flag.

        The value has FIVE readers -- pj-monitor sweeps by it while doctor,
        /metrics, the workers page and the WebSocket dashboard report by it --
        so it has to survive the loader as a top-level setting or the only
        place it can be declared is a flag on one daemon, which is exactly
        the state that let the monitor and every UI disagree.
        """
        path = write(
            tmp_path,
            'liveness_grace_seconds = 300\n\n[db_params]\nhost = "h"\n',
        )

        cfg = load_config_from_file(path, ["db_params", "liveness_grace_seconds"])

        assert cfg == {"db_params": {"host": "h"}, "liveness_grace_seconds": 300}
        assert db.resolve_liveness_grace(None, cfg["liveness_grace_seconds"]) == 300.0

    def test_the_liveness_grace_resolves_flag_then_file_then_default(self):
        """The precedence every deployment setting in this platform uses.

        ``is not None`` and not ``or``: a configured 0 is a real (bad)
        threshold, and silently substituting 60 for it would hide the
        misconfiguration the monitor warns about at startup.
        """
        assert db.resolve_liveness_grace(300.0, 120) == 300.0  # flag wins
        assert db.resolve_liveness_grace(None, 120) == 120.0  # then the file
        assert (
            db.resolve_liveness_grace(None, None)  # then the platform default
            == db.DEFAULT_LIVENESS_GRACE_SECONDS
        )
        assert db.resolve_liveness_grace(None, 0) == 0.0
        assert db.resolve_liveness_grace(0, 120) == 0.0

    def test_a_top_level_setting_written_under_db_params_is_refused(self, tmp_path):
        """The misplacement the shipped file itself had.

        Not a typo -- the key is spelled correctly and reads naturally where
        it sits. TOML has no way back out to the root, so it is a db_params
        key, an asyncpg.connect() keyword that does not exist, and the
        deployment silently runs on the default ceiling.
        """
        path = write(tmp_path, '[db_params]\nhost = "h"\nprio_ceiling = 100\n')

        with pytest.raises(ConfigError) as excinfo:
            load_config_from_file(path, ["db_params"])

        message = str(excinfo.value)
        assert "prio_ceiling" in message, "the message must name the key"
        assert "ABOVE the [db_params] header" in message, (
            "the message must say where the key belongs, not merely that it "
            "is wrong here"
        )

    def test_a_non_connect_keyword_under_db_params_is_refused_too(self, tmp_path):
        """The plain typo arm: not a top-level setting either, so the message
        says what the table IS rather than where to move the key."""
        path = write(tmp_path, '[db_params]\nhost = "h"\nhostt = "typo"\n')

        with pytest.raises(ConfigError, match="asyncpg.connect"):
            load_config_from_file(path, ["db_params"])

    def test_a_connect_keyword_in_the_wrong_case_is_refused(self, tmp_path):
        """`Host` is not a spelling of `host`.

        The table is handed to ``asyncpg.connect(**db_params)``, and keyword
        arguments are case-sensitive: a capitalised key is a keyword that does
        not exist. The check used to fold case, so this file loaded cleanly and
        then raised ``TypeError: connect() got an unexpected keyword argument
        'Host'`` from inside a daemon's startup, naming no file and no line.
        """
        path = write(tmp_path, '[db_params]\nHost = "h"\n')

        with pytest.raises(ConfigError) as excinfo:
            load_config_from_file(path, ["db_params"])

        message = str(excinfo.value)
        assert "'Host'" in message, "the message must name the key as written"
        assert "lowercase" in message, (
            "the operator has to be told the rule, not merely that the key is wrong"
        )
        assert "'host'" in message, "the message must name the fix"

    def test_the_connect_keywords_this_table_accepts_are_really_asyncpgs(self):
        """The allow-list IS another library's signature, read at import.

        Derived rather than transcribed because this project pins no upper
        bound on asyncpg: a hand-kept list starts refusing a keyword the day a
        release adds one, and refuses it by blaming the operator's config file.
        Both directions are asserted -- nothing accepted that connect() would
        reject, and nothing rejected that connect() would accept.
        """
        import inspect

        import asyncpg

        accepted = {
            name
            for name, p in inspect.signature(asyncpg.connect).parameters.items()
            if p.kind is not inspect.Parameter.VAR_KEYWORD
        }
        unknown = ASYNCPG_CONNECT_KEYS - accepted
        assert not unknown, (
            f"[db_params] would accept {sorted(unknown)}, which "
            f"asyncpg.connect() does not take: the table is passed to it "
            f"verbatim, so these would be a TypeError at connect time"
        )
        # `loop` is the one deliberate omission: a TOML file cannot name an
        # event loop, so a string there would reach asyncpg as one.
        assert accepted - ASYNCPG_CONNECT_KEYS == {"loop"}, (
            f"[db_params] refuses {sorted(accepted - ASYNCPG_CONNECT_KEYS)}, "
            f"which asyncpg.connect() accepts: an operator who needs one of "
            f"these would be told their config file is wrong when it is not"
        )

    def test_the_shipped_fallback_is_not_narrower_than_the_derived_set(self):
        """What a deployment gets when the signature cannot be introspected.

        The literal exists only for that case, so the one way it can do harm
        is by being SMALLER than the real signature -- every key it is missing
        becomes a ConfigError against a config file that is correct.
        """
        assert _ASYNCPG_CONNECT_KEYS_FALLBACK <= ASYNCPG_CONNECT_KEYS, (
            f"the fallback names keys the derived set does not: "
            f"{sorted(_ASYNCPG_CONNECT_KEYS_FALLBACK - ASYNCPG_CONNECT_KEYS)}"
        )
        # and it is a real list, not an empty set that would accept nothing
        assert {"host", "port", "database", "user", "password", "dsn"} <= (
            _ASYNCPG_CONNECT_KEYS_FALLBACK
        )


class TestDescribeDbTarget:
    """Operator-facing messages name the database and never the password."""

    def test_db_params_become_host_port_database(self):
        assert (
            describe_db_target(
                {
                    "host": "db.internal",
                    "port": 6432,
                    "database": "pyjobby",
                    "user": "pj",
                    "password": "hunter2",
                }
            )
            == "db.internal:6432/pyjobby"
        )

    def test_a_dsn_keeps_only_what_follows_the_credentials(self):
        described = describe_db_target("postgresql://pj:hunter2@db.internal:5432/pj")

        assert described == "db.internal:5432/pj"
        assert "hunter2" not in described

    def test_no_params_still_describes_something(self):
        assert describe_db_target(None) == "the connected database"


class TestEnvSubstitution:
    def test_a_whole_string_env_reference_is_replaced(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PYJOBBY_TEST_SECRET", "s3cr3t")
        path = write(
            tmp_path,
            '[db_params]\npassword = "${PYJOBBY_TEST_SECRET}"\nhost = "h"\n',
        )

        cfg = load_config_from_file(path, ["db_params"])

        assert cfg["db_params"]["password"] == "s3cr3t"
        assert cfg["db_params"]["host"] == "h"

    def test_substitution_reaches_into_arrays_and_tables(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PYJOBBY_TEST_HOST", "10.0.0.9")
        path = write(
            tmp_path,
            '[web_listen]\nsites = [{ host = "${PYJOBBY_TEST_HOST}", port = 1 }]\n',
        )

        cfg = load_config_from_file(path, ["web_listen"])

        assert cfg["web_listen"]["sites"][0]["host"] == "10.0.0.9"

    def test_an_unset_variable_is_a_loud_error_naming_it(self, tmp_path, monkeypatch):
        """A config that silently loads with a missing secret fails later,
        further from the cause."""
        monkeypatch.delenv("PYJOBBY_TEST_UNSET", raising=False)
        path = write(tmp_path, '[db_params]\npassword = "${PYJOBBY_TEST_UNSET}"\n')

        with pytest.raises(ConfigError, match="PYJOBBY_TEST_UNSET"):
            load_config_from_file(path, ["db_params"])

    def test_partial_references_are_left_alone(self, tmp_path):
        """Only a value that IS an env reference substitutes; embedded
        "${A}:${B}" templating invites quoting bugs and is not offered."""
        path = write(tmp_path, '[db_params]\nhost = "prefix-${NOT_A_REF}"\n')

        cfg = load_config_from_file(path, ["db_params"])

        assert cfg["db_params"]["host"] == "prefix-${NOT_A_REF}"

    def test_a_trailing_newline_defeats_the_reference(self, tmp_path, monkeypatch):
        """The anchor is \\Z, not $ -- $ also matches before a trailing
        newline, which would substitute "${VAR}\\n" and drop the newline."""
        monkeypatch.setenv("PYJOBBY_TEST_NL", "secret")
        path = write(tmp_path, '[db_params]\nhost = """${PYJOBBY_TEST_NL}\n"""\n')

        cfg = load_config_from_file(path, ["db_params"])

        # a value with a trailing newline is NOT a bare reference; left literal
        assert cfg["db_params"]["host"] == "${PYJOBBY_TEST_NL}\n"


class TestFailureModes:
    def test_missing_file(self, tmp_path):
        with pytest.raises(ConfigError, match="doesn't exist"):
            load_config_from_file(str(tmp_path / "absent.toml"), ["db_params"])

    def test_invalid_toml_is_a_parse_error(self, tmp_path):
        path = write(tmp_path, "= not toml\n")

        with pytest.raises(ConfigError, match="Failed to parse config file"):
            load_config_from_file(path, ["db_params"])

    def test_a_python_config_is_refused_by_name(self, tmp_path):
        """The refusal happens BEFORE the file is even opened: an executable
        config format is a remote-code-execution primitive wearing a
        settings file, and the message says where to move the settings.

        Proven by an OBSERVABLE side effect, not just by the error: the
        payload would create a sentinel file if it ran, so its ABSENCE after
        the refusal is what shows the code never executed. Asserting only the
        exception would pass even for a loader that ran the file and then
        raised."""
        sentinel = tmp_path / "payload-ran"
        path = write(
            tmp_path,
            f"import pathlib; pathlib.Path({str(sentinel)!r}).write_text('ran')\n",
            name="pyjobby.conf.py",
        )

        with pytest.raises(ConfigError, match="never executed"):
            load_config_from_file(path, ["db_params"])

        assert not sentinel.exists(), "the .py config was executed before refusal"

    def test_config_errors_are_runtime_errors(self, tmp_path):
        """Callers catching RuntimeError (every CLI entry point) keep
        working."""
        with pytest.raises(RuntimeError):
            load_config_from_file(str(tmp_path / "absent.toml"), ["db_params"])

    def test_a_non_utf8_file_is_a_config_error_not_a_raw_traceback(self, tmp_path):
        """UnicodeDecodeError must arrive as ConfigError (RuntimeError) so the
        `except RuntimeError` guard in every CLI entry point catches it."""
        path = tmp_path / "bad.toml"
        path.write_bytes(b'host = "\xff\xfe not utf-8"\n')

        with pytest.raises(ConfigError):
            load_config_from_file(str(path), ["db_params"])

    def test_an_oversized_file_is_refused_unread(self, tmp_path):
        path = tmp_path / "huge.toml"
        path.write_text("x = 1\n" + "# padding\n" * 200_000)

        with pytest.raises(ConfigError, match="refused above"):
            load_config_from_file(str(path), ["db_params"])
