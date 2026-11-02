import pytest


@pytest.fixture(autouse=True)
def _isolated_run_log(tmp_path_factory, monkeypatch):
    """Tests must never write to the developer's real run log."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path_factory.mktemp("state")))
