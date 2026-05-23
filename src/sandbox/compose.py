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


def render(cfg: ComposeConfig) -> str:
    tpl = _env.get_template("compose.yml.j2")
    return tpl.render(**cfg.__dict__)
