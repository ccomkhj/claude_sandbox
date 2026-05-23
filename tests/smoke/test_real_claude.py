import subprocess
import time

import pytest

from sandbox import cli, session


@pytest.mark.smoke
def test_real_claude_completes_a_trivial_goal(
    smoke_creds, fixture_repo, tiny_dump, sandbox_home, monkeypatch, tmp_path, capsys
):
    """End-to-end against the real Claude API. Manual / opt-in."""

    # Mock S3 fetch to return the local tiny dump
    monkeypatch.setattr(cli.dump, "fetch", lambda b, k: (tiny_dump, "smokeetag"))

    rc = cli.main([
        "start",
        "--repo", str(fixture_repo),
        "--goal", (
            "Create a new file x.py in the repository root containing exactly "
            "the line print('hello from sandbox'), then commit the change."
        ),
        "--dump-bucket", "x",
        "--dump-key", "y",
        "--db-name", "appdb",
    ])
    assert rc == 0
    sid = capsys.readouterr().out.strip().splitlines()[-1]
    project = sid.lower()

    # Poll up to 5 minutes for the agent to exit
    deadline = time.time() + 300
    while time.time() < deadline:
        ps = subprocess.run(
            ["docker", "ps", "-a", "--filter", f"name={project}-agent", "--format", "{{.Status}}"],
            check=True, capture_output=True, text=True,
        ).stdout
        if "Exited" in ps:
            break
        time.sleep(5)
    else:
        cli.main(["stop", sid])
        pytest.fail("real claude agent did not exit within 5 minutes")

    rc = cli.main(["finish", sid])
    assert rc == 0

    sdir = session.session_dir(sid)
    patch = (sdir / "patch.diff").read_text()
    assert "hello from sandbox" in patch, (
        f"goal not visible in patch — agent didn't follow instructions.\n"
        f"patch preview:\n{patch[:1000]}"
    )
