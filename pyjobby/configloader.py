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
    app_version = "2026.07.28+a1b2c3d"
    liveness_grace_seconds = 60

    [db_params]
    host = "postgres.internal"
    port = 5432
    database = "pyjobby"
    user = "pyjobby"
    password = "${PYJOBBY_DB_PASSWORD}"

    [web_listen]
    sites = [{ host = "127.0.0.1", port = 8080 }]
    paths = []

Note the ORDER: every bare key comes BEFORE the first table header, and they
have to. TOML has no way back out to the root, so a bare key written after
``[db_params]`` is a db_params key -- which is how a deployment ends up with a
priority ceiling that is silently an asyncpg connect() keyword nobody passes.
``load_config_from_file`` refuses that arrangement by name rather than letting
it load.

``db_params`` is asyncpg.connect() keyword arguments and only those
(``ASYNCPG_CONNECT_KEYS``).
Optional keys are simply omitted (TOML has no null): a missing
``web_listen`` table means no web listener, a missing ``prio_ceiling``
means the platform default, and a missing ``app_version`` means this
deployment does not pin work to a code version at all.

``app_version`` is declared HERE, once, because both halves of the pin read
it from the same file: ``pj --app-version`` defaults to it (what the workers
advertise) and ``JobClient.from_config`` defaults to it (what an enqueue
stamps). Two places to write the same string is one place to forget it.

``liveness_grace_seconds`` is here for a stronger version of that argument:
it has FIVE readers. ``pj-monitor`` sweeps by it (requeueing a silent
worker's in-flight jobs) while ``pj-admin doctor``, ``/metrics``, the web
admin's workers page and the WebSocket dashboard all merely REPORT by it --
so ``pj-monitor --liveness-grace 300`` alone produces a fleet whose monitor
considers a worker alive and whose every UI calls it dead. The flag remains,
for a one-off run; the file is how a deployment says it once.
"""

from __future__ import annotations

import inspect
import os
import re
import tomllib
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

import asyncpg  # type: ignore[import-untyped]


class ConfigError(RuntimeError):
    """Raised when a config file cannot be loaded or is invalid.

    Subclasses RuntimeError so callers catching RuntimeError keep working.
    Library code raises; CLI entry points decide whether to exit."""


#: A value that is EXACTLY an environment reference. Deliberately the whole
#: string rather than embedded interpolation: "${A}:${B}" templating invites
#: quoting bugs, while a value that IS a secret reference is unambiguous.
#: \Z, not $ -- $ also matches BEFORE a trailing newline, so "${VAR}\n"
#: would substitute and silently drop the newline.
_ENV_REF = re.compile(r"^\$\{([A-Za-z_][A-Za-z0-9_]*)\}\Z")

#: A config file larger than this is refused unread. A settings file is
#: kilobytes; a megabytes-large one is a mistake or a resource-exhaustion
#: attempt, and reading it into memory to then reject it is the exhaustion.
_MAX_CONFIG_BYTES = 1024 * 1024

#: Every top-level key a pyjobby config may define. A key outside this set is
#: refused rather than skipped: each daemon asks for the SUBSET of keys it
#: cares about, so an unknown key looks exactly like a key this particular
#: process did not want -- and `prio_ceilng = 100` would then silently leave
#: the setting at its default in every process, forever, with the file
#: sitting there looking like it said otherwise.
KNOWN_TOP_LEVEL_KEYS = frozenset(
    {
        "app_version",
        "db_params",
        "liveness_grace_seconds",
        "prio_ceiling",
        "web_listen",
    }
)

#: Every key ``[db_params]`` may hold: the ``asyncpg.connect()`` keyword
#: arguments, and nothing else -- the table is passed to it verbatim.
#:
#: Checked for the same reason the top-level set is, and against a failure that
#: is easier to write than a typo. TOML HAS NO WAY BACK OUT TO THE ROOT: a bare
#: key written after the ``[db_params]`` header belongs to db_params, so
#: ``prio_ceiling = 1000`` placed under the connection table -- the natural
#: reading order, and what this project's own shipped example did -- is not the
#: deployment's priority ceiling. It is a connect() keyword that does not exist,
#: every enqueue silently keeps the default ceiling, and the file sits there
#: looking like it said otherwise. Refused with the key named and its real home
#: named beside it.
#:
#: Deliberately generous: ``dsn`` and the pooling/TLS/statement-cache knobs are
#: in, because an operator who needs one of them should not have to patch this
#: list. ``loop`` is not: a config file cannot name an event loop.
#:
#: THE KEYS ARE LOWERCASE and the check is case-SENSITIVE, because
#: ``asyncpg.connect(**db_params)`` is: ``Host = "..."`` is not a spelling of
#: ``host``, it is a keyword argument that does not exist. Folding case here
#: only moved the failure -- validation passed and connect() raised TypeError
#: from inside a daemon's startup instead of the loader naming the key.
#:
#: DERIVED FROM THE FUNCTION rather than transcribed from its docs, because
#: this project pins no upper bound on asyncpg (see pyproject): a hand-written
#: allow-list is a list that starts REFUSING a keyword the day a new release
#: adds one, and the refusal arrives as a ConfigError blaming the operator's
#: file. The literal below is the fallback for an asyncpg whose signature
#: cannot be introspected (a C accelerator, a decorator that drops the
#: metadata), and tests/test_configloader.py binds it to the derived set so it
#: cannot rot into a narrower list than the one that ships.
_ASYNCPG_CONNECT_KEYS_FALLBACK: frozenset[str] = frozenset(
    {
        "command_timeout",
        "connection_class",
        "database",
        "direct_tls",
        "dsn",
        "gsslib",
        "host",
        "krbsrvname",
        "max_cacheable_statement_size",
        "max_cached_statement_lifetime",
        "passfile",
        "password",
        "port",
        "record_class",
        "server_settings",
        "service",
        "servicefile",
        "ssl",
        "statement_cache_size",
        "target_session_attrs",
        "timeout",
        "user",
    }
)

#: ``loop`` is a parameter of ``asyncpg.connect`` and is deliberately NOT a
#: config key: a TOML file cannot name an event loop, and a string there would
#: reach asyncpg as one.
_NOT_A_CONFIG_KEY: frozenset[str] = frozenset({"loop"})


def _derive_asyncpg_connect_keys() -> frozenset[str]:
    """Every keyword ``asyncpg.connect`` accepts, minus the ones a file cannot
    hold. Falls back to the shipped literal if the signature is unreadable."""
    try:
        parameters = inspect.signature(asyncpg.connect).parameters
    except TypeError, ValueError:  # pragma: no cover - defensive
        return _ASYNCPG_CONNECT_KEYS_FALLBACK
    derived = (
        frozenset(
            name
            for name, p in parameters.items()
            if p.kind
            in (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY)
        )
        - _NOT_A_CONFIG_KEY
    )
    return derived or _ASYNCPG_CONNECT_KEYS_FALLBACK


ASYNCPG_CONNECT_KEYS: frozenset[str] = _derive_asyncpg_connect_keys()


def describe_db_target(target: Mapping[str, Any] | str | None) -> str:
    """Name a connection target for an operator-facing message.

    Host, port and database only. A failure message has to say WHICH database
    could not be reached -- an operator running four deployments cannot act on
    "connection refused" -- and it must do that without printing the password
    that both a db_params table and a DSN string carry.
    """
    if target is None:
        return "the connected database"
    if isinstance(target, str):
        # everything before the last '@' in a DSN is userinfo, i.e. secret
        return target.rsplit("@", 1)[-1]
    host = target.get("host", "localhost")
    port = target.get("port", 5432)
    database = target.get("database", "?")
    return f"{host}:{port}/{database}"


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
                f"{source}: references environment variable {name!r}, which is not set"
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

    A top-level key outside ``KNOWN_TOP_LEVEL_KEYS`` is a ConfigError naming
    it, whether or not this caller asked for it -- see that constant.
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
        size = path.stat().st_size
    except OSError as e:
        raise ConfigError(f"Failed to read config file: {filename}: {e}") from e
    if size > _MAX_CONFIG_BYTES:
        raise ConfigError(
            f"{filename!r} is {size} bytes; a pyjobby config is refused above "
            f"{_MAX_CONFIG_BYTES} bytes (it is a settings file, not a dataset)"
        )

    try:
        with path.open("rb") as fh:
            raw = tomllib.load(fh)
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"Failed to parse config file {filename}: {e}") from e
    except (OSError, UnicodeDecodeError, RecursionError) as e:
        # UnicodeDecodeError: a non-UTF-8 file; RecursionError: pathologically
        # nested TOML. Both must arrive as ConfigError (RuntimeError) so the
        # `except RuntimeError` guard in every CLI entry point catches them
        # instead of a raw traceback escaping.
        raise ConfigError(f"Failed to read config file: {filename}: {e}") from e

    unknown = sorted(k for k in raw if k.lower() not in KNOWN_TOP_LEVEL_KEYS)
    if unknown:
        known = ", ".join(sorted(KNOWN_TOP_LEVEL_KEYS))
        raise ConfigError(
            f"unknown key {unknown[0]!r} in {filename}; known keys are "
            f"{known}. A typo here silently disables the setting, so it is "
            f"refused rather than skipped."
        )

    db_params = raw.get("db_params")
    if isinstance(db_params, dict):
        # Case-SENSITIVE, unlike the top-level check above: this table is
        # passed to asyncpg.connect() verbatim as **kwargs, and `Host` is not
        # a spelling of `host` there -- it is a keyword that does not exist.
        # Folded, `Host = "db"` passed validation here and then raised
        # TypeError from inside connect(), in a daemon's startup, with no
        # mention of the config file.
        misplaced = sorted(k for k in db_params if k not in ASYNCPG_CONNECT_KEYS)
        if misplaced:
            key = misplaced[0]
            if key.lower() in KNOWN_TOP_LEVEL_KEYS:
                home = "a TOP-LEVEL setting: move it ABOVE the [db_params] header"
            elif key.lower() in ASYNCPG_CONNECT_KEYS:
                home = (
                    f"the wrong case for one: asyncpg.connect() keywords are "
                    f"lowercase, so write {key.lower()!r}"
                )
            else:
                home = "not an asyncpg.connect() keyword"
            raise ConfigError(
                f"{key!r} is inside [db_params] in {filename}, and it is {home}. "
                f"TOML has no way back out to the root, so every bare key after "
                f"a table header belongs to that table -- which is why a "
                f"setting written under the connection block is silently not "
                f"the setting it looks like."
            )

    wanted = {k.lower() for k in keys}
    try:
        return {
            k.lower(): _substitute_env(v, source=filename)
            for k, v in raw.items()
            if k.lower() in wanted
        }
    except RecursionError as e:
        raise ConfigError(
            f"Failed to read config file: {filename}: structure too deeply nested ({e})"
        ) from e
