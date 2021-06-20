# Adapted from:
# https://github.com/benoitc/gunicorn/blob/d1f0f11b7b7d00f74dc22ead8e62d322eb128431/gunicorn/app/base.py

# This file is part of gunicorn released under the MIT license.
import importlib.util
import importlib.machinery
import os
import sys
import traceback
from typing import Iterable, Any, Dict


def chdir_addpath(path: str) -> None:
    # chdir to the configured path before loading,
    # default is the current dir
    os.chdir(path)

    # add the path to sys.path
    if path not in sys.path:
        sys.path.insert(0, path)


def get_config_from_filename(filename: str) -> Dict[str, Any]:
    if not os.path.exists(filename):
        raise RuntimeError("%r doesn't exist" % filename)

    ext = os.path.splitext(filename)[1]

    try:
        module_name = "__config__"
        if ext in [".py", ".pyc"]:
            spec = importlib.util.spec_from_file_location(module_name, filename)
        else:
            loader_ = importlib.machinery.SourceFileLoader(module_name, filename)
            spec = importlib.util.spec_from_file_location(
                module_name, filename, loader=loader_
            )

        mod = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = mod
        spec.loader.exec_module(mod)  # type: ignore
    except Exception:
        print("Failed to read config file: %s" % filename, file=sys.stderr)
        traceback.print_exc()
        sys.stderr.flush()
        sys.exit(1)

    return vars(mod)


def get_config_from_module_name(module_name: str) -> Dict[str, Any]:
    return vars(importlib.import_module(module_name))


def load_config_from_module_name_or_filename(
    location: str, keys: Iterable[str]
) -> Dict[str, Any]:
    """
    Loads the configuration file: the file is a python file, otherwise raise an RuntimeError
    Exception or stop the process if the configuration file contains a syntax error.
    """

    if location.startswith("python:"):
        module_name = location[len("python:") :]
        cfg = get_config_from_module_name(module_name)
    else:
        if location.startswith("file:"):
            filename = location[len("file:") :]
        else:
            filename = location

        cfg = get_config_from_filename(filename)

    return {k.lower(): v for k, v in cfg.items() if k.lower() in keys}


def load_config_from_file(filename: str, keys: Iterable[str]) -> Dict[str, Any]:
    """Main entry point for loading config file.

    An iterable of 'keys' must be provided to limit which parts of the module
    dict gets returned (otherwise dozens of dunders and copyright and exit
    and other things would be in the resulting dict).

    Prefix filename with "python:" to load as a python module name or
    prefix with "file:" to load as a filename (also loads with no prefix)"""
    return load_config_from_module_name_or_filename(location=filename, keys=keys)
