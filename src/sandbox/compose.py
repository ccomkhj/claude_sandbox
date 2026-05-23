from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import jinja2

_env = jinja2.Environment(
    loader=jinja2.FileSystemLoader(Path(__file__).parent / "templates"),
    autoescape=False,
    keep_trailing_newline=True,
    undefined=jinja2.StrictUndefined,
)


@dataclass
class ComposeConfig:
    session_id: str
    goal: str
    db_image: str
    agent_image_name: str
    build_dir_agent: str
    db_name: str
    proxy_image: str = "sandbox-proxy:latest"
    auth_env_name: str = "CLAUDE_CODE_OAUTH_TOKEN"
    auth_env_value: str = ""
    aws_credentials: dict | None = None


def render(cfg: ComposeConfig) -> str:
    tpl = _env.get_template("compose.yml.j2")
    return tpl.render(**cfg.__dict__)


def render_squid_config(*, allowlist_fqdns: list[str]) -> str:
    if not allowlist_fqdns:
        raise ValueError("allowlist cannot be empty")
    tpl = _env.get_template("squid.conf.j2")
    return tpl.render(allowlist_fqdns=allowlist_fqdns)
