"""Regression checks for source distribution contents."""

from __future__ import annotations

import subprocess
import sys
import tarfile
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

FORBIDDEN_SDIST_PATHS = (
    ".context/",
    ".github/",
    ".ralphex/",
    ".venv/",
    "CLAUDE.md",
    "TODOS.md",
    "docs/demo.gif",
    "docs/demo.tape",
    "docs/internal/",
    "docs/plans/",
    "docs/social-preview.png",
    "docs/superpowers/",
    "marketing/",
    "output/",
)


def test_sdist_excludes_private_and_non_package_paths(tmp_path: Path) -> None:
    dist_dir = tmp_path / "dist"

    subprocess.run(
        [
            sys.executable,
            "-m",
            "build",
            "--sdist",
            "--no-isolation",
            "--outdir",
            str(dist_dir),
        ],
        cwd=ROOT,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )

    (sdist_path,) = dist_dir.glob("*.tar.gz")
    with tarfile.open(sdist_path, "r:gz") as archive:
        names = archive.getnames()

    package_root = sdist_path.name.removesuffix(".tar.gz") + "/"
    relative_names = [name.removeprefix(package_root) for name in names]

    for forbidden in FORBIDDEN_SDIST_PATHS:
        assert all(
            name != forbidden.rstrip("/") and not name.startswith(forbidden)
            for name in relative_names
        )


def test_sdist_exclude_config_covers_private_and_non_package_paths() -> None:
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text())
    excludes = set(pyproject["tool"]["hatch"]["build"]["targets"]["sdist"]["exclude"])

    for forbidden in FORBIDDEN_SDIST_PATHS:
        assert f"/{forbidden.rstrip('/')}" in excludes
