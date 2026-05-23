import dataclasses

import pytest
import yaml

from sandbox.compose import ComposeConfig, render


def cfg(**overrides):
    base = ComposeConfig(
        session_id="01HK3P0000000000000000",
        goal="trivial goal",
        db_image="postgres:16",
        agent_image_name="sandbox-agent",
        build_dir_agent="./build/agent",
        db_name="appdb",
    )
    return dataclasses.replace(base, **overrides)


def test_render_parses_as_yaml():
    out = render(cfg())
    parsed = yaml.safe_load(out)
    assert parsed["name"] == "01hk3p0000000000000000"  # lowercased
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


def test_missing_template_field_raises_strict_undefined():
    import jinja2
    # Render directly with an incomplete dict — simulates a future template
    # field that isn't in ComposeConfig yet.
    from sandbox.compose import _env
    tpl = _env.from_string("hello {{ nonexistent_field }}")
    with pytest.raises(jinja2.UndefinedError):
        tpl.render()


def test_db_service_has_postgres_env():
    parsed = yaml.safe_load(render(cfg(db_name="appdb")))
    env = parsed["services"]["db"]["environment"]
    # postgres:16 refuses to start without one of these
    assert env.get("POSTGRES_HOST_AUTH_METHOD") == "trust"
    # initdb only creates appdb if POSTGRES_DB is set
    assert env.get("POSTGRES_DB") == "appdb"


def test_squid_config_renders_with_allowlist():
    from sandbox.compose import render_squid_config
    out = render_squid_config(allowlist_fqdns=[
        "api.anthropic.com",
        ".github.com",
        "pypi.org",
    ])
    assert "api.anthropic.com" in out
    assert ".github.com" in out
    assert "pypi.org" in out
    # Squid recipe markers
    assert "http_port 3128" in out
    # We are NOT doing TLS MITM
    assert "ssl_bump" not in out
    assert "http_access allow" in out
    # The catch-all deny must be present
    assert "http_access deny all" in out


def test_squid_config_rejects_empty_allowlist():
    from sandbox.compose import render_squid_config
    with pytest.raises(ValueError, match="allowlist cannot be empty"):
        render_squid_config(allowlist_fqdns=[])
