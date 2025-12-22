#!/usr/bin/env python3
"""
Comprehensive tests for configloader module.

Tests configuration loading from files and modules.
Coverage target: 70%+
"""

import os
import sys

import pytest

from pyjobby.configloader import (
    chdir_addpath,
    get_config_from_filename,
    get_config_from_module_name,
    load_config_from_file,
    load_config_from_module_name_or_filename,
)

# =============================================================================
# Test chdir_addpath
# =============================================================================


class TestChdirAddpath:
    """Test chdir_addpath function."""

    def test_chdir_and_addpath(self, tmp_path):
        """Test that chdir_addpath changes directory and adds to sys.path."""
        original_cwd = os.getcwd()
        original_syspath = sys.path.copy()

        try:
            # Create a temporary directory
            test_dir = tmp_path / "test_config_dir"
            test_dir.mkdir()

            # Call chdir_addpath
            chdir_addpath(str(test_dir))

            # Verify directory changed
            assert os.getcwd() == str(test_dir)

            # Verify path added to sys.path
            assert str(test_dir) in sys.path
            assert sys.path[0] == str(test_dir)  # Should be first
        finally:
            # Restore original state
            os.chdir(original_cwd)
            sys.path[:] = original_syspath

    def test_chdir_addpath_already_in_syspath(self, tmp_path):
        """Test that chdir_addpath doesn't add duplicate paths."""
        original_cwd = os.getcwd()
        original_syspath = sys.path.copy()

        try:
            test_dir = tmp_path / "test_duplicate"
            test_dir.mkdir()

            # Add path to sys.path first
            sys.path.insert(0, str(test_dir))

            # Call chdir_addpath
            chdir_addpath(str(test_dir))

            # Verify no duplicate added
            assert sys.path.count(str(test_dir)) == 1
        finally:
            os.chdir(original_cwd)
            sys.path[:] = original_syspath


# =============================================================================
# Test get_config_from_filename
# =============================================================================


class TestGetConfigFromFilename:
    """Test get_config_from_filename function."""

    def test_load_py_file(self, tmp_path):
        """Test loading a .py config file."""
        # Create a test config file
        config_file = tmp_path / "test_config.py"
        config_file.write_text("""
DB_HOST = "localhost"
DB_PORT = 5432
DB_NAME = "testdb"
DEBUG = True
MAX_WORKERS = 10
""")

        config = get_config_from_filename(str(config_file))

        assert config["DB_HOST"] == "localhost"
        assert config["DB_PORT"] == 5432
        assert config["DB_NAME"] == "testdb"
        assert config["DEBUG"] is True
        assert config["MAX_WORKERS"] == 10

    def test_load_file_with_no_extension(self, tmp_path):
        """Test loading a config file with no extension."""
        config_file = tmp_path / "config"
        config_file.write_text("""
QUEUE = "high-priority"
TIMEOUT = 30
""")

        config = get_config_from_filename(str(config_file))

        assert config["QUEUE"] == "high-priority"
        assert config["TIMEOUT"] == 30

    def test_nonexistent_file_raises_error(self):
        """Test that loading non-existent file raises RuntimeError."""
        with pytest.raises(RuntimeError, match="doesn't exist"):
            get_config_from_filename("/nonexistent/path/config.py")

    def test_invalid_syntax_exits(self, tmp_path):
        """Test that invalid Python syntax exits the process."""
        config_file = tmp_path / "bad_config.py"
        config_file.write_text("""
VALID_VAR = "test"
this is invalid python syntax!!!
""")

        # Should call sys.exit(1) on syntax error
        with pytest.raises(SystemExit) as exc_info:
            get_config_from_filename(str(config_file))

        assert exc_info.value.code == 1


# =============================================================================
# Test get_config_from_module_name
# =============================================================================


class TestGetConfigFromModuleName:
    """Test get_config_from_module_name function."""

    def test_load_existing_module(self):
        """Test loading configuration from an existing module."""
        # Use a real Python module
        config = get_config_from_module_name("os")

        # Should have standard os module attributes
        assert "path" in config
        assert "environ" in config

    def test_load_custom_module(self, tmp_path, monkeypatch):
        """Test loading a custom module."""
        # Create a custom module
        module_dir = tmp_path / "custom_module"
        module_dir.mkdir()

        config_module = module_dir / "my_config.py"
        config_module.write_text("""
APP_NAME = "PyJobby"
VERSION = "2.0.0"
ENABLED = True
""")

        # Add to sys.path so it can be imported
        monkeypatch.syspath_prepend(str(module_dir))

        config = get_config_from_module_name("my_config")

        assert config["APP_NAME"] == "PyJobby"
        assert config["VERSION"] == "2.0.0"
        assert config["ENABLED"] is True


# =============================================================================
# Test load_config_from_module_name_or_filename
# =============================================================================


class TestLoadConfigFromModuleNameOrFilename:
    """Test load_config_from_module_name_or_filename function."""

    def test_load_with_python_prefix(self, tmp_path, monkeypatch):
        """Test loading with 'python:' prefix."""
        # Create a module
        module_dir = tmp_path / "test_modules"
        module_dir.mkdir()

        config_module = module_dir / "app_config.py"
        config_module.write_text("""
db_host = "localhost"
db_port = 5432
api_key = "secret123"
other_var = "ignored"
""")

        monkeypatch.syspath_prepend(str(module_dir))

        config = load_config_from_module_name_or_filename(
            "python:app_config", keys=["db_host", "db_port", "api_key"]
        )

        # Only requested keys should be returned (lowercased)
        assert config == {
            "db_host": "localhost",
            "db_port": 5432,
            "api_key": "secret123",
        }
        # other_var should not be in config
        assert "other_var" not in config

    def test_load_with_file_prefix(self, tmp_path):
        """Test loading with 'file:' prefix."""
        config_file = tmp_path / "file_config.py"
        config_file.write_text("""
REDIS_HOST = "127.0.0.1"
REDIS_PORT = 6379
WORKERS = 5
IGNORED = "not requested"
""")

        config = load_config_from_module_name_or_filename(
            f"file:{config_file}", keys=["redis_host", "redis_port", "workers"]
        )

        assert config == {"redis_host": "127.0.0.1", "redis_port": 6379, "workers": 5}
        assert "ignored" not in config

    def test_load_without_prefix(self, tmp_path):
        """Test loading without any prefix (defaults to file)."""
        config_file = tmp_path / "no_prefix.py"
        config_file.write_text("""
QUEUE_NAME = "default"
PRIORITY = 100
""")

        config = load_config_from_module_name_or_filename(
            str(config_file), keys=["queue_name", "priority"]
        )

        assert config == {"queue_name": "default", "priority": 100}

    def test_keys_case_insensitive(self, tmp_path):
        """Test that keys matching is case-insensitive."""
        config_file = tmp_path / "case_test.py"
        config_file.write_text("""
DB_HOST = "localhost"
db_port = 5432
Db_Name = "testdb"
""")

        config = load_config_from_module_name_or_filename(
            str(config_file), keys=["db_host", "db_port", "db_name"]
        )

        # All should be lowercased in result
        assert "db_host" in config
        assert "db_port" in config
        assert "db_name" in config


# =============================================================================
# Test load_config_from_file (main entry point)
# =============================================================================


class TestLoadConfigFromFile:
    """Test load_config_from_file main entry point."""

    def test_main_entry_point_with_file(self, tmp_path):
        """Test main entry point loads file correctly."""
        config_file = tmp_path / "main_config.py"
        config_file.write_text("""
SERVER_HOST = "0.0.0.0"
SERVER_PORT = 8080
WORKERS = 4
""")

        config = load_config_from_file(
            str(config_file), keys=["server_host", "server_port", "workers"]
        )

        assert config["server_host"] == "0.0.0.0"
        assert config["server_port"] == 8080
        assert config["workers"] == 4

    def test_main_entry_point_with_module(self, tmp_path, monkeypatch):
        """Test main entry point loads module correctly."""
        module_dir = tmp_path / "modules"
        module_dir.mkdir()

        config_module = module_dir / "server_config.py"
        config_module.write_text("""
listen_host = "0.0.0.0"
listen_port = 9000
""")

        monkeypatch.syspath_prepend(str(module_dir))

        config = load_config_from_file(
            "python:server_config", keys=["listen_host", "listen_port"]
        )

        assert config["listen_host"] == "0.0.0.0"
        assert config["listen_port"] == 9000

    def test_empty_keys_returns_empty_dict(self, tmp_path):
        """Test that requesting no keys returns empty dict."""
        config_file = tmp_path / "config.py"
        config_file.write_text("""
VAR1 = "value1"
VAR2 = "value2"
""")

        config = load_config_from_file(str(config_file), keys=[])

        assert config == {}


# =============================================================================
# Integration Tests
# =============================================================================


class TestConfigLoaderIntegration:
    """Integration tests for real-world config loading scenarios."""

    def test_load_database_config(self, tmp_path):
        """Test loading typical database configuration."""
        config_file = tmp_path / "db.conf.py"
        config_file.write_text("""
# Database Configuration
DB_PARAMS = {
    'host': 'localhost',
    'port': 5432,
    'database': 'pyjobby',
    'user': 'pyjobby_user',
    'password': 'secret',
}

POOL_SIZE = 10
POOL_TIMEOUT = 30
""")

        config = load_config_from_file(
            str(config_file), keys=["db_params", "pool_size", "pool_timeout"]
        )

        assert isinstance(config["db_params"], dict)
        assert config["db_params"]["host"] == "localhost"
        assert config["db_params"]["port"] == 5432
        assert config["pool_size"] == 10
        assert config["pool_timeout"] == 30

    def test_load_worker_config(self, tmp_path):
        """Test loading typical worker configuration."""
        config_file = tmp_path / "worker.conf.py"
        config_file.write_text("""
QUEUE = "high-priority"
PRIO = 500
CHECK_INTERVAL = 1.0
BATCH_SIZE = 100
CAPABILITIES = ["email", "sms", "webhook"]
""")

        config = load_config_from_file(
            str(config_file),
            keys=["queue", "prio", "check_interval", "batch_size", "capabilities"],
        )

        assert config["queue"] == "high-priority"
        assert config["prio"] == 500
        assert config["check_interval"] == 1.0
        assert config["batch_size"] == 100
        assert config["capabilities"] == ["email", "sms", "webhook"]
