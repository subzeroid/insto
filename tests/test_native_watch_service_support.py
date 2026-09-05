"""Offline tests for the opt-in native smoke's bounded, isolated setup."""

import os
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.e2e import test_watch_service as native


@pytest.fixture
def native_tmp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.delenv("INSTO_TEST_HOME", raising=False)
    base = tmp_path / "native-test"
    base.mkdir(mode=0o700)
    case = base / "case0"
    case.mkdir(mode=0o700)
    return case


@pytest.mark.parametrize("explicit", [False, True])
def test_new_native_home_is_private_and_predictable(
    native_tmp: Path, monkeypatch: pytest.MonkeyPatch, explicit: bool
) -> None:
    expected = (native_tmp.parent if explicit else native_tmp) / "service home"
    if explicit:
        monkeypatch.setenv("INSTO_TEST_HOME", str(expected))
    home = native._create_test_home(native_tmp)
    assert home == expected.resolve()
    assert home.is_dir() and stat.S_IMODE(home.stat().st_mode) == 0o700
    assert home.stat().st_uid == os.getuid()


@pytest.mark.parametrize("variant", ["relative", "wrong_parent", "wrong_name", "empty"])
def test_native_home_rejects_unexpected_location(
    native_tmp: Path, monkeypatch: pytest.MonkeyPatch, variant: str
) -> None:
    requested = {
        "relative": "service home",
        "wrong_parent": str(native_tmp / "service home"),
        "wrong_name": str(native_tmp.parent / "different"),
        "empty": "",
    }[variant]
    monkeypatch.setenv("INSTO_TEST_HOME", requested)
    with pytest.raises(ValueError, match="home"):
        native._create_test_home(native_tmp)
    assert list(native_tmp.iterdir()) == []
    assert list(native_tmp.parent.iterdir()) == [native_tmp]


@pytest.mark.parametrize("kind", ["directory", "file", "symlink", "dangling_symlink"])
def test_native_home_refuses_existing_leaf(
    native_tmp: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    home = native_tmp.parent / "service home"
    if kind == "directory":
        home.mkdir()
    elif kind == "file":
        home.write_text("preserve")
    else:
        home.symlink_to(native_tmp if kind == "symlink" else native_tmp / "missing")
    monkeypatch.setenv("INSTO_TEST_HOME", str(home))
    before = home.lstat()
    with pytest.raises(FileExistsError):
        native._create_test_home(native_tmp)
    assert home.lstat() == before


def test_native_home_refuses_public_parent(
    native_tmp: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    native_tmp.parent.chmod(0o755)
    monkeypatch.setenv("INSTO_TEST_HOME", str(native_tmp.parent / "service home"))
    with pytest.raises(ValueError, match="parent"):
        native._create_test_home(native_tmp)
    assert not (native_tmp.parent / "service home").exists()


def test_native_home_refuses_symlink_parent(
    native_tmp: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    parent = native_tmp.parent
    real_parent = parent.with_name("real-native-test")
    parent.rename(real_parent)
    parent.symlink_to(real_parent, target_is_directory=True)
    monkeypatch.setenv("INSTO_TEST_HOME", str(parent / "service home"))
    with pytest.raises(ValueError, match="parent"):
        native._create_test_home(native_tmp)
    assert not (real_parent / "service home").exists()


def test_native_home_refuses_foreign_parent(
    native_tmp: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    parent = native_tmp.parent
    monkeypatch.setenv("INSTO_TEST_HOME", str(parent / "service home"))
    original = Path.lstat

    def lstat(path: Path) -> object:
        if path == parent:
            return SimpleNamespace(st_uid=os.getuid() + 1, st_mode=stat.S_IFDIR | 0o700)
        return original(path)

    monkeypatch.setattr(Path, "lstat", lstat)
    with pytest.raises(ValueError, match="parent"):
        native._create_test_home(native_tmp)
    assert not (parent / "service home").exists()


@pytest.mark.parametrize("setting,expected", [(None, ["-I"]), ("0", ["-I"]), ("1", ["-I", "-B"])])
def test_native_python_flags_are_explicit(
    monkeypatch: pytest.MonkeyPatch, setting: str | None, expected: list[str]
) -> None:
    monkeypatch.delenv("INSTO_TEST_NO_BYTECODE", raising=False)
    if setting is not None:
        monkeypatch.setenv("INSTO_TEST_NO_BYTECODE", setting)
    assert native._python_flags() == expected
