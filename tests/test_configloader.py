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

from pyjobby.configloader import ConfigError, load_config_from_file


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
unrelated = "ignored"

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
        assert "unrelated" not in cfg
        assert "web_listen" not in cfg  # not requested

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

    def test_substitution_reaches_into_arrays_and_tables(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setenv("PYJOBBY_TEST_HOST", "10.0.0.9")
        path = write(
            tmp_path,
            '[web_listen]\nsites = [{ host = "${PYJOBBY_TEST_HOST}", port = 1 }]\n',
        )

        cfg = load_config_from_file(path, ["web_listen"])

        assert cfg["web_listen"]["sites"][0]["host"] == "10.0.0.9"

    def test_an_unset_variable_is_a_loud_error_naming_it(
        self, tmp_path, monkeypatch
    ):
        """A config that silently loads with a missing secret fails later,
        further from the cause."""
        monkeypatch.delenv("PYJOBBY_TEST_UNSET", raising=False)
        path = write(
            tmp_path, '[db_params]\npassword = "${PYJOBBY_TEST_UNSET}"\n'
        )

        with pytest.raises(ConfigError, match="PYJOBBY_TEST_UNSET"):
            load_config_from_file(path, ["db_params"])

    def test_partial_references_are_left_alone(self, tmp_path):
        """Only a value that IS an env reference substitutes; embedded
        "${A}:${B}" templating invites quoting bugs and is not offered."""
        path = write(tmp_path, '[db_params]\nhost = "prefix-${NOT_A_REF}"\n')

        cfg = load_config_from_file(path, ["db_params"])

        assert cfg["db_params"]["host"] == "prefix-${NOT_A_REF}"


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
        settings file, and the message says where to move the settings."""
        path = write(
            tmp_path,
            "import os; os.system('true')  # must never run\n",
            name="pyjobby.conf.py",
        )

        with pytest.raises(ConfigError, match="never executed"):
            load_config_from_file(path, ["db_params"])

    def test_config_errors_are_runtime_errors(self, tmp_path):
        """Callers catching RuntimeError (every CLI entry point) keep
        working."""
        with pytest.raises(RuntimeError):
            load_config_from_file(str(tmp_path / "absent.toml"), ["db_params"])
