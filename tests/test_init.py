"""
Tests for pyjobby/__init__.py module initialization.

Covers version detection and fallback mechanisms.
"""

from unittest.mock import patch


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
