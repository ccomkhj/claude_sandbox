import shutil as _sh
import socket
import subprocess
import time
from pathlib import Path

import pytest

from sandbox import cli, session


@pytest.mark.integration
def test_end_to_end_with_stub_agent(
    docker_available, fixture_repo, tiny_dump, fake_auth_env, sandbox_home, monkeypatch, tmp_path, capsys
):
    if not docker_available:
        pytest.skip("docker daemon not available")

    # Assemble a patched images/ tree that uses agent-stub in place of the real agent.
    # v0.2.0: db/ subdir is gone — the CLI uses upstream postgres:16 and imports the dump
    # at runtime via docker cp + pg_restore. Only agent/ is needed here.
    images_real = Path(__file__).resolve().parents[2] / "images"
    images_patched = tmp_path / "images"
    (images_patched / "agent").mkdir(parents=True)
    _sh.copy(images_real / "agent-stub" / "Dockerfile", images_patched / "agent" / "Dockerfile")
    _sh.copy(images_real / "agent-stub" / "entrypoint.sh", images_patched / "agent" / "entrypoint.sh")

    # v0.3.0: CLI now builds the proxy image from images/proxy — copy it into the
    # patched images root so the monkeypatched _images_root() resolves it correctly.
    _sh.copytree(images_real / "proxy", images_patched / "proxy")

    monkeypatch.setattr(cli, "_images_root", lambda: images_patched)

    # Mock S3 fetch to return the local tiny.dump
    monkeypatch.setattr(cli.dump, "fetch", lambda b, k: (tiny_dump, "tinyetag"))

    # Snapshot sandbox-db:* images before the run so we can detect any NEW ones
    # created during it. Stale images from earlier v0.1 runs may exist on dev
    # machines; the invariant is "v0.2 did not build a new one", not "the store
    # is clean".
    def _sandbox_db_image_ids() -> set[str]:
        out = subprocess.run(
            ["docker", "image", "ls", "sandbox-db", "--format", "{{.ID}}"],
            check=True, capture_output=True, text=True,
        ).stdout
        return set(out.split())
    sandbox_db_before = _sandbox_db_image_ids()

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
    project = sid.lower()

    # Stub exits almost immediately. Poll for agent container to be Exited.
    deadline = time.time() + 180
    while time.time() < deadline:
        ps = subprocess.run(
            ["docker", "ps", "-a", "--filter", f"name={project}-agent", "--format", "{{.Status}}"],
            check=True, capture_output=True, text=True,
        ).stdout
        if "Exited" in ps:
            break
        time.sleep(2)
    else:
        # Best effort cleanup before failing
        subprocess.run(["docker", "compose", "-p", project, "down", "-v", "--rmi", "local"],
                       capture_output=True)
        pytest.fail("agent container never exited")

    try:
        rc = cli.main(["finish", sid])
        assert rc == 0

        bare = session.session_dir(sid) / "bare.git"
        log = subprocess.run(
            ["git", "-C", str(bare), "log", "--format=%s", f"sandbox/{sid}"],
            check=True, capture_output=True, text=True,
        ).stdout
        assert "stub: stub goal" in log

        # v0.2.0 invariant: no NEW sandbox-db:* image was created during the run.
        # (Stale images from earlier v0.1 runs may still exist on dev machines.)
        new_sandbox_db = _sandbox_db_image_ids() - sandbox_db_before
        assert new_sandbox_db == set(), (
            f"v0.2 flow created a sandbox-db image: {new_sandbox_db!r}"
        )
    finally:
        subprocess.run(["docker", "compose", "-p", project, "down", "-v", "--rmi", "local"],
                       capture_output=True)


@pytest.mark.integration
def test_live_postgres_source_end_to_end(
    docker_available, fixture_repo, fake_auth_env, sandbox_home, monkeypatch, tmp_path, capsys
):
    """Spin up a real postgres:16 container; pass --postgres-source pointing at it; expect a full session round-trip."""
    if not docker_available:
        pytest.skip("docker daemon not available")

    # Find an unused port on the host for the source DB
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    source_port = sock.getsockname()[1]
    sock.close()

    source_container = f"sandbox-pg-source-{source_port}"

    # Start a postgres:16 container on the host, seeded with a tiny table
    subprocess.run([
        "docker", "run", "-d", "--rm",
        "--name", source_container,
        "-p", f"{source_port}:5432",
        "-e", "POSTGRES_HOST_AUTH_METHOD=trust",
        "-e", "POSTGRES_DB=appdb",
        "postgres:16",
    ], check=True, capture_output=True, text=True)

    try:
        # Wait for the source DB to accept connections
        deadline = time.time() + 60
        ready = False
        while time.time() < deadline:
            probe = subprocess.run([
                "docker", "exec", source_container,
                "pg_isready", "-U", "postgres", "-d", "appdb",
            ], capture_output=True, text=True)
            if probe.returncode == 0:
                ready = True
                break
            time.sleep(1)
        assert ready, "source postgres never became ready"

        # Seed it with a tiny table
        subprocess.run([
            "docker", "exec", source_container,
            "psql", "-U", "postgres", "-d", "appdb",
            "-c", "CREATE TABLE marker (note text); INSERT INTO marker VALUES ('hello-from-source');",
        ], check=True, capture_output=True, text=True)

        # Patch images_root to use agent-stub (we don't need real claude here)
        images_real = Path(__file__).resolve().parents[2] / "images"
        images_patched = tmp_path / "images"
        images_patched.mkdir()
        (images_patched / "agent").mkdir()
        (images_patched / "proxy").mkdir()
        _sh.copy(images_real / "agent-stub" / "Dockerfile", images_patched / "agent" / "Dockerfile")
        _sh.copy(images_real / "agent-stub" / "entrypoint.sh", images_patched / "agent" / "entrypoint.sh")
        _sh.copy(images_real / "proxy" / "Dockerfile", images_patched / "proxy" / "Dockerfile")
        _sh.copy(images_real / "proxy" / "entrypoint.sh", images_patched / "proxy" / "entrypoint.sh")

        monkeypatch.setattr(cli, "_images_root", lambda: images_patched)

        # Run sandbox start with --postgres-source pointing at the host port.
        # Use host.docker.internal so the one-shot pg_dump container can reach the source DB on the host.
        # NOTE: host.docker.internal works on Docker Desktop (mac/win). On Linux it is not
        # auto-defined — the bridge gateway IP (172.17.0.1) may be needed instead.
        source_url = f"postgres://postgres@host.docker.internal:{source_port}/appdb"
        rc = cli.main([
            "start",
            "--repo", str(fixture_repo),
            "--goal", "live pg dump test",
            "--postgres-source", source_url,
            "--db-name", "appdb",
            "--agent-image", "sandbox-agent-livepg",
        ])
        assert rc == 0
        sid = capsys.readouterr().out.strip().splitlines()[-1]
        project = sid.lower()

        # Wait for the stub agent to exit
        agent_name = f"{project}-agent-1"
        deadline = time.time() + 180
        exited = False
        while time.time() < deadline:
            ps = subprocess.run(
                ["docker", "ps", "-a", "--filter", f"name={agent_name}", "--format", "{{.Status}}"],
                check=True, capture_output=True, text=True,
            ).stdout
            if "Exited" in ps:
                exited = True
                break
            time.sleep(2)

        try:
            assert exited, "agent stub container never exited"

            rc = cli.main(["finish", sid])
            assert rc == 0

            # The bare repo should have the stub's commit
            bare = session.session_dir(sid) / "bare.git"
            log = subprocess.run(
                ["git", "-C", str(bare), "log", "--format=%s", f"sandbox/{sid}"],
                check=True, capture_output=True, text=True,
            ).stdout
            assert "stub: live pg dump test" in log
        finally:
            # Best-effort cleanup of the sandbox session
            subprocess.run(
                ["docker", "compose", "-p", project, "down", "-v", "--rmi", "local"],
                capture_output=True,
            )
    finally:
        # Tear down the source postgres
        subprocess.run(
            ["docker", "rm", "-f", source_container],
            capture_output=True,
        )
