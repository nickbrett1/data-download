"""Smoke test: the src-layout package installs and imports cleanly."""


def test_package_imports():
    import data_download

    assert data_download.__version__
