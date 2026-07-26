"""
Comprehensive tests for configloader.py - configuration loading utilities.
Using LIVE file operations with NO MOCKS for maximum correctness guarantees!
"""

import os
import sys
import tempfile

import pytest

from pyjobby.configloader import (
    chdir_addpath,
    get_config_from_filename,
    get_config_from_module_name,
    load_config_from_file,
    load_config_from_module_name_or_filename,
)


class TestChdirAddpath:
    """Test directory changing and path manipulation - covers lines 16-20."""

    def test_chdir_addpath_changes_directory(self):
        """Test that chdir_addpath changes to the specified directory."""
        original_dir = os.getcwd()
        with tempfile.TemporaryDirectory() as tmpdir:
            chdir_addpath(tmpdir)
            # realpath: macOS tempdirs live behind the /var -> /private/var symlink
            assert os.path.realpath(os.getcwd()) == os.path.realpath(tmpdir)
            assert tmpdir in sys.path
            # Restore original directory
            os.chdir(original_dir)
            sys.path.remove(tmpdir)

    def test_chdir_addpath_adds_to_sys_path(self):
        """Test that path is added to sys.path - covers lines 19-20."""
        original_dir = os.getcwd()
        with tempfile.TemporaryDirectory() as tmpdir:
            # Ensure it's not already in path
            if tmpdir in sys.path:
                sys.path.remove(tmpdir)

            chdir_addpath(tmpdir)
            assert tmpdir in sys.path
            # Check it's at the front
            assert sys.path[0] == tmpdir

            # Restore
            os.chdir(original_dir)
            sys.path.remove(tmpdir)


class TestGetConfigFromFilename:
    """Test loading config from Python files - covers lines 24-48."""

    def test_get_config_from_py_file(self):
        """Test loading config from .py file - covers lines 31-41."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as f:
            f.write('db_params = {"host": "localhost", "port": 5432}\n')
            f.write('web_listen = {"port": 8080}\n')
            f.write("test_value = 42\n")
            f.name
            f.flush()

            try:
                config = get_config_from_filename(f.name)
                assert config["db_params"] == {"host": "localhost", "port": 5432}
                assert config["web_listen"] == {"port": 8080}
                assert config["test_value"] == 42
            finally:
                os.unlink(f.name)

    def test_get_config_from_nonexistent_file_raises(self):
        """Test that non-existent file raises RuntimeError - covers line 24-25."""
        with pytest.raises(RuntimeError) as excinfo:
            get_config_from_filename("/nonexistent/path/config.py")
        assert "doesn't exist" in str(excinfo.value)

    def test_get_config_from_non_py_extension(self):
        """Test loading config from non-.py file - covers lines 34-37."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".conf", delete=False) as f:
            f.write('custom_setting = "test"\n')
            f.flush()

            try:
                config = get_config_from_filename(f.name)
                assert config["custom_setting"] == "test"
            finally:
                os.unlink(f.name)


class TestGetConfigFromModuleName:
    """Test loading config from Python module - covers line 52."""

    def test_get_config_from_module_name(self):
        """Test loading config from module name - covers line 52."""
        # Use a built-in module
        config = get_config_from_module_name("os")
        assert "getcwd" in config
        assert callable(config["getcwd"])


class TestLoadConfigFromModuleNameOrFilename:
    """Test combined loader - covers lines 63-74."""

    def test_load_from_python_prefix(self):
        """Test loading with python: prefix - covers lines 63-65."""
        config = load_config_from_module_name_or_filename(
            "python:os.path", keys={"join", "exists", "dirname"}
        )
        assert "join" in config
        assert "exists" in config
        assert "dirname" in config

    def test_load_from_file_prefix(self):
        """Test loading with file: prefix - covers lines 67-68."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as f:
            f.write('db_params = {"host": "test"}\n')
            f.write("web_listen = None\n")
            f.flush()

            try:
                config = load_config_from_module_name_or_filename(
                    f"file:{f.name}", keys={"db_params", "web_listen"}
                )
                assert config["db_params"] == {"host": "test"}
                assert config["web_listen"] is None
            finally:
                os.unlink(f.name)

    def test_load_from_filename_no_prefix(self):
        """Test loading without prefix - covers lines 69-70."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as f:
            f.write('setting1 = "value1"\n')
            f.write("setting2 = 123\n")
            f.flush()

            try:
                config = load_config_from_module_name_or_filename(
                    f.name, keys={"setting1", "setting2"}
                )
                assert config["setting1"] == "value1"
                assert config["setting2"] == 123
            finally:
                os.unlink(f.name)


class TestLoadConfigFromFile:
    """Test main entry point - covers line 86."""

    def test_load_config_from_file(self):
        """Test main entry point function - covers line 86."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as f:
            f.write('db_params = {"host": "localhost", "user": "test"}\n')
            f.write('web_listen = {"port": 9000}\n')
            f.write('extra = "ignored"\n')
            f.flush()

            try:
                config = load_config_from_file(f.name, keys={"db_params", "web_listen"})
                assert "db_params" in config
                assert "web_listen" in config
                assert "extra" not in config  # Should be filtered out
            finally:
                os.unlink(f.name)
