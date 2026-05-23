import pytest


def pytest_addoption(parser):
    parser.addoption(
        "--run-integration",
        action="store_true",
        default=False,
        help="run integration tests that need a real Docker daemon",
    )
    parser.addoption(
        "--run-smoke",
        action="store_true",
        default=False,
        help="run smoke tests that hit the real Claude API (requires ANTHROPIC_API_KEY or ~/.claude/.credentials.json)",
    )


def pytest_collection_modifyitems(config, items):
    if not config.getoption("--run-integration"):
        skip_integration = pytest.mark.skip(reason="needs --run-integration")
        for item in items:
            if "integration" in item.keywords:
                item.add_marker(skip_integration)
    if not config.getoption("--run-smoke"):
        skip_smoke = pytest.mark.skip(reason="needs --run-smoke")
        for item in items:
            if "smoke" in item.keywords:
                item.add_marker(skip_smoke)


@pytest.fixture
def sandbox_home(tmp_path, monkeypatch):
    """Redirect SANDBOX_HOME to a tmp dir for any test that touches state."""
    home = tmp_path / "sandbox_home"
    home.mkdir()
    monkeypatch.setenv("SANDBOX_HOME", str(home))
    return home
