"""Configuration loading: a TOML file, read as DATA.

The config file is ``pyjobby.toml`` — parsed with the standard library's
``tomllib``, never executed. A config format that runs code means every
daemon executes arbitrary Python from whatever path it was pointed at,
which is a remote-code-execution primitive wearing a settings file; inert
data cannot do that, and TOML is the format the Python packaging ecosystem
already standardized on.

Secrets still come from the environment, explicitly: any string value of
the exact form ``"${VAR_NAME}"`` is replaced with ``os.environ["VAR_NAME"]``
at load time, and a reference to an unset variable is a loud ConfigError
naming the variable — a config that silently loaded with a missing secret
would fail later, further from the cause.

::

    # pyjobby.toml
    prio_ceiling = 1000

    [db_params]
    host = "postgres.internal"
    port = 5432
    database = "pyjobby"
    user = "pyjobby"
    password = "${PYJOBBY_DB_PASSWORD}"

    [web_listen]
    sites = [{ host = "127.0.0.1", port = 8080 }]
    paths = []

``db_params`` is asyncpg.connect() keyword arguments and only those.
Optional keys are simply omitted (TOML has no null): a missing
``web_listen`` table means no web listener, a missing ``prio_ceiling``
means the platform default.
"""

from __future__ import annotations

import os
import re
import tomllib
from collections.abc import Iterable
from pathlib import Path
from typing import Any


class ConfigError(RuntimeError):
    """Raised when a config file cannot be loaded or is invalid.

    Subclasses RuntimeError so callers catching RuntimeError keep working.
    Library code raises; CLI entry points decide whether to exit."""


#: A value that is EXACTLY an environment reference. Deliberately the whole
#: string rather than embedded interpolation: "${A}:${B}" templating invites
#: quoting bugs, while a value that IS a secret reference is unambiguous.
_ENV_REF = re.compile(r"^\$\{([A-Za-z_][A-Za-z0-9_]*)\}$")


def _substitute_env(value: Any, *, source: str) -> Any:
    """Replace ``"${VAR}"`` strings with the environment's value, recursively
    through tables and arrays. Unset variables are a loud error naming the
    variable and the file — a config that silently loads with a missing
    secret fails later, further from the cause."""
    if isinstance(value, str):
        match = _ENV_REF.match(value)
        if match is None:
            return value
        name = match.group(1)
        if name not in os.environ:
            raise ConfigError(
                f"{source}: references environment variable {name!r}, "
                f"which is not set"
            )
        return os.environ[name]
    if isinstance(value, dict):
        return {k: _substitute_env(v, source=source) for k, v in value.items()}
    if isinstance(value, list):
        return [_substitute_env(v, source=source) for v in value]
    return value


def load_config_from_file(filename: str, keys: Iterable[str]) -> dict[str, Any]:
    """Load ``filename`` as TOML and return the requested top-level ``keys``.

    Keys absent from the file are absent from the result (callers use
    ``.get``), so optional settings need no null spelling. A ``.py`` config
    is refused by name with the reason: the executable-config format it
    implies is exactly what this loader exists not to be.
    """
    path = Path(filename)
    if path.suffix == ".py":
        raise ConfigError(
            f"{filename!r} is a Python file; pyjobby config is TOML "
            f"(pyjobby.toml), read as data and never executed. Move the "
            f"same settings into TOML tables — secrets become "
            f'"${{ENV_VAR}}" references.'
        )
    if not path.exists():
        raise ConfigError(f"{filename!r} doesn't exist")

    try:
        with path.open("rb") as fh:
            raw = tomllib.load(fh)
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"Failed to parse config file {filename}: {e}") from e
    except OSError as e:
        raise ConfigError(f"Failed to read config file: {filename}: {e}") from e

    wanted = {k.lower() for k in keys}
    return {
        k.lower(): _substitute_env(v, source=filename)
        for k, v in raw.items()
        if k.lower() in wanted
    }
