__version__ = "0.3.11"


def franky_version() -> str:
    """Resolve the running version. Prefer installed package metadata (what
    `pip install franky-agent==X` pins and what the published image tag is keyed to); fall back
    to __version__ when metadata is absent (running from a source checkout with no install). If
    both resolve and DISAGREE it's a dev-env skew (stale editable metadata vs a bumped
    __version__) - warn to stderr and trust metadata."""
    import sys
    import importlib.metadata

    from ._install import DIST_NAME

    try:
        meta = importlib.metadata.version(DIST_NAME)
    except Exception:
        return __version__
    if meta != __version__:
        print(
            f"franky: version skew - metadata={meta}, __version__={__version__} (trusting metadata)",
            file=sys.stderr,
        )
    return meta
