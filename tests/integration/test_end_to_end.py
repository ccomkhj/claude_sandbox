import shutil as _sh
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

    # Assemble a patched images/ tree that uses agent-stub in place of the real agent.
    # v0.2.0: db/ subdir is gone — the CLI uses upstream postgres:16 and imports the dump
    # at runtime via docker cp + pg_restore. Only agent/ is needed here.
    images_real = Path(__file__).resolve().parents[2] / "images"
    images_patched = tmp_path / "images"
    (images_patched / "agent").mkdir(parents=True)
    _sh.copy(images_real / "agent-stub" / "Dockerfile", images_patched / "agent" / "Dockerfile")
    _sh.copy(images_real / "agent-stub" / "entrypoint.sh", images_patched / "agent" / "entrypoint.sh")
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
