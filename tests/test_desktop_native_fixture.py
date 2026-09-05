"""The opt-in native fixture refuses non-fake or unsafe homes before mutation."""

import pytest


@pytest.mark.parametrize(
    "fault", ["token", "symlink", "mode", "home_mode", "parent_mode", "missing_db"]
)
def test_native_fixture_rejects_unsafe_home(tmp_path, fault):
    from tests.e2e.desktop_lifecycle import validate_fixture

    home = tmp_path / "service home"
    home.mkdir(mode=0o700)
    config = home / "config.toml"
    config.write_bytes(b'backend = "fake"\n')
    config.chmod(0o600)
    db = home / "store.db"
    db.touch(mode=0o600)
    if fault == "token":
        config.write_bytes(b'backend = "hikerapi"\n')
    elif fault == "symlink":
        config.unlink()
        config.symlink_to(db)
    elif fault == "mode":
        config.chmod(0o644)
    elif fault == "home_mode":
        home.chmod(0o755)
    elif fault == "parent_mode":
        home.parent.chmod(0o755)
    else:
        db.unlink()
    with pytest.raises((AssertionError, FileNotFoundError)):
        validate_fixture(home)
