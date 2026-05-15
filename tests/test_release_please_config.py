"""Regression checks for release-please configuration."""

from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_release_please_extra_files_use_supported_types() -> None:
    config = json.loads((ROOT / ".release-please-config.json").read_text())

    extra_files = config["packages"]["."]["extra-files"]

    assert {"type": "generic", "path": "insto/_version.py"} in extra_files
    assert all(item.get("type") != "python" for item in extra_files if isinstance(item, dict))


def test_project_version_files_are_in_sync() -> None:
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text())
    version_file = (ROOT / "insto/_version.py").read_text()

    match = re.search(r'__version__ = "([^"]+)"', version_file)

    assert match is not None
    assert match.group(1) == pyproject["project"]["version"]
    assert "x-release-please-version" in version_file
