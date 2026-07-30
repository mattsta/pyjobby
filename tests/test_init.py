"""
Tests for pyjobby/__init__.py module initialization.

Covers version detection and fallback mechanisms.
"""

import pathlib
from unittest.mock import patch

import pyjobby


class TestVersionDetection:
    """Test version detection in __init__.py"""

    def test_version_loaded_successfully(self):
        """Test that __version__ is loaded from package metadata."""
        import pyjobby

        # Should have a version (either from metadata or "dev")
        assert hasattr(pyjobby, "__version__")
        assert isinstance(pyjobby.__version__, str)
        assert len(pyjobby.__version__) > 0

    def test_version_fallback_on_metadata_error(self):
        """Test __version__ fallback when metadata.version() fails - covers lines 9-10."""
        # We need to test the fallback path by simulating import failure
        # This is tricky because the module is already imported

        # Instead, let's test the logic by importing the module code
        from importlib import metadata

        with patch("importlib.metadata.version") as mock_version:
            # The fallback catches the specific PackageNotFoundError (not
            # arbitrary exceptions — library code must not swallow those)
            mock_version.side_effect = metadata.PackageNotFoundError("pyjobby")

            # Re-import the module to trigger the fallback
            import importlib

            import pyjobby

            importlib.reload(pyjobby)

            # Should fall back to "dev"
            assert pyjobby.__version__ == "dev"


class TestTheErrorTaxonomyIsImportable:
    """Every exception the docs tell a caller to catch must be a
    ``from pyjobby import ...``, and the family relationships must be real.

    Both halves were wrong. ``JobTimeout`` was documented as the type that
    tells "the job ran out of its configured time" apart from a
    ``TimeoutError`` the job raised itself -- and it was not exported, so the
    only way to write that ``except`` was to import from ``pyjobby.dxe``,
    which no documentation mentioned. And the three recorded failures shared a
    docstring paragraph explaining that they are recorded-not-signalled
    without sharing a base class, so a caller who wanted "any durable failure
    the platform diagnosed" had to enumerate them and be wrong the day a
    fourth arrived.
    """

    def test_every_documented_error_is_a_top_level_name(self):
        import pyjobby

        for name in (
            "DXEError",
            "JobTimeout",
            "NondeterminismError",
            "SpeculativeEnqueueExhausted",
            "StaleExecutionError",
            "StepFailure",
            "StepTimeoutError",
            "StreamClosedError",
        ):
            assert hasattr(pyjobby, name), f"pyjobby.{name} is not importable"
            assert name in pyjobby.__all__, f"pyjobby.{name} is not in __all__"

    def test_the_recorded_failures_share_a_base_and_the_signals_do_not(self):
        """``StepFailure`` is what a step's ``error`` column gets written from;
        ``DXEError`` is a control-flow signal that bypasses recording. A member
        of one that was also a member of the other would make
        ``except StepFailure`` swallow a supersession."""
        from pyjobby import (
            DXEError,
            JobTimeout,
            NondeterminismError,
            StaleExecutionError,
            StepFailure,
            StepTimeoutError,
            StreamClosedError,
        )

        for recorded in (StepTimeoutError, StreamClosedError, JobTimeout):
            assert issubclass(recorded, StepFailure)
            assert not issubclass(recorded, DXEError)
        for signal in (StaleExecutionError, NondeterminismError):
            assert issubclass(signal, DXEError)
            assert not issubclass(signal, StepFailure)

    def test_a_step_timeout_is_not_a_timeout_error(self):
        """Deliberate: job code raises ``TimeoutError`` on its own account all
        the time, and a handler for "the platform's budget expired" must not
        catch those."""
        from pyjobby import JobTimeout, StepTimeoutError

        assert not issubclass(StepTimeoutError, TimeoutError)
        assert not issubclass(JobTimeout, TimeoutError)

    def test_the_speculative_refusal_is_still_a_runtime_error(self):
        """Both speculative loops raised a bare ``RuntimeError`` before the
        named type existed, so every ``except RuntimeError`` written against
        them has to keep working -- and the new type carries the key so a
        handler need not parse the sentence to find out which one lost."""
        from pyjobby import SpeculativeEnqueueExhausted

        assert issubclass(SpeculativeEnqueueExhausted, RuntimeError)
        raised = SpeculativeEnqueueExhausted("identity_key", "order:1", 5, "boom")
        assert (raised.kind, raised.key, raised.attempts) == (
            "identity_key",
            "order:1",
            5,
        )


class TestTheEnqueueRulesSplitIsAnInternalMove:
    """``pyjobby.enqueue_rules`` was carved out of ``pyjobby.client``, and it
    is the ONE home for what it owns.

    The carve-out shipped with every name re-exported at its old
    ``pyjobby.client`` path "for API compatibility". There is nothing to be
    compatible with: this is a forward-only framework with no live
    deployments, so the re-export bought no user anything and cost the
    codebase two import paths for one constant -- two things to keep in step,
    and one of them to grep past. It is gone.

    The layering is what the split was actually for: the new module imports
    nothing from the package, which is what lets ``db.fork_job`` and ``dag``
    reach a validator at the TOP of the file instead of with a
    function-local ``from .client import`` written to dodge an import cycle.
    """

    def test_the_rules_have_exactly_one_import_path(self):
        """``client`` does not re-export them, and nothing asks it to.

        ``SpeculativeEnqueueExhausted`` is the one deliberate second path,
        and it is ``pyjobby`` itself rather than ``pyjobby.client``: an
        application has to be able to CATCH it without importing a rules
        module. Everything else -- the bounds, the validators, the refusal
        messages -- is ``pyjobby.enqueue_rules`` and nowhere else.
        """
        import pyjobby
        from pyjobby import client, enqueue_rules

        owned = {
            name
            for name in dir(enqueue_rules)
            # `annotations` is the __future__ import, not a rule
            if not name.startswith("__") and name != "annotations"
        }
        assert len(owned) > 15, "the sweep found almost nothing; check the filter"

        # client legitimately USES several of these; what it must not do is
        # hold a name it neither uses nor defines purely to re-export it.
        re_exported = {
            name
            for name in owned
            if getattr(client, name, None) is getattr(enqueue_rules, name)
        }
        unused_by_client = {
            name for name in re_exported if client.__dict__.get(name) is not None
        }
        source = pathlib.Path(client.__file__).read_text()
        for name in sorted(unused_by_client):
            # one mention is the import line itself; a genuine user has more
            assert source.count(name) > 1, (
                f"client imports {name} without using it -- that is a "
                f"re-export, and enqueue_rules is the only import path"
            )

        assert pyjobby.SpeculativeEnqueueExhausted is (
            enqueue_rules.SpeculativeEnqueueExhausted
        )

    def test_the_key_length_bound_has_one_name(self):
        """``MAX_PARTITION_KEY_LENGTH`` was an alias of ``MAX_KEY_LENGTH``.

        One bound for all four caller-chosen keys, and therefore one name for
        it. A second name for the same number is a second thing to find when
        the number changes, and the alias existed only because
        ``partition_key`` documented the reasoning first.
        """
        from pyjobby import enqueue_rules

        assert enqueue_rules.MAX_KEY_LENGTH == 256
        assert not hasattr(enqueue_rules, "MAX_PARTITION_KEY_LENGTH")

    def test_no_module_in_the_package_reaches_the_rules_through_client(self):
        """The sweep, so the next importer cannot reintroduce the second path.

        Read out of the source rather than from ``sys.modules``: what is under
        test is what the files SAY, not what happens to be importable.
        """
        import ast

        from pyjobby import enqueue_rules

        owned = {
            name
            for name in dir(enqueue_rules)
            if not name.startswith("__") and name != "annotations"
        }
        package = pathlib.Path(pyjobby.__file__).parent
        offenders = []
        for source in sorted(package.rglob("*.py")):
            tree = ast.parse(source.read_text())
            for node in ast.walk(tree):
                if not isinstance(node, ast.ImportFrom):
                    continue
                if node.module not in ("client", "pyjobby.client"):
                    continue
                for alias in node.names:
                    if alias.name in owned:
                        offenders.append(
                            f"{source.relative_to(package)}:{node.lineno}: {alias.name}"
                        )
        assert not offenders, (
            "these import an enqueue rule through pyjobby.client; the one "
            "path is pyjobby.enqueue_rules:\n" + "\n".join(offenders)
        )

    def test_the_rules_module_imports_nothing_from_the_package(self):
        """The whole reason it can be imported at the top of db.py and dag.py.

        Read out of the source rather than from ``sys.modules``: an import
        that happens to be satisfied already at test time would pass a runtime
        check while still being a cycle for anyone importing in another order.
        """
        import ast
        import pathlib

        import pyjobby.enqueue_rules as rules

        tree = ast.parse(pathlib.Path(rules.__file__).read_text())
        intra_package = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and (node.level or 0) > 0
        ]
        assert not intra_package, (
            f"enqueue_rules imports from the package "
            f"({[n.module for n in intra_package]}); it is the leaf every "
            f"other module has to be able to import unconditionally"
        )

    def test_the_layering_inversion_is_gone(self):
        """``db`` and ``dag`` reach the rules at the top of the file.

        Both used to import them INSIDE a function -- the shape a cycle
        forces, and the shape that hides a dependency from every reader and
        every tool.
        """
        import ast
        import pathlib

        from pyjobby import dag, db

        for module in (db, dag):
            tree = ast.parse(pathlib.Path(module.__file__).read_text())
            # Inside a function BODY specifically. A module-level
            # `if TYPE_CHECKING: from .client import JobClient` is not the
            # shape under test: it costs nothing at runtime and is how a type
            # annotation is supposed to reach a class it must not import.
            deferred = [
                child
                for node in ast.walk(tree)
                if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
                for child in ast.walk(node)
                if isinstance(child, ast.ImportFrom) and child.module == "client"
            ]
            assert not deferred, (
                f"{module.__name__} still imports from .client inside a "
                f"function body; that import is the cycle enqueue_rules exists "
                f"to break"
            )
