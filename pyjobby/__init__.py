try:
    from importlib import metadata
except ImportError:
    # Running on pre-3.8 Python; use importlib-metadata package
    import importlib_metadata as metadata  # type: ignore

try:
    __version__ = metadata.version(__name__)
except:
    __version__ = "dev"
