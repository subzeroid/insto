import pytest

from insto.desktop.configuration import config_bytes, initialize_database
from insto.desktop.profile import Profile


@pytest.fixture
def monitoring_profile(tmp_path):
    profile = Profile(tmp_path / "desktop")
    with profile.locked(initialize=True):
        initialize_database(profile.home / "store.db")
        profile.write_config(config_bytes(profile, "offline-desktop-token"))
        profile.write_state(profile.new_state(remaining=8, desired="stopped"))
    return profile
