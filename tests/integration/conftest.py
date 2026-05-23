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
def fake_creds(tmp_path, monkeypatch):
    home = tmp_path / "fake_home"
    (home / ".claude").mkdir(parents=True)
    (home / ".claude" / ".credentials.json").write_text('{"placeholder": true}')
    # Preserve the real Docker config directory so CLI plugins (docker-compose) are
    # still discoverable after HOME is redirected to a tmp dir.
    real_docker_config = Path.home() / ".docker"
    monkeypatch.setenv("DOCKER_CONFIG", str(real_docker_config))
    monkeypatch.setenv("HOME", str(home))
    return home
