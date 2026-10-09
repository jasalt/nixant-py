import pytest


@pytest.fixture(autouse=True)
def _no_cwd_target(monkeypatch: pytest.MonkeyPatch) -> None:
    """Commands default to dev unless a test says the directory names a target."""
    monkeypatch.setattr("nixant.cli.target_at", lambda provider, root, path: None)
