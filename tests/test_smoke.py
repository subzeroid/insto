import insto


def test_package_imports() -> None:
    assert insto is not None


def test_version_is_v0_1_0() -> None:
    assert insto.__version__ == "0.1.0"
