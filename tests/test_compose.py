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
        auth_env_name="CLAUDE_CODE_OAUTH_TOKEN",   # NEW
        auth_env_value="dummy-token",              # NEW
        aws_credentials=None,
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
    # We are NOT doing TLS MITM
    assert "ssl_bump" not in out
    assert "http_access allow" in out
    # Allowlist ACL must be present using dstdomain (compatible with standard
    # forward proxy; ssl::server_name requires SSL-bump/MITM mode which we don't use)
    assert "acl allowlisted dstdomain" in out
    # http_port and the catch-all deny are provided by the ubuntu/squid base
    # image's /etc/squid/squid.conf — our conf.d snippet only needs the allowlist ACL


def test_squid_config_rejects_empty_allowlist():
    from sandbox.compose import render_squid_config
    with pytest.raises(ValueError, match="allowlist cannot be empty"):
        render_squid_config(allowlist_fqdns=[])


def test_proxy_service_present():
    parsed = yaml.safe_load(render(cfg(proxy_image="sandbox-proxy:latest")))
    assert "proxy" in parsed["services"]
    assert parsed["services"]["proxy"]["image"] == "sandbox-proxy:latest"


def test_agent_net_is_internal():
    # v0.3.0: agent has NO direct egress; only path out is via proxy on proxy_egress_net
    parsed = yaml.safe_load(render(cfg()))
    assert parsed["networks"]["agent_net"]["internal"] is True


def test_proxy_on_agent_net_and_egress_net():
    parsed = yaml.safe_load(render(cfg()))
    proxy_nets = set(parsed["services"]["proxy"]["networks"])
    assert "agent_net" in proxy_nets
    assert "proxy_egress_net" in proxy_nets


def test_agent_has_https_proxy_env():
    parsed = yaml.safe_load(render(cfg()))
    env = parsed["services"]["agent"]["environment"]
    assert env["HTTPS_PROXY"] == "http://proxy:3128"
    assert env["HTTP_PROXY"] == "http://proxy:3128"
    # db should be in NO_PROXY so postgres connections don't go through Squid
    assert "db" in env["NO_PROXY"]


def test_agent_not_on_proxy_egress_net():
    parsed = yaml.safe_load(render(cfg()))
    agent_nets = set(parsed["services"]["agent"]["networks"])
    assert "agent_net" in agent_nets
    assert "db_net" in agent_nets
    assert "proxy_egress_net" not in agent_nets


def test_proxy_egress_net_is_a_bridge_with_internet():
    parsed = yaml.safe_load(render(cfg()))
    assert parsed["networks"]["proxy_egress_net"]["driver"] == "bridge"
    assert parsed["networks"]["proxy_egress_net"].get("internal", False) is False


def test_agent_renders_oauth_token_env_when_configured():
    parsed = yaml.safe_load(render(cfg(
        auth_env_name="CLAUDE_CODE_OAUTH_TOKEN",
        auth_env_value="oauth-fake-abc",
    )))
    env = parsed["services"]["agent"]["environment"]
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "oauth-fake-abc"
    assert "ANTHROPIC_API_KEY" not in env


def test_agent_renders_api_key_env_when_configured():
    parsed = yaml.safe_load(render(cfg(
        auth_env_name="ANTHROPIC_API_KEY",
        auth_env_value="sk-ant-api-99",
    )))
    env = parsed["services"]["agent"]["environment"]
    assert env["ANTHROPIC_API_KEY"] == "sk-ant-api-99"
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in env


def test_agent_auth_env_handles_special_chars():
    """tojson filter must escape quotes/newlines safely."""
    weird = 'tok"en\nwith"quotes'
    parsed = yaml.safe_load(render(cfg(
        auth_env_name="CLAUDE_CODE_OAUTH_TOKEN",
        auth_env_value=weird,
    )))
    assert parsed["services"]["agent"]["environment"]["CLAUDE_CODE_OAUTH_TOKEN"] == weird


def test_agent_renders_aws_env_when_credentials_provided():
    aws_creds = {
        "access_key_id": "ASIAFAKE",
        "secret_access_key": "secret-fake",
        "session_token": "session-fake",
        "region": "us-east-1",
    }
    parsed = yaml.safe_load(render(cfg(aws_credentials=aws_creds)))
    env = parsed["services"]["agent"]["environment"]
    assert env["AWS_ACCESS_KEY_ID"] == "ASIAFAKE"
    assert env["AWS_SECRET_ACCESS_KEY"] == "secret-fake"
    assert env["AWS_SESSION_TOKEN"] == "session-fake"
    assert env["AWS_REGION"] == "us-east-1"


def test_agent_omits_aws_env_when_credentials_none():
    parsed = yaml.safe_load(render(cfg(aws_credentials=None)))
    env = parsed["services"]["agent"]["environment"]
    assert "AWS_ACCESS_KEY_ID" not in env
    assert "AWS_SECRET_ACCESS_KEY" not in env
    assert "AWS_SESSION_TOKEN" not in env
    assert "AWS_REGION" not in env


def test_agent_renders_aws_env_without_session_token_when_unset():
    aws_creds = {
        "access_key_id": "AKIAFAKE",
        "secret_access_key": "secret-fake",
        "session_token": None,  # long-lived creds path
        "region": "eu-west-1",
    }
    parsed = yaml.safe_load(render(cfg(aws_credentials=aws_creds)))
    env = parsed["services"]["agent"]["environment"]
    assert env["AWS_ACCESS_KEY_ID"] == "AKIAFAKE"
    assert env["AWS_REGION"] == "eu-west-1"
    assert "AWS_SESSION_TOKEN" not in env
