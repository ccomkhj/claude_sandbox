import pytest


def pytest_addoption(parser):
    parser.addoption(
        "--run-integration",
        action="store_true",
        default=False,
        help="run integration tests that need a real Docker daemon",
    )


def pytest_collection_modifyitems(config, items):
    if config.getoption("--run-integration"):
        return
    skip_marker = pytest.mark.skip(reason="needs --run-integration")
    for item in items:
        if "integration" in item.keywords:
            item.add_marker(skip_marker)


@pytest.fixture
def sandbox_home(tmp_path, monkeypatch):
    """Redirect SANDBOX_HOME to a tmp dir for any test that touches state."""
    home = tmp_path / "sandbox_home"
    home.mkdir()
    monkeypatch.setenv("SANDBOX_HOME", str(home))
    return home
