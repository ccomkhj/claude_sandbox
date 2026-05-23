import os
import subprocess
from pathlib import Path

import pytest


@pytest.fixture
def fixture_repo(tmp_path):
    """Same as integration/conftest.py: a tiny git repo on `main` with one commit."""
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
    dump_dir = tmp_path / "dump"
    dump_dir.mkdir()
    out = dump_dir / "tiny.dump"
    subprocess.run([
        "docker", "run", "--rm",
        "-v", f"{dump_dir}:/out",
        "postgres:16",
        "su", "-c",
        "initdb -D /tmp/d -U postgres >/dev/null && "
        "pg_ctl -D /tmp/d -l /tmp/log -o '-c listen_addresses= -c unix_socket_directories=/tmp' start >/dev/null && "
        "createdb -h /tmp -U postgres appdb && "
        "psql -h /tmp -U postgres appdb -c 'CREATE TABLE t (x int); INSERT INTO t VALUES (42);' >/dev/null && "
        "pg_dump -h /tmp -U postgres -Fc -f /out/tiny.dump appdb",
        "postgres",
    ], check=True)
    return out


@pytest.fixture
def smoke_creds(monkeypatch, tmp_path):
    """Skip cleanly if neither ANTHROPIC_API_KEY nor real ~/.claude/.credentials.json exist."""
    if os.environ.get("ANTHROPIC_API_KEY"):
        home = tmp_path / "smoke_home"
        (home / ".claude").mkdir(parents=True)
        (home / ".claude" / ".credentials.json").write_text(
            '{"anthropic_api_key": "%s"}' % os.environ["ANTHROPIC_API_KEY"]
        )
        monkeypatch.setenv("HOME", str(home))
        return
    real_creds = os.path.expanduser("~/.claude/.credentials.json")
    if not os.path.isfile(real_creds):
        pytest.skip("no ANTHROPIC_API_KEY and no ~/.claude/.credentials.json — skipping smoke test")
    # Else: run with real HOME (don't override it)
