from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal, Optional

import ulid

Status = Literal[
    "starting",
    "running",
    "finished",
    "failed",
    "crashed",
    "stopped",
]


def sandbox_home() -> Path:
    return Path(os.environ.get("SANDBOX_HOME", Path.home() / ".sandbox"))


def session_dir(sid: str) -> Path:
    return sandbox_home() / "sessions" / sid


def _new_id() -> str:
    return str(ulid.ULID())


@dataclass
class Meta:
    id: str
    status: Status
    goal: str
    repo: str
    started_at: float
    branch: str
    follower_pid: Optional[int] = None
    finished_at: Optional[float] = None
    exit_code: Optional[int] = None


def new_session(goal: str, repo: str) -> Meta:
    sid = _new_id()
    meta = Meta(
        id=sid,
        status="starting",
        goal=goal,
        repo=str(repo),
        started_at=time.time(),
        branch=f"sandbox/{sid}",
    )
    d = session_dir(sid)
    d.mkdir(parents=True, exist_ok=False)
    save(meta)
    return meta


def save(meta: Meta) -> None:
    path = session_dir(meta.id) / "meta.json"
    path.write_text(json.dumps(asdict(meta), indent=2, sort_keys=True))


def load(sid: str) -> Meta:
    path = session_dir(sid) / "meta.json"
    try:
        raw = path.read_text()
    except FileNotFoundError as e:
        raise LookupError(f"no session {sid!r}") from e
    return Meta(**json.loads(raw))


def find(prefix: str) -> Meta:
    base = sandbox_home() / "sessions"
    if not base.exists():
        raise LookupError(f"no sessions yet (looked in {base})")
    matches = sorted(p.name for p in base.iterdir() if p.is_dir() and p.name.startswith(prefix))
    if not matches:
        raise LookupError(f"no session matching {prefix!r}")
    if len(matches) > 1:
        raise LookupError(f"ambiguous prefix {prefix!r}: matches {matches}")
    return load(matches[0])


def all_sessions() -> list[Meta]:
    base = sandbox_home() / "sessions"
    if not base.exists():
        return []
    return [load(p.name) for p in sorted(base.iterdir()) if (p / "meta.json").exists()]
