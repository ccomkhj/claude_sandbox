import yaml

from sandbox.compose import ComposeConfig, render


def cfg(**overrides):
    base = ComposeConfig(
        session_id="01HK3P0000000000000000",
        goal="trivial goal",
        db_image="sandbox-db:abc123",
        agent_image_name="sandbox-agent",
        build_dir_db="./build/db",
        build_dir_agent="./build/agent",
        db_name="appdb",
    )
    for k, v in overrides.items():
        setattr(base, k, v)
    return base


def test_render_parses_as_yaml():
    out = render(cfg())
    parsed = yaml.safe_load(out)
    assert parsed["name"] == "01HK3P0000000000000000"
    assert "agent" in parsed["services"]
    assert "db" in parsed["services"]


def test_db_net_is_internal():
    parsed = yaml.safe_load(render(cfg()))
    assert parsed["networks"]["db_net"]["internal"] is True


def test_db_only_on_db_net():
    parsed = yaml.safe_load(render(cfg()))
    assert parsed["services"]["db"]["networks"] == ["db_net"]


def test_agent_on_both_networks():
    parsed = yaml.safe_load(render(cfg()))
    assert set(parsed["services"]["agent"]["networks"]) == {"agent_net", "db_net"}


def test_no_host_bind_mounts():
    parsed = yaml.safe_load(render(cfg()))
    for svc in parsed["services"].values():
        for vol in svc.get("volumes", []) or []:
            assert not (
                isinstance(vol, str)
                and ":" in vol
                and (vol.startswith("/") or vol.startswith("."))
            ), f"forbidden bind mount: {vol}"


def test_agent_depends_on_db_healthcheck():
    parsed = yaml.safe_load(render(cfg()))
    dep = parsed["services"]["agent"]["depends_on"]
    assert dep["db"]["condition"] == "service_healthy"


def test_goal_passed_through_env():
    parsed = yaml.safe_load(render(cfg(goal="please be careful")))
    assert parsed["services"]["agent"]["environment"]["GOAL"] == "please be careful"
