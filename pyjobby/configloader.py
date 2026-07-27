from __future__ import annotations

# Adapted from:
# https://github.com/benoitc/gunicorn/blob/d1f0f11b7b7d00f74dc22ead8e62d322eb128431/gunicorn/app/base.py
# This file is part of gunicorn released under the MIT license.
import importlib.machinery
import importlib.util
import os
import sys
from collections.abc import Iterable
from pathlib import Path
from typing import Any


class ConfigError(RuntimeError):
    """Raised when a config file cannot be loaded or executed.

    Subclasses RuntimeError so callers catching RuntimeError keep working.
    Library code raises; CLI entry points decide whether to exit."""


def chdir_addpath(path: str) -> None:
    # chdir to the configured path before loading,
    # default is the current dir
    os.chdir(path)

    # add the path to sys.path
    if path not in sys.path:
        sys.path.insert(0, path)


def get_config_from_filename(filename: str) -> dict[str, Any]:
    if not Path(filename).exists():
        raise RuntimeError(f"{filename!r} doesn't exist")

    ext = Path(filename).suffix

    try:
        module_name = "__config__"
        if ext in [".py", ".pyc"]:
            spec = importlib.util.spec_from_file_location(module_name, filename)
        else:
            loader_ = importlib.machinery.SourceFileLoader(module_name, filename)
            spec = importlib.util.spec_from_file_location(
                module_name, filename, loader=loader_
            )

        assert spec is not None
        mod = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = mod
        spec.loader.exec_module(mod)  # type: ignore[union-attr]
    except Exception as e:
        raise ConfigError(f"Failed to read config file: {filename}: {e}") from e

    return vars(mod)


def get_config_from_module_name(module_name: str) -> dict[str, Any]:
    return vars(importlib.import_module(module_name))


def load_config_from_module_name_or_filename(
    location: str, keys: Iterable[str]
) -> dict[str, Any]:
    """
    Loads the configuration file: the file is a python file, otherwise raise an RuntimeError
    Exception or stop the process if the configuration file contains a syntax error.
    """

    if location.startswith("python:"):
        module_name = location[len("python:") :]
        cfg = get_config_from_module_name(module_name)
    else:
        filename = location.removeprefix("file:")

        cfg = get_config_from_filename(filename)

    return {k.lower(): v for k, v in cfg.items() if k.lower() in keys}


def load_config_from_file(filename: str, keys: Iterable[str]) -> dict[str, Any]:
    """Main entry point for loading config file.

    An iterable of 'keys' must be provided to limit which parts of the module
    dict gets returned (otherwise dozens of dunders and copyright and exit
    and other things would be in the resulting dict).

    Prefix filename with "python:" to load as a python module name or
    prefix with "file:" to load as a filename (also loads with no prefix)"""
    return load_config_from_module_name_or_filename(location=filename, keys=keys)
