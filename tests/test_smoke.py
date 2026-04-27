import insto


def test_package_imports() -> None:
    assert insto is not None


def test_version_is_pep440_string() -> None:
    """Smoke check: insto exposes a sane __version__ string. Pinning to a
    specific value here would just churn on every release; the actual version
    is the single source of truth in `insto/_version.py`."""
    import re

    assert isinstance(insto.__version__, str)
    assert re.fullmatch(r"\d+\.\d+\.\d+(?:[abc]\d+|rc\d+|\.dev\d+)?", insto.__version__)
