# Claude Code Sandbox Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a Python CLI (`sandbox`) that spawns isolated Docker Compose sessions, each running Claude Code against a fresh production Postgres dump and a copy of the user's repo, with the agent's changes coming back as a reviewable git branch.

**Architecture:** Compose-per-session. Each session is a `docker compose` project named with a ULID. Two services: `agent` (Claude Code) on a public-egress network plus an internal-only network; `db` (Postgres restored from S3 dump) on the internal network only. Inputs (repo bundle, ~/.claude creds, dump) enter via build context — never bind-mounted. Outputs return via `docker cp` of a git bundle the agent writes on exit.

**Tech Stack:** Python 3.11+, argparse, jinja2, boto3, python-ulid, pytest, moto[s3], Docker, Docker Compose v2, Postgres 16, Node 20.

**Spec reference:** `docs/superpowers/specs/2026-05-21-claude-code-sandbox-design.md`

---

## File structure

```
claude_sandbox/
├── pyproject.toml
├── README.md
├── .gitignore
├── docs/superpowers/
│   ├── specs/2026-05-21-claude-code-sandbox-design.md
│   └── plans/2026-05-21-claude-code-sandbox.md       (this file)
├── src/sandbox/
│   ├── __init__.py
│   ├── cli.py                  # argparse, verb dispatch
│   ├── session.py              # ~/.sandbox state dir, meta.json, ULID
│   ├── compose.py              # pure compose.yml renderer
│   ├── docker.py               # subprocess wrappers
│   ├── dump.py                 # S3 fetch with ETag cache
│   ├── repo.py                 # git bundle/bare/format-patch
│   └── templates/
│       └── compose.yml.j2
├── images/
│   ├── agent/
│   │   ├── Dockerfile
│   │   └── entrypoint.sh
│   ├── agent-stub/             # used only by integration tests
│   │   ├── Dockerfile
│   │   └── entrypoint.sh
│   └── db/
│       ├── Dockerfile
│       └── init.sh
└── tests/
    ├── conftest.py
    ├── test_session.py
    ├── test_compose.py
    ├── test_repo.py
    ├── test_dump.py
    ├── test_docker.py
    ├── test_cli.py
    └── integration/
        ├── conftest.py
        ├── test_end_to_end.py
        └── test_isolation.py
```

**Decomposition rationale:** Each `sandbox.*` module has a single responsibility and exactly one external dependency (subprocess, boto3, git CLI, or stdlib). The CLI is the only module that orchestrates across them. Image-side scripts (entrypoints, init) are kept short and shell-based — anything complex moves to the Python side where it's testable.

---

## Task 1: Project scaffold

**Files:**
- Create: `pyproject.toml`
- Create: `.gitignore`
- Create: `README.md`
- Create: `src/sandbox/__init__.py`
- Create: `tests/__init__.py`
- Create: `tests/conftest.py`

- [ ] **Step 1: Create `pyproject.toml`**

```toml
[project]
name = "claude-code-sandbox"
version = "0.1.0"
description = "Run Claude Code in an isolated Docker sandbox against a production Postgres dump."
requires-python = ">=3.11"
dependencies = [
    "boto3>=1.34",
    "jinja2>=3.1",
    "python-ulid>=2.2",
]

[project.optional-dependencies]
dev = [
    "pytest>=8",
    "moto[s3]>=5",
]

[project.scripts]
sandbox = "sandbox.cli:main"

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/sandbox"]

[tool.pytest.ini_options]
testpaths = ["tests"]
markers = [
    "integration: requires Docker daemon; opt in with --run-integration",
]
addopts = "-ra"
```

- [ ] **Step 2: Create `.gitignore`**

```
__pycache__/
*.pyc
.pytest_cache/
.venv/
dist/
build/
*.egg-info/
.DS_Store
```

- [ ] **Step 3: Create `README.md`**

```markdown
# claude-code-sandbox

Run Claude Code in an isolated Docker sandbox against a production Postgres dump.

See `docs/superpowers/specs/2026-05-21-claude-code-sandbox-design.md` for design.

Quickstart (filled in by Task 16).
```

- [ ] **Step 4: Create `src/sandbox/__init__.py` and `tests/__init__.py`**

Both files are empty.

- [ ] **Step 5: Create `tests/conftest.py`**

```python
import os
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
```

- [ ] **Step 6: Install dev deps and verify the empty test suite passes**

Run:
```
python -m venv .venv && source .venv/bin/activate && pip install -e '.[dev]' && pytest
```
Expected: `no tests ran` (exit 0) — confirms install + pytest discovery work.

- [ ] **Step 7: Commit**

```
git add pyproject.toml .gitignore README.md src tests
git commit -m "chore: scaffold python package and pytest config"
```

---

## Task 2: `sandbox.session` — state directory and metadata

**Files:**
- Create: `src/sandbox/session.py`
- Create: `tests/test_session.py`

- [ ] **Step 1: Write the failing tests**

`tests/test_session.py`:
```python
import json
import time

import pytest

from sandbox import session


def test_new_session_creates_dir_and_meta(sandbox_home):
    meta = session.new_session(goal="refactor X", repo="/tmp/myrepo")
    assert meta.status == "starting"
    assert meta.goal == "refactor X"
    assert meta.repo == "/tmp/myrepo"
    assert meta.branch == f"sandbox/{meta.id}"
    assert meta.started_at <= time.time()

    sdir = session.session_dir(meta.id)
    assert sdir.is_dir()
    on_disk = json.loads((sdir / "meta.json").read_text())
    assert on_disk["id"] == meta.id
    assert on_disk["status"] == "starting"


def test_load_round_trips(sandbox_home):
    meta = session.new_session(goal="g", repo="/tmp/r")
    loaded = session.load(meta.id)
    assert loaded == meta


def test_save_overwrites(sandbox_home):
    meta = session.new_session(goal="g", repo="/tmp/r")
    meta.status = "running"
    session.save(meta)
    assert session.load(meta.id).status == "running"


def test_find_by_prefix(sandbox_home):
    a = session.new_session(goal="a", repo="/tmp/r")
    b = session.new_session(goal="b", repo="/tmp/r")
    found = session.find(a.id[:6])
    assert found.id == a.id
    assert found.id != b.id


def test_find_ambiguous_prefix_raises(sandbox_home, monkeypatch):
    monkeypatch.setattr(session, "_new_id", lambda: "AABBCC" + "0" * 20)
    a = session.new_session(goal="a", repo="/tmp/r")
    monkeypatch.setattr(session, "_new_id", lambda: "AABBCD" + "0" * 20)
    b = session.new_session(goal="b", repo="/tmp/r")
    with pytest.raises(LookupError, match="ambiguous"):
        session.find("AABB")


def test_find_no_match_raises(sandbox_home):
    with pytest.raises(LookupError, match="no session"):
        session.find("ZZZZ")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_session.py -v`
Expected: import error or AttributeError — module/functions don't exist yet.

- [ ] **Step 3: Implement `src/sandbox/session.py`**

```python
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
        repo=str(Path(repo).resolve()),
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
    return Meta(**json.loads(path.read_text()))


def find(prefix: str) -> Meta:
    base = sandbox_home() / "sessions"
    if not base.exists():
        raise LookupError(f"no sessions yet (looked in {base})")
    matches = sorted(p.name for p in base.iterdir() if p.name.startswith(prefix))
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_session.py -v`
Expected: 6 passed.

- [ ] **Step 5: Commit**

```
git add src/sandbox/session.py tests/test_session.py
git commit -m "feat(session): state dir, meta.json, ULID ids, prefix lookup"
```

---

## Task 3: `sandbox.compose` — render compose.yml

**Files:**
- Create: `src/sandbox/compose.py`
- Create: `src/sandbox/templates/compose.yml.j2`
- Create: `tests/test_compose.py`

- [ ] **Step 1: Write the failing tests**

`tests/test_compose.py`:
```python
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
            # A host bind mount is a string with ":" and a leading "/" or "."
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_compose.py -v`
Expected: import errors.

- [ ] **Step 3: Create the template**

`src/sandbox/templates/compose.yml.j2`:
```yaml
name: {{ session_id }}

services:
  db:
    image: {{ db_image }}
    networks: [db_net]
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U postgres -d {{ db_name }}"]
      interval: 5s
      timeout: 3s
      retries: 60

  agent:
    image: {{ agent_image_name }}:{{ session_id }}
    build:
      context: {{ build_dir_agent }}
    depends_on:
      db:
        condition: service_healthy
    environment:
      GOAL: {{ goal | tojson }}
      BRANCH: sandbox/{{ session_id }}
      DATABASE_URL: postgres://postgres@db:5432/{{ db_name }}
    networks: [agent_net, db_net]

networks:
  agent_net:
    driver: bridge
  db_net:
    driver: bridge
    internal: true
```

- [ ] **Step 4: Implement `src/sandbox/compose.py`**

```python
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
    db_image: str            # e.g. sandbox-db:<dump-etag>
    agent_image_name: str    # base name, tag is appended as :<session_id>
    build_dir_db: str        # relative path in compose file (unused at runtime — db image is prebuilt)
    build_dir_agent: str     # relative path in compose file
    db_name: str             # postgres database name to wait on


def render(cfg: ComposeConfig) -> str:
    tpl = _env.get_template("compose.yml.j2")
    return tpl.render(**cfg.__dict__)
```

- [ ] **Step 5: Add `pyyaml` to dev deps for the test**

Edit `pyproject.toml`:
```toml
[project.optional-dependencies]
dev = [
    "pytest>=8",
    "moto[s3]>=5",
    "pyyaml>=6",
]
```
Then run `pip install -e '.[dev]'`.

- [ ] **Step 6: Run tests to verify they pass**

Run: `pytest tests/test_compose.py -v`
Expected: 7 passed.

- [ ] **Step 7: Commit**

```
git add src/sandbox/compose.py src/sandbox/templates/compose.yml.j2 tests/test_compose.py pyproject.toml
git commit -m "feat(compose): pure renderer for per-session compose.yml with isolation invariants"
```

---

## Task 4: `sandbox.repo` — host-side git bridge

**Files:**
- Create: `src/sandbox/repo.py`
- Create: `tests/test_repo.py`

- [ ] **Step 1: Write the failing tests**

`tests/test_repo.py`:
```python
import subprocess
from pathlib import Path

import pytest

from sandbox import repo


def git(cwd: Path, *args: str) -> str:
    res = subprocess.run(
        ["git", "-C", str(cwd), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return res.stdout


@pytest.fixture
def src_repo(tmp_path):
    r = tmp_path / "src"
    r.mkdir()
    git(r, "init", "-b", "main")
    git(r, "config", "user.email", "t@t")
    git(r, "config", "user.name", "t")
    (r / "hello.txt").write_text("hi\n")
    git(r, "add", ".")
    git(r, "commit", "-m", "init")
    return r


def test_bundle_host_repo_creates_clonable_bundle(src_repo, tmp_path):
    bundle = tmp_path / "repo.bundle"
    repo.bundle_host_repo(src_repo, bundle)
    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", str(bundle), str(clone)], check=True)
    assert (clone / "hello.txt").read_text() == "hi\n"


def test_init_bare_repo(tmp_path):
    bare = tmp_path / "bare.git"
    repo.init_bare_repo(bare)
    assert (bare / "HEAD").exists()
    # bare repos do not contain a working tree
    assert not (bare / "hello.txt").exists()


def test_fetch_bundle_into_bare(src_repo, tmp_path):
    # Make a branch in src and bundle it
    git(src_repo, "checkout", "-b", "feature")
    (src_repo / "more.txt").write_text("more\n")
    git(src_repo, "add", ".")
    git(src_repo, "commit", "-m", "feature")
    branch_bundle = tmp_path / "branch.bundle"
    subprocess.run(
        ["git", "-C", str(src_repo), "bundle", "create", str(branch_bundle), "feature"],
        check=True,
    )

    bare = tmp_path / "bare.git"
    repo.init_bare_repo(bare)
    repo.fetch_bundle_into_bare(bundle=branch_bundle, bare=bare, branch="feature")

    out = git(bare, "log", "--format=%s", "feature")
    assert "feature" in out


def test_format_patch_returns_non_empty(src_repo, tmp_path):
    git(src_repo, "checkout", "-b", "feature")
    (src_repo / "more.txt").write_text("more\n")
    git(src_repo, "add", ".")
    git(src_repo, "commit", "-m", "feature")
    branch_bundle = tmp_path / "branch.bundle"
    subprocess.run(
        ["git", "-C", str(src_repo), "bundle", "create", str(branch_bundle), "feature"],
        check=True,
    )
    bare = tmp_path / "bare.git"
    repo.init_bare_repo(bare)
    repo.fetch_bundle_into_bare(bundle=branch_bundle, bare=bare, branch="feature")

    patch = repo.format_patch(bare=bare, branch="feature", base="main")
    assert "more.txt" in patch
    assert "diff --git" in patch
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_repo.py -v`
Expected: import errors.

- [ ] **Step 3: Implement `src/sandbox/repo.py`**

```python
from __future__ import annotations

import subprocess
from pathlib import Path


def _git(*args: str, cwd: Path | None = None, capture: bool = False) -> str:
    res = subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=capture,
        text=True,
    )
    return res.stdout if capture else ""


def bundle_host_repo(src: Path, out_bundle: Path) -> None:
    """Create a git bundle of `src` at `out_bundle`. Includes all refs."""
    out_bundle.parent.mkdir(parents=True, exist_ok=True)
    _git("bundle", "create", str(out_bundle), "--all", cwd=src)


def init_bare_repo(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    _git("init", "--bare", "-b", "main", str(path))


def fetch_bundle_into_bare(*, bundle: Path, bare: Path, branch: str) -> None:
    _git("fetch", str(bundle), f"{branch}:{branch}", cwd=bare)


def format_patch(*, bare: Path, branch: str, base: str = "main") -> str:
    return _git(
        "format-patch",
        "--stdout",
        f"{base}..{branch}",
        cwd=bare,
        capture=True,
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_repo.py -v`
Expected: 4 passed.

- [ ] **Step 5: Commit**

```
git add src/sandbox/repo.py tests/test_repo.py
git commit -m "feat(repo): git bundle/bare/fetch/format-patch helpers"
```

---

## Task 5: `sandbox.dump` — S3 fetch with ETag cache

**Files:**
- Create: `src/sandbox/dump.py`
- Create: `tests/test_dump.py`

- [ ] **Step 1: Write the failing tests**

`tests/test_dump.py`:
```python
import boto3
import pytest
from moto import mock_aws

from sandbox import dump


@pytest.fixture
def s3_bucket(monkeypatch, sandbox_home):
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test")
    with mock_aws():
        s3 = boto3.client("s3")
        s3.create_bucket(Bucket="dumps")
        s3.put_object(Bucket="dumps", Key="prod/latest.dump", Body=b"DUMPDATA-v1")
        yield s3


def test_fetch_downloads_first_time(s3_bucket, sandbox_home):
    local, etag = dump.fetch("dumps", "prod/latest.dump")
    assert local.is_file()
    assert local.read_bytes() == b"DUMPDATA-v1"
    assert etag


def test_fetch_skips_when_cached(s3_bucket, sandbox_home, monkeypatch):
    dump.fetch("dumps", "prod/latest.dump")
    calls = []
    orig = s3_bucket.download_file

    def spy(Bucket, Key, Filename):
        calls.append((Bucket, Key))
        return orig(Bucket=Bucket, Key=Key, Filename=Filename)

    # Patch the boto3 client used by `dump.fetch`
    monkeypatch.setattr(dump, "_client", lambda: type("C", (), {
        "head_object": s3_bucket.head_object,
        "download_file": spy,
    })())
    dump.fetch("dumps", "prod/latest.dump")
    assert calls == []  # not re-downloaded


def test_fetch_redownloads_when_etag_changes(s3_bucket, sandbox_home):
    local1, etag1 = dump.fetch("dumps", "prod/latest.dump")
    s3_bucket.put_object(Bucket="dumps", Key="prod/latest.dump", Body=b"DUMPDATA-v2")
    local2, etag2 = dump.fetch("dumps", "prod/latest.dump")
    assert etag1 != etag2
    assert local2.read_bytes() == b"DUMPDATA-v2"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_dump.py -v`
Expected: import error.

- [ ] **Step 3: Implement `src/sandbox/dump.py`**

```python
from __future__ import annotations

from pathlib import Path

import boto3

from sandbox.session import sandbox_home


def cache_dir() -> Path:
    d = sandbox_home() / "cache" / "dumps"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _client():
    return boto3.client("s3")


def _safe(key: str) -> str:
    return key.replace("/", "_").replace(":", "_")


def fetch(bucket: str, key: str) -> tuple[Path, str]:
    """Download s3://bucket/key to the host cache. Skip if a file matching the
    current ETag is already cached. Return (local_path, etag)."""
    client = _client()
    head = client.head_object(Bucket=bucket, Key=key)
    etag = head["ETag"].strip('"')
    local = cache_dir() / f"{_safe(key)}.{etag}.dump"
    if local.exists():
        return local, etag
    tmp = local.with_suffix(local.suffix + ".part")
    client.download_file(Bucket=bucket, Key=key, Filename=str(tmp))
    tmp.rename(local)
    return local, etag
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_dump.py -v`
Expected: 3 passed.

- [ ] **Step 5: Commit**

```
git add src/sandbox/dump.py tests/test_dump.py
git commit -m "feat(dump): S3 fetch with ETag-keyed cache"
```

---

## Task 6: `sandbox.docker` — subprocess wrappers

**Files:**
- Create: `src/sandbox/docker.py`
- Create: `tests/test_docker.py`

- [ ] **Step 1: Write the failing tests**

We test command construction (no real docker calls). Each function builds an argv and delegates to a single `_run`.

`tests/test_docker.py`:
```python
import subprocess
from pathlib import Path

from sandbox import docker


def test_compose_up_argv(monkeypatch):
    captured = []

    def fake_run(argv, **kwargs):
        captured.append(argv)
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(docker, "_run", fake_run)
    docker.compose_up(project="abc", compose_file=Path("/x/compose.yml"), build=True, detach=True)
    assert captured == [[
        "docker", "compose",
        "-p", "abc",
        "-f", "/x/compose.yml",
        "up", "-d", "--build",
    ]]


def test_compose_down_argv(monkeypatch):
    captured = []
    monkeypatch.setattr(docker, "_run", lambda argv, **k: captured.append(argv) or subprocess.CompletedProcess(argv, 0, "", ""))
    docker.compose_down(project="abc", compose_file=Path("/x/compose.yml"), volumes=True, rmi_local=True)
    assert captured == [[
        "docker", "compose",
        "-p", "abc",
        "-f", "/x/compose.yml",
        "down", "-v", "--rmi", "local",
    ]]


def test_compose_ps_argv(monkeypatch):
    captured = []
    monkeypatch.setattr(docker, "_run", lambda argv, **k: captured.append(argv) or subprocess.CompletedProcess(argv, 0, '[{"Name":"x"}]', ""))
    docker.compose_ps(project="abc", compose_file=Path("/x/compose.yml"))
    assert captured == [[
        "docker", "compose",
        "-p", "abc",
        "-f", "/x/compose.yml",
        "ps", "--format", "json",
    ]]


def test_compose_kill_argv(monkeypatch):
    captured = []
    monkeypatch.setattr(docker, "_run", lambda argv, **k: captured.append(argv) or subprocess.CompletedProcess(argv, 0, "", ""))
    docker.compose_kill(project="abc", compose_file=Path("/x/compose.yml"), signal="SIGTERM")
    assert captured == [[
        "docker", "compose",
        "-p", "abc",
        "-f", "/x/compose.yml",
        "kill", "-s", "SIGTERM",
    ]]


def test_docker_cp_argv(monkeypatch):
    captured = []
    monkeypatch.setattr(docker, "_run", lambda argv, **k: captured.append(argv) or subprocess.CompletedProcess(argv, 0, "", ""))
    docker.cp(src="abc-agent-1:/output/branch.bundle", dst=Path("/host/branch.bundle"))
    assert captured == [["docker", "cp", "abc-agent-1:/output/branch.bundle", "/host/branch.bundle"]]


def test_docker_build_argv(monkeypatch):
    captured = []
    monkeypatch.setattr(docker, "_run", lambda argv, **k: captured.append(argv) or subprocess.CompletedProcess(argv, 0, "", ""))
    docker.build(context=Path("/x/build/db"), tag="sandbox-db:etag123")
    assert captured == [["docker", "build", "-t", "sandbox-db:etag123", "/x/build/db"]]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_docker.py -v`
Expected: import errors.

- [ ] **Step 3: Implement `src/sandbox/docker.py`**

```python
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Optional


def _run(argv: list[str], *, check: bool = True, capture: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(argv, check=check, capture_output=capture, text=True)


def _compose_base(project: str, compose_file: Path) -> list[str]:
    return ["docker", "compose", "-p", project, "-f", str(compose_file)]


def compose_up(*, project: str, compose_file: Path, build: bool = True, detach: bool = True) -> subprocess.CompletedProcess:
    argv = _compose_base(project, compose_file) + ["up"]
    if detach:
        argv.append("-d")
    if build:
        argv.append("--build")
    return _run(argv)


def compose_down(*, project: str, compose_file: Path, volumes: bool = True, rmi_local: bool = True) -> subprocess.CompletedProcess:
    argv = _compose_base(project, compose_file) + ["down"]
    if volumes:
        argv.append("-v")
    if rmi_local:
        argv += ["--rmi", "local"]
    return _run(argv)


def compose_ps(*, project: str, compose_file: Path) -> subprocess.CompletedProcess:
    return _run(_compose_base(project, compose_file) + ["ps", "--format", "json"])


def compose_kill(*, project: str, compose_file: Path, signal: str = "SIGTERM") -> subprocess.CompletedProcess:
    return _run(_compose_base(project, compose_file) + ["kill", "-s", signal])


def compose_logs_follow(*, project: str, compose_file: Path, stdout_path: Path) -> subprocess.Popen:
    """Spawn a detached `docker compose logs -f` whose stdout/stderr go to a file."""
    argv = _compose_base(project, compose_file) + ["logs", "-f", "--no-color"]
    stdout_path.parent.mkdir(parents=True, exist_ok=True)
    log_file = open(stdout_path, "ab", buffering=0)
    return subprocess.Popen(
        argv,
        stdout=log_file,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )


def cp(*, src: str, dst: Path) -> subprocess.CompletedProcess:
    return _run(["docker", "cp", src, str(dst)])


def build(*, context: Path, tag: str) -> subprocess.CompletedProcess:
    return _run(["docker", "build", "-t", tag, str(context)])


def container_name(*, project: str, service: str, index: int = 1) -> str:
    return f"{project}-{service}-{index}"
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_docker.py -v`
Expected: 6 passed.

- [ ] **Step 5: Commit**

```
git add src/sandbox/docker.py tests/test_docker.py
git commit -m "feat(docker): subprocess wrappers for docker compose + cp + build"
```

---

## Task 7: `images/db/` — Postgres image with dump baked in

**Files:**
- Create: `images/db/Dockerfile`
- Create: `images/db/init.sh`

This image is built per-dump (tag = ETag) by the CLI. The `dump.dump` file is placed in the build context by the CLI before calling `docker build`.

- [ ] **Step 1: Create `images/db/Dockerfile`**

```dockerfile
FROM postgres:16

ENV POSTGRES_HOST_AUTH_METHOD=trust \
    POSTGRES_DB=appdb \
    POSTGRES_USER=postgres

# The dump file is copied in by the CLI before `docker build`.
COPY dump.dump /docker-entrypoint-initdb.d/00_dump.dump
COPY init.sh /docker-entrypoint-initdb.d/01_restore.sh
RUN chmod +x /docker-entrypoint-initdb.d/01_restore.sh
```

- [ ] **Step 2: Create `images/db/init.sh`**

```bash
#!/usr/bin/env bash
set -euo pipefail

DUMP=/docker-entrypoint-initdb.d/00_dump.dump
DB=${POSTGRES_DB:-appdb}
USER=${POSTGRES_USER:-postgres}

if [ ! -s "$DUMP" ]; then
  echo "no dump found at $DUMP; skipping restore" >&2
  exit 0
fi

# pg_restore handles custom-format dumps; --clean --if-exists makes it idempotent
# even though postgres initdb has already created an empty $POSTGRES_DB.
pg_restore \
  --username="$USER" \
  --dbname="$DB" \
  --no-owner --no-acl \
  --clean --if-exists \
  --exit-on-error \
  "$DUMP"
```

- [ ] **Step 3: Commit**

```
git add images/db
git commit -m "feat(images/db): postgres image that restores baked-in dump on first init"
```

---

## Task 8: `images/agent/` — Claude Code agent image

**Files:**
- Create: `images/agent/Dockerfile`
- Create: `images/agent/entrypoint.sh`

Built per-session (tag = session id) because the build context contains the user's repo bundle and Claude Code credentials. Both must be torn down with the session.

- [ ] **Step 1: Create `images/agent/Dockerfile`**

```dockerfile
FROM node:20-slim

# git + postgres client + coreutils for shred
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
        git \
        ca-certificates \
        postgresql-client \
        coreutils \
 && rm -rf /var/lib/apt/lists/*

RUN npm install -g @anthropic-ai/claude-code

# Per-session build context contains:
#   repo.bundle       — git bundle of the user's repo
#   .credentials.json — Claude Code credentials (mode 0600)
#   settings.json     — optional Claude Code settings (may be absent)
COPY repo.bundle /input/repo.bundle
COPY .credentials.json /input/.credentials.json
# The placeholder makes the COPY work whether or not settings.json exists.
COPY settings.json* /input/

COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

WORKDIR /work
ENTRYPOINT ["/entrypoint.sh"]
```

- [ ] **Step 2: Create `images/agent/entrypoint.sh`**

```bash
#!/usr/bin/env bash
set -euo pipefail

: "${GOAL:?GOAL env var required}"
: "${BRANCH:?BRANCH env var required}"

# Install creds into the location claude expects, then shred originals.
mkdir -p /root/.claude
cp /input/.credentials.json /root/.claude/.credentials.json
chmod 600 /root/.claude/.credentials.json
if [ -f /input/settings.json ]; then
  cp /input/settings.json /root/.claude/settings.json
fi
shred -u /input/.credentials.json
[ -f /input/settings.json ] && shred -u /input/settings.json || true

# Clone repo bundle and check out the agent's branch.
git clone /input/repo.bundle /work/repo
cd /work/repo
git config user.email "agent@sandbox.local"
git config user.name "sandbox agent"
git checkout -b "$BRANCH"

# Exit trap: always commit + bundle whatever state exists.
mkdir -p /output
finish() {
  rc=$?
  set +e
  cd /work/repo
  git add -A
  git commit -m "agent wip" >/dev/null 2>&1
  git bundle create /output/branch.bundle "$BRANCH" >/dev/null 2>&1
  echo "$rc" > /output/exit_code
}
trap finish EXIT

# Run Claude Code in headless mode against the goal prompt.
exec claude --dangerously-skip-permissions -p "$GOAL"
```

- [ ] **Step 3: Commit**

```
git add images/agent
git commit -m "feat(images/agent): claude-code image with cred shred and exit-trap bundle"
```

---

## Task 9: `images/agent-stub/` — fake agent for integration tests

**Files:**
- Create: `images/agent-stub/Dockerfile`
- Create: `images/agent-stub/entrypoint.sh`

Identical structure to the real agent, but runs a deterministic stub instead of `claude`. Lets integration tests verify the full plumbing without needing Anthropic creds.

- [ ] **Step 1: Create `images/agent-stub/Dockerfile`**

```dockerfile
FROM debian:bookworm-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
        git \
        ca-certificates \
        postgresql-client \
        curl \
        coreutils \
 && rm -rf /var/lib/apt/lists/*

COPY repo.bundle /input/repo.bundle
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

WORKDIR /work
ENTRYPOINT ["/entrypoint.sh"]
```

- [ ] **Step 2: Create `images/agent-stub/entrypoint.sh`**

```bash
#!/usr/bin/env bash
set -euo pipefail

: "${GOAL:?GOAL env var required}"
: "${BRANCH:?BRANCH env var required}"

git clone /input/repo.bundle /work/repo
cd /work/repo
git config user.email "stub@sandbox.local"
git config user.name "stub agent"
git checkout -b "$BRANCH"

mkdir -p /output
finish() {
  rc=$?
  set +e
  cd /work/repo
  git add -A
  git commit -m "stub: $GOAL" >/dev/null 2>&1
  git bundle create /output/branch.bundle "$BRANCH" >/dev/null 2>&1
  echo "$rc" > /output/exit_code
}
trap finish EXIT

# Write a marker file and exit.
echo "$GOAL" > /work/repo/STUB_OUTPUT.md
```

- [ ] **Step 3: Commit**

```
git add images/agent-stub
git commit -m "test(images/agent-stub): deterministic stub agent for integration tests"
```

---

## Task 10: `sandbox.cli` — entry point, `start` verb wiring

**Files:**
- Create: `src/sandbox/cli.py`
- Create: `tests/test_cli.py`

- [ ] **Step 1: Write the failing tests (parser + start orchestration)**

`tests/test_cli.py`:
```python
import json
import shutil
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from sandbox import cli, session


def test_parser_recognizes_all_verbs():
    p = cli.build_parser()
    for verb in ("start", "status", "logs", "finish", "stop", "prune"):
        ns = p.parse_args([verb] + (["--repo", "/tmp/r", "--goal", "g"] if verb == "start" else ["zzz"] if verb in ("status", "logs", "finish", "stop") else []))
        assert ns.verb == verb


def test_start_orchestrates_and_prints_session_id(sandbox_home, tmp_path, monkeypatch, capsys):
    # Real on-disk source repo so `repo.bundle_host_repo` works.
    import subprocess
    src = tmp_path / "src"
    src.mkdir()
    subprocess.run(["git", "-C", str(src), "init", "-b", "main"], check=True)
    subprocess.run(["git", "-C", str(src), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(src), "config", "user.name", "t"], check=True)
    (src / "x").write_text("x")
    subprocess.run(["git", "-C", str(src), "add", "."], check=True)
    subprocess.run(["git", "-C", str(src), "commit", "-m", "init"], check=True)

    creds = tmp_path / "creds_home" / ".claude"
    creds.mkdir(parents=True)
    (creds / ".credentials.json").write_text("{}")
    monkeypatch.setenv("HOME", str(tmp_path / "creds_home"))

    # Stub the side-effect modules
    fake_dump = MagicMock(return_value=(tmp_path / "fake.dump", "etag123"))
    (tmp_path / "fake.dump").write_bytes(b"x")
    monkeypatch.setattr(cli.dump, "fetch", fake_dump)

    fake_build = MagicMock()
    fake_up = MagicMock()
    fake_logs = MagicMock(return_value=MagicMock(pid=4242))
    monkeypatch.setattr(cli.docker, "build", fake_build)
    monkeypatch.setattr(cli.docker, "compose_up", fake_up)
    monkeypatch.setattr(cli.docker, "compose_logs_follow", fake_logs)

    rc = cli.main([
        "start",
        "--repo", str(src),
        "--goal", "refactor X",
        "--dump-bucket", "dumps",
        "--dump-key", "prod/latest.dump",
        "--db-name", "appdb",
    ])
    assert rc == 0
    out = capsys.readouterr().out.strip()
    sid = out.splitlines()[-1].strip()

    meta = session.load(sid)
    assert meta.goal == "refactor X"
    assert meta.status == "running"
    assert meta.follower_pid == 4242

    sdir = session.session_dir(sid)
    assert (sdir / "compose.yml").exists()
    assert (sdir / "build" / "agent" / "repo.bundle").exists()
    assert (sdir / "build" / "agent" / ".credentials.json").exists()
    assert (sdir / "build" / "db" / "dump.dump").exists()
    assert (sdir / "bare.git" / "HEAD").exists()

    fake_dump.assert_called_once_with("dumps", "prod/latest.dump")
    fake_build.assert_called_once()
    fake_up.assert_called_once()
    fake_logs.assert_called_once()


def test_start_aborts_when_creds_missing(sandbox_home, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HOME", str(tmp_path / "empty_home"))
    rc = cli.main([
        "start", "--repo", str(tmp_path), "--goal", "g",
        "--dump-bucket", "b", "--dump-key", "k", "--db-name", "appdb",
    ])
    assert rc != 0
    assert "credentials" in capsys.readouterr().err.lower()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_cli.py -v`
Expected: import errors.

- [ ] **Step 3: Implement `src/sandbox/cli.py` (parser + `start` only)**

```python
from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

from sandbox import compose, docker, dump, repo, session


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="sandbox")
    sub = p.add_subparsers(dest="verb", required=True)

    sp = sub.add_parser("start", help="start a new sandboxed session")
    sp.add_argument("--repo", required=True, help="host path to the repo to copy into the sandbox")
    sp.add_argument("--goal", required=True, help="goal prompt passed to Claude Code")
    sp.add_argument("--dump-bucket", required=True)
    sp.add_argument("--dump-key", required=True)
    sp.add_argument("--db-name", default="appdb")
    sp.add_argument("--agent-image", default="sandbox-agent", help="base name of per-session agent image")

    for verb in ("status", "logs", "finish", "stop"):
        s = sub.add_parser(verb)
        s.add_argument("session", help="session id or unique prefix")
        if verb == "logs":
            s.add_argument("-f", "--follow", action="store_true")

    sub.add_parser("prune", help="remove finished sessions older than 30 days")

    return p


def _images_root() -> Path:
    # repo layout: <repo>/src/sandbox/cli.py and <repo>/images/
    return Path(__file__).resolve().parents[2] / "images"


def cmd_start(args: argparse.Namespace) -> int:
    creds_src = Path.home() / ".claude" / ".credentials.json"
    if not creds_src.is_file():
        print(
            "error: ~/.claude/.credentials.json not found. "
            "Run `claude login` on the host before starting a sandbox.",
            file=sys.stderr,
        )
        return 2

    meta = session.new_session(goal=args.goal, repo=args.repo)
    sdir = session.session_dir(meta.id)

    # 1. Fetch dump (cached by ETag)
    local_dump, etag = dump.fetch(args.dump_bucket, args.dump_key)

    # 2. Assemble db build context
    db_build = sdir / "build" / "db"
    db_build.mkdir(parents=True)
    shutil.copy(_images_root() / "db" / "Dockerfile", db_build / "Dockerfile")
    shutil.copy(_images_root() / "db" / "init.sh", db_build / "init.sh")
    shutil.copy(local_dump, db_build / "dump.dump")
    db_image = f"sandbox-db:{etag}"
    docker.build(context=db_build, tag=db_image)

    # 3. Assemble agent build context
    agent_build = sdir / "build" / "agent"
    agent_build.mkdir(parents=True)
    shutil.copy(_images_root() / "agent" / "Dockerfile", agent_build / "Dockerfile")
    shutil.copy(_images_root() / "agent" / "entrypoint.sh", agent_build / "entrypoint.sh")

    repo.bundle_host_repo(Path(args.repo), agent_build / "repo.bundle")
    shutil.copy(creds_src, agent_build / ".credentials.json")
    (agent_build / ".credentials.json").chmod(0o600)
    settings_src = Path.home() / ".claude" / "settings.json"
    if settings_src.is_file():
        shutil.copy(settings_src, agent_build / "settings.json")

    # 4. Bare repo for the user to fetch from
    repo.init_bare_repo(sdir / "bare.git")

    # 5. Render and write compose.yml
    cfg = compose.ComposeConfig(
        session_id=meta.id,
        goal=args.goal,
        db_image=db_image,
        agent_image_name=args.agent_image,
        build_dir_db="./build/db",
        build_dir_agent="./build/agent",
        db_name=args.db_name,
    )
    (sdir / "compose.yml").write_text(compose.render(cfg))

    # 6. Up
    docker.compose_up(project=meta.id, compose_file=sdir / "compose.yml", build=True, detach=True)

    # 7. Start log follower
    follower = docker.compose_logs_follow(
        project=meta.id,
        compose_file=sdir / "compose.yml",
        stdout_path=sdir / "logs" / "agent.log",
    )

    meta.status = "running"
    meta.follower_pid = follower.pid
    session.save(meta)

    print(meta.id)
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.verb == "start":
        return cmd_start(args)
    # other verbs added in later tasks
    print(f"verb {args.verb!r} not implemented yet", file=sys.stderr)
    return 1
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_cli.py -v`
Expected: 3 passed.

- [ ] **Step 5: Commit**

```
git add src/sandbox/cli.py tests/test_cli.py
git commit -m "feat(cli): argparse skeleton and start verb orchestration"
```

---

## Task 11: `sandbox.cli` — `status` and `logs` verbs

**Files:**
- Modify: `src/sandbox/cli.py`
- Modify: `tests/test_cli.py`

- [ ] **Step 1: Add failing tests**

Append to `tests/test_cli.py`:
```python
def test_status_prints_metadata(sandbox_home, monkeypatch, capsys):
    m = session.new_session(goal="g", repo="/tmp/r")
    m.status = "running"
    m.follower_pid = 1
    session.save(m)

    monkeypatch.setattr(cli.docker, "compose_ps", lambda **kw: MagicMock(stdout='[{"Service":"agent","State":"running"}]'))

    rc = cli.main(["status", m.id[:6]])
    out = capsys.readouterr().out
    assert rc == 0
    assert m.id in out
    assert "running" in out
    assert "g" in out  # goal echoed


def test_logs_streams_existing_file(sandbox_home, monkeypatch, capsys):
    m = session.new_session(goal="g", repo="/tmp/r")
    log_path = session.session_dir(m.id) / "logs" / "agent.log"
    log_path.parent.mkdir(parents=True)
    log_path.write_text("line1\nline2\n")

    rc = cli.main(["logs", m.id])
    out = capsys.readouterr().out
    assert rc == 0
    assert "line1" in out
    assert "line2" in out
```

- [ ] **Step 2: Run tests, see failures**

Run: `pytest tests/test_cli.py -k "status or logs" -v`
Expected: failures (verbs not implemented).

- [ ] **Step 3: Implement `cmd_status` and `cmd_logs`**

Replace the dispatch tail of `main()` and add functions:

```python
def cmd_status(args: argparse.Namespace) -> int:
    meta = session.find(args.session)
    sdir = session.session_dir(meta.id)
    try:
        ps = docker.compose_ps(project=meta.id, compose_file=sdir / "compose.yml")
        compose_state = ps.stdout
    except Exception as e:
        compose_state = f"<compose ps failed: {e}>"
    print(f"id:      {meta.id}")
    print(f"status:  {meta.status}")
    print(f"goal:    {meta.goal}")
    print(f"repo:    {meta.repo}")
    print(f"branch:  {meta.branch}")
    print(f"started: {meta.started_at}")
    print(f"compose: {compose_state.strip()}")
    return 0


def cmd_logs(args: argparse.Namespace) -> int:
    meta = session.find(args.session)
    log_path = session.session_dir(meta.id) / "logs" / "agent.log"
    if not log_path.is_file():
        print(f"no logs at {log_path}", file=sys.stderr)
        return 1
    if not args.follow:
        sys.stdout.write(log_path.read_text())
        return 0
    # Follow: simple tail -f
    import time as _time
    with log_path.open() as fh:
        fh.seek(0, 2)  # end
        while True:
            line = fh.readline()
            if not line:
                _time.sleep(0.5)
                continue
            sys.stdout.write(line)
            sys.stdout.flush()


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.verb == "start":
        return cmd_start(args)
    if args.verb == "status":
        return cmd_status(args)
    if args.verb == "logs":
        return cmd_logs(args)
    print(f"verb {args.verb!r} not implemented yet", file=sys.stderr)
    return 1
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_cli.py -v`
Expected: 5 passed.

- [ ] **Step 5: Commit**

```
git add src/sandbox/cli.py tests/test_cli.py
git commit -m "feat(cli): status and logs verbs"
```

---

## Task 12: `sandbox.cli` — `finish` verb

**Files:**
- Modify: `src/sandbox/cli.py`
- Modify: `tests/test_cli.py`

- [ ] **Step 1: Add failing test**

Append to `tests/test_cli.py`:
```python
def test_finish_imports_branch_into_bare(sandbox_home, tmp_path, monkeypatch, capsys):
    import subprocess

    m = session.new_session(goal="g", repo="/tmp/r")
    m.status = "running"
    session.save(m)
    sdir = session.session_dir(m.id)

    # Prepare bare repo on host
    repo.init_bare_repo(sdir / "bare.git")

    # Build a fake "container output" bundle for the branch
    work = tmp_path / "work"
    work.mkdir()
    subprocess.run(["git", "-C", str(work), "init", "-b", "main"], check=True)
    subprocess.run(["git", "-C", str(work), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(work), "config", "user.name", "t"], check=True)
    (work / "init").write_text("init")
    subprocess.run(["git", "-C", str(work), "add", "."], check=True)
    subprocess.run(["git", "-C", str(work), "commit", "-m", "init"], check=True)
    subprocess.run(["git", "-C", str(work), "checkout", "-b", m.branch], check=True)
    (work / "x").write_text("x")
    subprocess.run(["git", "-C", str(work), "add", "."], check=True)
    subprocess.run(["git", "-C", str(work), "commit", "-m", "agent change"], check=True)
    bundle_src = tmp_path / "branch.bundle"
    subprocess.run(["git", "-C", str(work), "bundle", "create", str(bundle_src), m.branch], check=True)

    # docker cp + compose_down stubbed
    def fake_cp(*, src, dst):
        import shutil as _sh
        _sh.copy(bundle_src, dst)
    monkeypatch.setattr(cli.docker, "cp", fake_cp)
    monkeypatch.setattr(cli.docker, "compose_ps", lambda **kw: MagicMock(stdout="[]"))  # nothing running
    fake_down = MagicMock()
    monkeypatch.setattr(cli.docker, "compose_down", fake_down)

    rc = cli.main(["finish", m.id])
    assert rc == 0

    log = subprocess.run(
        ["git", "-C", str(sdir / "bare.git"), "log", "--format=%s", m.branch],
        check=True, capture_output=True, text=True,
    ).stdout
    assert "agent change" in log

    assert (sdir / "patch.diff").is_file()
    assert (sdir / "patch.diff").read_text().strip()

    fake_down.assert_called_once()
    assert session.load(m.id).status == "finished"
```

- [ ] **Step 2: Run test to see it fail**

Run: `pytest tests/test_cli.py::test_finish_imports_branch_into_bare -v`
Expected: fail (verb not implemented).

- [ ] **Step 3: Implement `cmd_finish`**

Add to `src/sandbox/cli.py`:
```python
import time
import json as _json


def _is_running(project: str, compose_file: Path) -> bool:
    try:
        ps = docker.compose_ps(project=project, compose_file=compose_file)
    except Exception:
        return False
    try:
        rows = _json.loads(ps.stdout or "[]")
    except Exception:
        return False
    if isinstance(rows, dict):
        rows = [rows]
    return any(r.get("State") == "running" for r in rows)


def cmd_finish(args: argparse.Namespace) -> int:
    meta = session.find(args.session)
    sdir = session.session_dir(meta.id)
    compose_file = sdir / "compose.yml"

    if _is_running(meta.id, compose_file):
        print(
            f"session {meta.id} is still running. Use `sandbox stop {meta.id}` first.",
            file=sys.stderr,
        )
        return 2

    # Pull branch bundle out of the agent container
    bundle_dst = sdir / "branch.bundle"
    container = docker.container_name(project=meta.id, service="agent")
    try:
        docker.cp(src=f"{container}:/output/branch.bundle", dst=bundle_dst)
    except Exception as e:
        print(f"failed to copy branch bundle: {e}", file=sys.stderr)
        return 1

    # Fetch into bare and write a single-file patch for convenience
    repo.fetch_bundle_into_bare(bundle=bundle_dst, bare=sdir / "bare.git", branch=meta.branch)
    (sdir / "patch.diff").write_text(repo.format_patch(bare=sdir / "bare.git", branch=meta.branch))

    # Tear down compose project; this also removes per-session images so creds + db disappear
    docker.compose_down(project=meta.id, compose_file=compose_file, volumes=True, rmi_local=True)

    meta.status = "finished"
    meta.finished_at = time.time()
    session.save(meta)

    print(f"branch ready: git fetch {sdir / 'bare.git'} {meta.branch}")
    print(f"patch:        {sdir / 'patch.diff'}")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    dispatch = {
        "start": cmd_start,
        "status": cmd_status,
        "logs": cmd_logs,
        "finish": cmd_finish,
    }
    handler = dispatch.get(args.verb)
    if handler is None:
        print(f"verb {args.verb!r} not implemented yet", file=sys.stderr)
        return 1
    return handler(args)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_cli.py -v`
Expected: 6 passed.

- [ ] **Step 5: Commit**

```
git add src/sandbox/cli.py tests/test_cli.py
git commit -m "feat(cli): finish verb — docker cp bundle, fetch into bare, format-patch, compose down"
```

---

## Task 13: `sandbox.cli` — `stop` and `prune` verbs

**Files:**
- Modify: `src/sandbox/cli.py`
- Modify: `tests/test_cli.py`

- [ ] **Step 1: Add failing tests**

```python
def test_stop_sends_sigterm_then_compose_down(sandbox_home, monkeypatch):
    m = session.new_session(goal="g", repo="/tmp/r")
    m.status = "running"
    session.save(m)

    fake_kill = MagicMock()
    fake_down = MagicMock()
    monkeypatch.setattr(cli.docker, "compose_kill", fake_kill)
    monkeypatch.setattr(cli.docker, "compose_down", fake_down)
    monkeypatch.setattr(cli, "_wait_until_stopped", lambda **kw: True)  # short-circuit

    rc = cli.main(["stop", m.id])
    assert rc == 0
    fake_kill.assert_called_once()
    fake_down.assert_called_once()
    assert session.load(m.id).status == "stopped"


def test_prune_removes_old_finished_sessions(sandbox_home):
    import time as _t
    m = session.new_session(goal="g", repo="/tmp/r")
    m.status = "finished"
    m.finished_at = _t.time() - 60 * 60 * 24 * 31  # 31 days ago
    session.save(m)
    fresh = session.new_session(goal="g2", repo="/tmp/r")
    fresh.status = "finished"
    fresh.finished_at = _t.time()
    session.save(fresh)

    rc = cli.main(["prune"])
    assert rc == 0
    assert not session.session_dir(m.id).exists()
    assert session.session_dir(fresh.id).exists()
```

- [ ] **Step 2: Run tests to see them fail**

Run: `pytest tests/test_cli.py -k "stop or prune" -v`
Expected: failures.

- [ ] **Step 3: Implement `cmd_stop` and `cmd_prune`**

Add to `src/sandbox/cli.py`:
```python
import shutil as _shutil


def _wait_until_stopped(*, project: str, compose_file: Path, timeout_s: float = 30.0) -> bool:
    import time as _time
    start = _time.time()
    while _time.time() - start < timeout_s:
        if not _is_running(project, compose_file):
            return True
        _time.sleep(1.0)
    return False


def cmd_stop(args: argparse.Namespace) -> int:
    meta = session.find(args.session)
    sdir = session.session_dir(meta.id)
    compose_file = sdir / "compose.yml"
    try:
        docker.compose_kill(project=meta.id, compose_file=compose_file, signal="SIGTERM")
    except Exception:
        pass
    if not _wait_until_stopped(project=meta.id, compose_file=compose_file):
        # force teardown if SIGTERM didn't take
        docker.compose_kill(project=meta.id, compose_file=compose_file, signal="SIGKILL")
    docker.compose_down(project=meta.id, compose_file=compose_file, volumes=True, rmi_local=True)
    meta.status = "stopped"
    session.save(meta)
    print(f"stopped {meta.id}")
    return 0


def cmd_prune(args: argparse.Namespace) -> int:
    import time as _time
    cutoff = _time.time() - 60 * 60 * 24 * 30  # 30 days
    removed = []
    for meta in session.all_sessions():
        if meta.status in ("finished", "failed", "crashed", "stopped"):
            if meta.finished_at is not None and meta.finished_at < cutoff:
                _shutil.rmtree(session.session_dir(meta.id))
                removed.append(meta.id)
    print(f"pruned {len(removed)} session(s)")
    for sid in removed:
        print(f"  {sid}")
    return 0
```

Extend the dispatch table:
```python
    dispatch = {
        "start": cmd_start,
        "status": cmd_status,
        "logs": cmd_logs,
        "finish": cmd_finish,
        "stop": cmd_stop,
        "prune": cmd_prune,
    }
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_cli.py -v`
Expected: 8 passed.

- [ ] **Step 5: Commit**

```
git add src/sandbox/cli.py tests/test_cli.py
git commit -m "feat(cli): stop and prune verbs"
```

---

## Task 14: Integration test — end-to-end with `agent-stub`

**Files:**
- Create: `tests/integration/__init__.py`
- Create: `tests/integration/conftest.py`
- Create: `tests/integration/test_end_to_end.py`

This test really runs Docker. It is skipped by default; the user opts in with `pytest --run-integration`.

- [ ] **Step 1: Create `tests/integration/__init__.py` (empty)**

- [ ] **Step 2: Create `tests/integration/conftest.py`**

```python
import os
import subprocess
from pathlib import Path

import pytest


@pytest.fixture(scope="session")
def docker_available() -> bool:
    try:
        subprocess.run(["docker", "info"], check=True, capture_output=True)
        return True
    except Exception:
        return False


@pytest.fixture
def fixture_repo(tmp_path):
    src = tmp_path / "fixture_repo"
    src.mkdir()
    subprocess.run(["git", "-C", str(src), "init", "-b", "main"], check=True)
    subprocess.run(["git", "-C", str(src), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(src), "config", "user.name", "t"], check=True)
    (src / "README.md").write_text("hello\n")
    subprocess.run(["git", "-C", str(src), "add", "."], check=True)
    subprocess.run(["git", "-C", str(src), "commit", "-m", "init"], check=True)
    return src


@pytest.fixture
def tiny_dump(tmp_path):
    """Produce a valid pg_dump custom-format file using a one-shot postgres container."""
    out = tmp_path / "tiny.dump"
    subprocess.run([
        "docker", "run", "--rm",
        "-v", f"{tmp_path}:/out",
        "postgres:16",
        "bash", "-lc",
        "pg_dump --help >/dev/null && "
        "initdb -D /tmp/d -U postgres >/dev/null && "
        "pg_ctl -D /tmp/d -l /tmp/log -o '-c listen_addresses= -c unix_socket_directories=/tmp' start >/dev/null && "
        "createdb -h /tmp -U postgres appdb && "
        "psql -h /tmp -U postgres appdb -c 'CREATE TABLE t (x int); INSERT INTO t VALUES (42);' >/dev/null && "
        "pg_dump -h /tmp -U postgres -Fc -f /out/tiny.dump appdb",
    ], check=True)
    return out


@pytest.fixture
def fake_creds(tmp_path, monkeypatch):
    home = tmp_path / "fake_home"
    (home / ".claude").mkdir(parents=True)
    (home / ".claude" / ".credentials.json").write_text('{"placeholder": true}')
    monkeypatch.setenv("HOME", str(home))
    return home
```

- [ ] **Step 3: Create `tests/integration/test_end_to_end.py`**

```python
import json
import subprocess
import time
from pathlib import Path

import pytest

from sandbox import cli, session


@pytest.mark.integration
def test_end_to_end_with_stub_agent(
    docker_available, fixture_repo, tiny_dump, fake_creds, sandbox_home, monkeypatch, tmp_path, capsys
):
    if not docker_available:
        pytest.skip("docker daemon not available")

    # Have the CLI use the stub agent image instead of the real one.
    # The stub Dockerfile lives at images/agent-stub/; point the agent build dir there.
    monkeypatch.setattr(
        cli, "_images_root",
        lambda: Path(__file__).resolve().parents[2] / "images",
    )

    # Mock S3 to copy the local tiny.dump instead.
    def fake_fetch(bucket, key):
        return (tiny_dump, "tinyetag")
    monkeypatch.setattr(cli.dump, "fetch", fake_fetch)

    # Use the stub image: swap the agent image dir at copy time.
    import shutil as _sh
    real_cmd_start = cli.cmd_start

    def patched_start(args):
        # Replace the agent Dockerfile + entrypoint with stub versions
        rc = real_cmd_start(args)
        return rc

    # Easier: monkeypatch _images_root to return a temp tree that uses stub.
    images_real = Path(__file__).resolve().parents[2] / "images"
    images_patched = tmp_path / "images"
    (images_patched / "db").mkdir(parents=True)
    (images_patched / "agent").mkdir(parents=True)
    _sh.copy(images_real / "db" / "Dockerfile", images_patched / "db" / "Dockerfile")
    _sh.copy(images_real / "db" / "init.sh", images_patched / "db" / "init.sh")
    _sh.copy(images_real / "agent-stub" / "Dockerfile", images_patched / "agent" / "Dockerfile")
    _sh.copy(images_real / "agent-stub" / "entrypoint.sh", images_patched / "agent" / "entrypoint.sh")
    monkeypatch.setattr(cli, "_images_root", lambda: images_patched)

    rc = cli.main([
        "start",
        "--repo", str(fixture_repo),
        "--goal", "stub goal",
        "--dump-bucket", "x", "--dump-key", "y",
        "--db-name", "appdb",
        "--agent-image", "sandbox-agent-stub",
    ])
    assert rc == 0
    sid = capsys.readouterr().out.strip().splitlines()[-1]

    # Stub exits almost immediately. Poll for `exit_code` to appear in the agent.
    deadline = time.time() + 120
    while time.time() < deadline:
        ps = subprocess.run(
            ["docker", "ps", "-a", "--filter", f"name={sid}-agent", "--format", "{{.Status}}"],
            check=True, capture_output=True, text=True,
        ).stdout
        if "Exited" in ps:
            break
        time.sleep(2)
    else:
        pytest.fail("agent container never exited")

    rc = cli.main(["finish", sid])
    assert rc == 0

    bare = session.session_dir(sid) / "bare.git"
    log = subprocess.run(
        ["git", "-C", str(bare), "log", "--format=%s", f"sandbox/{sid}"],
        check=True, capture_output=True, text=True,
    ).stdout
    assert "stub: stub goal" in log
```

- [ ] **Step 4: Run the integration test**

Run: `pytest tests/integration/test_end_to_end.py --run-integration -v`
Expected: 1 passed (takes ~2-5 minutes — first run builds Postgres image).

- [ ] **Step 5: Commit**

```
git add tests/integration
git commit -m "test(integration): end-to-end run with stub agent and tiny pg dump"
```

---

## Task 15: Integration test — isolation invariants

**Files:**
- Create: `tests/integration/test_isolation.py`

- [ ] **Step 1: Create the test**

`tests/integration/test_isolation.py`:
```python
import subprocess
import time
from pathlib import Path

import pytest

from sandbox import cli, session


@pytest.mark.integration
def test_isolation_invariants(
    docker_available, fixture_repo, tiny_dump, fake_creds, sandbox_home, monkeypatch, tmp_path, capsys
):
    if not docker_available:
        pytest.skip("docker daemon not available")

    # Use stub agent that idles long enough for us to docker exec into it.
    import shutil as _sh
    images_real = Path(__file__).resolve().parents[2] / "images"
    images_patched = tmp_path / "images"
    (images_patched / "db").mkdir(parents=True)
    (images_patched / "agent").mkdir(parents=True)
    _sh.copy(images_real / "db" / "Dockerfile", images_patched / "db" / "Dockerfile")
    _sh.copy(images_real / "db" / "init.sh", images_patched / "db" / "init.sh")
    _sh.copy(images_real / "agent-stub" / "Dockerfile", images_patched / "agent" / "Dockerfile")

    # Custom entrypoint that idles 120s after its normal stub work.
    entry = images_patched / "agent" / "entrypoint.sh"
    entry.write_text((images_real / "agent-stub" / "entrypoint.sh").read_text().replace(
        "echo \"$GOAL\" > /work/repo/STUB_OUTPUT.md",
        "echo \"$GOAL\" > /work/repo/STUB_OUTPUT.md\nsleep 120",
    ))
    entry.chmod(0o755)

    monkeypatch.setattr(cli, "_images_root", lambda: images_patched)
    monkeypatch.setattr(cli.dump, "fetch", lambda b, k: (tiny_dump, "tinyetag"))

    rc = cli.main([
        "start", "--repo", str(fixture_repo), "--goal", "iso",
        "--dump-bucket", "x", "--dump-key", "y", "--db-name", "appdb",
        "--agent-image", "sandbox-agent-iso",
    ])
    assert rc == 0
    sid = capsys.readouterr().out.strip().splitlines()[-1]

    # Wait for both services to be up
    deadline = time.time() + 120
    agent_name = f"{sid}-agent-1"
    db_name = f"{sid}-db-1"
    while time.time() < deadline:
        ps = subprocess.run(
            ["docker", "ps", "--format", "{{.Names}}"], check=True, capture_output=True, text=True
        ).stdout
        if agent_name in ps and db_name in ps:
            break
        time.sleep(2)
    else:
        pytest.fail("services never came up")

    try:
        # Agent CAN reach db
        r = subprocess.run(
            ["docker", "exec", agent_name, "psql", "-h", "db", "-U", "postgres", "-d", "appdb", "-c", "SELECT 1"],
            capture_output=True, text=True,
        )
        assert r.returncode == 0, f"agent could not reach db: {r.stderr}"

        # Agent CANNOT reach host.docker.internal on common ports
        r = subprocess.run(
            ["docker", "exec", agent_name, "bash", "-c",
             "curl --max-time 3 -sS http://host.docker.internal:5432/ ; echo rc=$?"],
            capture_output=True, text=True,
        )
        assert "rc=0" not in r.stdout, "agent reached host.docker.internal — isolation broken"

        # db CANNOT reach the public internet
        r = subprocess.run(
            ["docker", "exec", db_name, "bash", "-c",
             "getent hosts example.com >/dev/null 2>&1; echo rc=$?"],
            capture_output=True, text=True,
        )
        assert r.stdout.strip().endswith("rc=2") or r.stdout.strip().endswith("rc=1"), \
            f"db resolved an external host: {r.stdout}"
    finally:
        cli.main(["stop", sid])
```

- [ ] **Step 2: Run the test**

Run: `pytest tests/integration/test_isolation.py --run-integration -v`
Expected: 1 passed.

- [ ] **Step 3: Commit**

```
git add tests/integration/test_isolation.py
git commit -m "test(integration): assert isolation invariants on a live sandbox"
```

---

## Task 16: README, smoke instructions, and tag v0.1.0

**Files:**
- Modify: `README.md`

- [ ] **Step 1: Write the README**

```markdown
# claude-code-sandbox

Run Claude Code in an isolated Docker sandbox against a production Postgres dump.

The agent gets a fresh Postgres restored from S3, a copy of your repo on its own branch, and a Claude Code installation with your existing credentials — all in a container that cannot read your host filesystem and cannot reach host services. Long-running goal-mode tasks run detached; you retrieve the agent's branch when it's done.

## Prerequisites

- Docker Desktop (or any Docker daemon with `docker compose` v2)
- Python 3.11+
- A Claude Code login on the host (`claude login`) so `~/.claude/.credentials.json` exists
- AWS credentials with read access to the dump bucket

## Install

```sh
git clone <this repo>
cd claude-code-sandbox
python -m venv .venv && source .venv/bin/activate
pip install -e .
```

This installs the `sandbox` CLI on your PATH.

## Use

```sh
# Kick off a long-running goal task
sandbox start \
  --repo ~/code/my-app \
  --goal "refactor X to use the new payments module" \
  --dump-bucket my-dumps \
  --dump-key prod/latest.dump \
  --db-name appdb
# → prints session id, e.g. 01HK3P...

# Check on it
sandbox status 01HK3P
sandbox logs 01HK3P -f

# When it exits, pull the branch back to your local repo
sandbox finish 01HK3P
# → prints:  git fetch ~/.sandbox/sessions/01HK3P.../bare.git sandbox/01HK3P...

# Force-stop one that's stuck
sandbox stop 01HK3P

# Clean up sessions older than 30 days
sandbox prune
```

## Testing

```sh
pip install -e '.[dev]'
pytest                          # fast unit tests
pytest --run-integration        # also runs real-Docker tests (~5 min)
```

## Design

See `docs/superpowers/specs/2026-05-21-claude-code-sandbox-design.md`.
```

- [ ] **Step 2: Run the full unit test suite as a smoke check**

Run: `pytest`
Expected: all unit tests pass.

- [ ] **Step 3: Commit and tag**

```
git add README.md
git commit -m "docs: README with quickstart"
git tag v0.1.0
```

---

## Self-Review

**1. Spec coverage:**
- Run Claude Code in Docker sandbox → Tasks 8, 10.
- Container cannot read host FS / reach host services → enforced by Task 3 (compose template assertions) and Task 15 (live isolation test).
- Production Postgres dump from S3 → Task 5 + Task 7.
- Repo copied in, changes back as a git branch → Tasks 4, 10, 12.
- Detached sessions, multiple concurrent jobs, ULID ids → Tasks 2, 10, 13.
- Self-contained repo runnable by anyone with Docker + Python → Tasks 1, 16.
- Error-handling rows in the spec → Task 10 (creds-missing), Task 12 (refuses on running session), Task 13 (stop/prune), Task 8 (exit-trap always bundles).
- Credential hygiene invariants → Task 8 (entrypoint shreds), Task 12 (`down --rmi local` removes per-session image), Task 15 (live tests).
- Testing requirements (unit + integration + isolation) → Tasks 2-6 (unit), 14 (integration), 15 (isolation). Smoke is documented in Task 16 README but left manual per spec.

**2. Placeholder scan:** No TBDs, no "implement later", no "add error handling" without code. Every code step has actual code; every command has expected output.

**3. Type consistency:**
- `Meta` fields used identically across CLI verbs.
- `ComposeConfig` consumed only by `compose.render`; the snapshot test pins its shape.
- `docker.cp` signature `(*, src: str, dst: Path)` matches all call sites in Tasks 10 and 12.
- `repo.fetch_bundle_into_bare` signature stable across Tasks 4 and 12.
- Image naming convention `sandbox-agent:<session_id>` and `sandbox-db:<etag>` consistent in Tasks 7, 8, 10.

No gaps found.
