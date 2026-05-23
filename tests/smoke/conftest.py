import os
import subprocess

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
def smoke_creds(monkeypatch):
    """Skip cleanly if neither CLAUDE_CODE_OAUTH_TOKEN nor ANTHROPIC_API_KEY is set.

    These are the two auth modes v0.4+ supports. Prefer CLAUDE_CODE_OAUTH_TOKEN
    (subscription-billed via `claude setup-token`); ANTHROPIC_API_KEY falls back
    to pay-per-token API billing.
    """
    if os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", "").strip():
        return
    if os.environ.get("ANTHROPIC_API_KEY", "").strip():
        return
    pytest.skip(
        "smoke test needs CLAUDE_CODE_OAUTH_TOKEN (run `claude setup-token` on the host) "
        "or ANTHROPIC_API_KEY in env — neither was set"
    )
