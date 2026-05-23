import json
import shutil
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from sandbox import cli, session


def test_parser_recognizes_all_verbs():
    p = cli.build_parser()
    for verb in ("start", "status", "logs", "finish", "stop", "prune"):
        ns = p.parse_args([verb] + (["--repo", "/tmp/r", "--goal", "g", "--dump-bucket", "b", "--dump-key", "k"] if verb == "start" else ["zzz"] if verb in ("status", "logs", "finish", "stop") else []))
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

    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "dummy-token-for-test")

    # Stub the side-effect modules
    fake_dump = MagicMock(return_value=(tmp_path / "fake.dump", "etag123"))
    (tmp_path / "fake.dump").write_bytes(b"x")
    monkeypatch.setattr(cli.dump, "fetch", fake_dump)

    fake_build = MagicMock()
    fake_up = MagicMock()
    fake_cp = MagicMock()
    fake_logs = MagicMock(return_value=MagicMock(pid=4242))
    fake_exec = MagicMock(return_value=MagicMock(returncode=0))
    monkeypatch.setattr(cli.docker, "build", fake_build)
    monkeypatch.setattr(cli.docker, "compose_up", fake_up)
    monkeypatch.setattr(cli.docker, "cp", fake_cp)
    monkeypatch.setattr(cli.docker, "compose_logs_follow", fake_logs)
    monkeypatch.setattr(cli.docker, "exec_in_container", fake_exec)
    monkeypatch.setattr(cli, "_wait_for_db_ready", MagicMock())
    monkeypatch.setattr(cli, "_wait_for_proxy_ready", MagicMock())

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
    assert meta.agent_image == f"sandbox-agent:{sid}"
    assert meta.db_image is None  # v0.2.0: no per-session db image

    sdir = session.session_dir(sid)
    assert (sdir / "compose.yml").exists()
    assert not (sdir / "build" / "agent" / "repo.bundle").exists()
    assert not (sdir / "build" / "agent" / ".credentials.json").exists()
    assert (sdir / "bare.git" / "HEAD").exists()

    fake_dump.assert_called_once_with("dumps", "prod/latest.dump")
    fake_up.assert_called_once()
    cp_sources = [call.kwargs["src"] for call in fake_cp.call_args_list]
    assert str(sdir / "input" / "repo.bundle") in cp_sources
    assert not any(".credentials.json" in s for s in cp_sources)
    fake_logs.assert_called_once()


def test_start_aborts_when_no_auth_env_set(sandbox_home, monkeypatch, capsys):
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    rc = cli.main([
        "start", "--repo", "/tmp", "--goal", "g",
        "--dump-bucket", "b", "--dump-key", "k", "--db-name", "appdb",
    ])
    err = capsys.readouterr().err
    assert rc != 0
    assert "claude setup-token" in err


def test_start_marks_session_failed_when_orchestration_raises(
    sandbox_home, tmp_path, monkeypatch, capsys
):
    import subprocess
    src = tmp_path / "src"
    src.mkdir()
    subprocess.run(["git", "-C", str(src), "init", "-b", "main"], check=True)
    subprocess.run(["git", "-C", str(src), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(src), "config", "user.name", "t"], check=True)
    (src / "x").write_text("x")
    subprocess.run(["git", "-C", str(src), "add", "."], check=True)
    subprocess.run(["git", "-C", str(src), "commit", "-m", "init"], check=True)

    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "dummy-token-for-test")

    # Force dump.fetch to blow up after session is created
    monkeypatch.setattr(cli.dump, "fetch", MagicMock(side_effect=RuntimeError("s3 boom")))

    with pytest.raises(RuntimeError, match="s3 boom"):
        cli.main([
            "start", "--repo", str(src), "--goal", "g",
            "--dump-bucket", "b", "--dump-key", "k", "--db-name", "appdb",
        ])

    # Exactly one session should exist, and it should be marked failed.
    sessions = session.all_sessions()
    assert len(sessions) == 1
    assert sessions[0].status == "failed"
    assert sessions[0].finished_at is not None


def test_start_tears_down_compose_when_input_copy_fails(
    sandbox_home, tmp_path, monkeypatch
):
    import subprocess
    src = tmp_path / "src"
    src.mkdir()
    subprocess.run(["git", "-C", str(src), "init", "-b", "main"], check=True)
    subprocess.run(["git", "-C", str(src), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(src), "config", "user.name", "t"], check=True)
    (src / "x").write_text("x")
    subprocess.run(["git", "-C", str(src), "add", "."], check=True)
    subprocess.run(["git", "-C", str(src), "commit", "-m", "init"], check=True)

    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "dummy-token-for-test")

    (tmp_path / "fake.dump").write_bytes(b"x")
    monkeypatch.setattr(cli.dump, "fetch", MagicMock(return_value=(tmp_path / "fake.dump", "e")))
    monkeypatch.setattr(cli.docker, "build", MagicMock())
    monkeypatch.setattr(cli.docker, "compose_up", MagicMock())
    monkeypatch.setattr(cli.docker, "cp", MagicMock(side_effect=RuntimeError("cp boom")))
    monkeypatch.setattr(cli, "_wait_for_db_ready", MagicMock())
    monkeypatch.setattr(cli, "_wait_for_proxy_ready", MagicMock())
    compose_down = MagicMock()
    remove_images = MagicMock()
    monkeypatch.setattr(cli.docker, "compose_down", compose_down)
    monkeypatch.setattr(cli.docker, "remove_images", remove_images)

    with pytest.raises(RuntimeError, match="cp boom"):
        cli.main([
            "start", "--repo", str(src), "--goal", "g",
            "--dump-bucket", "b", "--dump-key", "k", "--db-name", "appdb",
        ])

    m = session.all_sessions()[0]
    assert m.status == "failed"
    compose_down.assert_called_once()
    # v0.2.0: db_image is None (upstream postgres:16), so only agent_image is cleaned up
    remove_images.assert_called_once_with(m.agent_image)
    sdir = session.session_dir(m.id)
    assert not (sdir / "input" / "repo.bundle").exists()


def test_start_passes_lowercased_project_to_docker(
    sandbox_home, tmp_path, monkeypatch, capsys
):
    import subprocess
    src = tmp_path / "src"
    src.mkdir()
    subprocess.run(["git", "-C", str(src), "init", "-b", "main"], check=True)
    subprocess.run(["git", "-C", str(src), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(src), "config", "user.name", "t"], check=True)
    (src / "x").write_text("x")
    subprocess.run(["git", "-C", str(src), "add", "."], check=True)
    subprocess.run(["git", "-C", str(src), "commit", "-m", "init"], check=True)

    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "dummy-token-for-test")

    (tmp_path / "fake.dump").write_bytes(b"x")
    monkeypatch.setattr(cli.dump, "fetch", MagicMock(return_value=(tmp_path / "fake.dump", "e")))
    monkeypatch.setattr(cli.docker, "build", MagicMock())
    fake_up = MagicMock()
    fake_cp = MagicMock()
    fake_logs = MagicMock(return_value=MagicMock(pid=1))
    fake_exec = MagicMock(return_value=MagicMock(returncode=0))
    monkeypatch.setattr(cli.docker, "compose_up", fake_up)
    monkeypatch.setattr(cli.docker, "cp", fake_cp)
    monkeypatch.setattr(cli.docker, "compose_logs_follow", fake_logs)
    monkeypatch.setattr(cli.docker, "exec_in_container", fake_exec)
    monkeypatch.setattr(cli, "_wait_for_db_ready", MagicMock())
    monkeypatch.setattr(cli, "_wait_for_proxy_ready", MagicMock())

    rc = cli.main([
        "start", "--repo", str(src), "--goal", "g",
        "--dump-bucket", "b", "--dump-key", "k", "--db-name", "appdb",
    ])
    assert rc == 0
    sid = capsys.readouterr().out.strip().splitlines()[-1]
    project = fake_up.call_args.kwargs["project"]
    assert project == sid.lower()
    assert project == project.lower()  # idempotent — verifies it's already lowercased
    assert fake_logs.call_args.kwargs["project"] == project


def test_start_requires_dump_bucket_and_key(monkeypatch):
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["start", "--repo", "/tmp/r", "--goal", "g"])


def test_status_prints_metadata(sandbox_home, monkeypatch, capsys):
    m = session.new_session(goal="g", repo="/tmp/r")
    m.status = "running"
    m.follower_pid = 1
    session.save(m)

    monkeypatch.setattr(cli.docker, "compose_ps", lambda **kw: MagicMock(stdout='[{"Service":"agent","State":"running"}]'))

    rc = cli.main(["status", m.id[:12]])
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


def test_finish_imports_branch_into_bare(sandbox_home, tmp_path, monkeypatch, capsys):
    import subprocess

    m = session.new_session(goal="g", repo="/tmp/r")
    m.status = "running"
    session.save(m)
    sdir = session.session_dir(m.id)

    # Prepare bare repo on host
    from sandbox import repo
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
    subprocess.run(["git", "-C", str(work), "bundle", "create", str(bundle_src), "main", m.branch], check=True)

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

    patch = (sdir / "patch.diff").read_text()
    assert "agent change" in patch       # the agent's commit is in the patch
    assert "init" not in patch           # base 'init' commit is NOT in the patch
    assert "diff --git" in patch         # well-formed patch

    fake_down.assert_called_once()
    assert session.load(m.id).exit_code == 0
    assert session.load(m.id).status == "finished"


def test_finish_uses_agent_reported_base_branch(sandbox_home, tmp_path, monkeypatch, capsys):
    import subprocess

    m = session.new_session(goal="g", repo="/tmp/r")
    m.status = "running"
    m.agent_image = f"sandbox-agent:{m.id}"
    m.db_image = f"sandbox-db:{m.id.lower()}"
    session.save(m)
    sdir = session.session_dir(m.id)

    from sandbox import repo
    repo.init_bare_repo(sdir / "bare.git")

    work = tmp_path / "work"
    work.mkdir()
    subprocess.run(["git", "-C", str(work), "init", "-b", "trunk"], check=True)
    subprocess.run(["git", "-C", str(work), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(work), "config", "user.name", "t"], check=True)
    (work / "base.txt").write_text("base\n")
    subprocess.run(["git", "-C", str(work), "add", "."], check=True)
    subprocess.run(["git", "-C", str(work), "commit", "-m", "base"], check=True)
    subprocess.run(["git", "-C", str(work), "checkout", "-b", m.branch], check=True)
    (work / "feature.txt").write_text("feature\n")
    subprocess.run(["git", "-C", str(work), "add", "."], check=True)
    subprocess.run(["git", "-C", str(work), "commit", "-m", "feature"], check=True)
    bundle_src = tmp_path / "branch.bundle"
    subprocess.run(["git", "-C", str(work), "bundle", "create", str(bundle_src), "trunk", m.branch], check=True)

    def fake_cp(*, src, dst):
        import shutil as _sh
        if src.endswith("/branch.bundle"):
            _sh.copy(bundle_src, dst)
        elif src.endswith("/exit_code"):
            dst.write_text("0\n")
        elif src.endswith("/base_branch"):
            dst.write_text("trunk\n")
        else:
            raise AssertionError(f"unexpected docker cp source: {src}")

    monkeypatch.setattr(cli.docker, "cp", fake_cp)
    monkeypatch.setattr(cli.docker, "compose_ps", lambda **kw: MagicMock(stdout="[]"))
    monkeypatch.setattr(cli.docker, "compose_down", MagicMock())
    monkeypatch.setattr(cli.docker, "remove_images", MagicMock())

    rc = cli.main(["finish", m.id])
    assert rc == 0

    patch = (sdir / "patch.diff").read_text()
    assert "feature.txt" in patch
    assert "base.txt" not in patch
    loaded = session.load(m.id)
    assert loaded.base_branch == "trunk"
    assert loaded.exit_code == 0
    assert loaded.status == "finished"


def test_finish_marks_crashed_when_agent_exit_code_nonzero(
    sandbox_home, tmp_path, monkeypatch, capsys
):
    import subprocess

    m = session.new_session(goal="g", repo="/tmp/r")
    m.status = "running"
    m.agent_image = f"sandbox-agent:{m.id}"
    m.db_image = f"sandbox-db:{m.id.lower()}"
    session.save(m)
    sdir = session.session_dir(m.id)

    from sandbox import repo
    repo.init_bare_repo(sdir / "bare.git")

    work = tmp_path / "work"
    work.mkdir()
    subprocess.run(["git", "-C", str(work), "init", "-b", "main"], check=True)
    subprocess.run(["git", "-C", str(work), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(work), "config", "user.name", "t"], check=True)
    (work / "base.txt").write_text("base\n")
    subprocess.run(["git", "-C", str(work), "add", "."], check=True)
    subprocess.run(["git", "-C", str(work), "commit", "-m", "base"], check=True)
    subprocess.run(["git", "-C", str(work), "checkout", "-b", m.branch], check=True)
    (work / "partial.txt").write_text("partial\n")
    subprocess.run(["git", "-C", str(work), "add", "."], check=True)
    subprocess.run(["git", "-C", str(work), "commit", "-m", "partial"], check=True)
    bundle_src = tmp_path / "branch.bundle"
    subprocess.run(["git", "-C", str(work), "bundle", "create", str(bundle_src), "main", m.branch], check=True)

    def fake_cp(*, src, dst):
        import shutil as _sh
        if src.endswith("/branch.bundle"):
            _sh.copy(bundle_src, dst)
        elif src.endswith("/exit_code"):
            dst.write_text("7\n")
        elif src.endswith("/base_branch"):
            dst.write_text("main\n")
        else:
            raise AssertionError(f"unexpected docker cp source: {src}")

    monkeypatch.setattr(cli.docker, "cp", fake_cp)
    monkeypatch.setattr(cli.docker, "compose_ps", lambda **kw: MagicMock(stdout="[]"))
    monkeypatch.setattr(cli.docker, "compose_down", MagicMock())
    monkeypatch.setattr(cli.docker, "remove_images", MagicMock())

    rc = cli.main(["finish", m.id])
    assert rc == 0

    loaded = session.load(m.id)
    assert loaded.status == "crashed"
    assert loaded.exit_code == 7
    assert (sdir / "patch.diff").read_text()


def test_finish_removes_custom_tagged_images(sandbox_home, tmp_path, monkeypatch, capsys):
    import subprocess

    m = session.new_session(goal="g", repo="/tmp/r")
    m.status = "running"
    m.agent_image = f"sandbox-agent-custom:{m.id}"
    m.db_image = f"sandbox-db:{m.id.lower()}"
    session.save(m)
    sdir = session.session_dir(m.id)

    from sandbox import repo
    repo.init_bare_repo(sdir / "bare.git")

    work = tmp_path / "work"
    work.mkdir()
    subprocess.run(["git", "-C", str(work), "init", "-b", "main"], check=True)
    subprocess.run(["git", "-C", str(work), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(work), "config", "user.name", "t"], check=True)
    (work / "base.txt").write_text("base\n")
    subprocess.run(["git", "-C", str(work), "add", "."], check=True)
    subprocess.run(["git", "-C", str(work), "commit", "-m", "base"], check=True)
    subprocess.run(["git", "-C", str(work), "checkout", "-b", m.branch], check=True)
    (work / "x").write_text("x\n")
    subprocess.run(["git", "-C", str(work), "add", "."], check=True)
    subprocess.run(["git", "-C", str(work), "commit", "-m", "change"], check=True)
    bundle_src = tmp_path / "branch.bundle"
    subprocess.run(["git", "-C", str(work), "bundle", "create", str(bundle_src), "main", m.branch], check=True)

    def fake_cp(*, src, dst):
        import shutil as _sh
        if src.endswith("/branch.bundle"):
            _sh.copy(bundle_src, dst)
        elif src.endswith("/exit_code"):
            dst.write_text("0\n")
        elif src.endswith("/base_branch"):
            dst.write_text("main\n")
        else:
            raise AssertionError(f"unexpected docker cp source: {src}")

    remove_images = MagicMock()
    monkeypatch.setattr(cli.docker, "cp", fake_cp)
    monkeypatch.setattr(cli.docker, "compose_ps", lambda **kw: MagicMock(stdout="[]"))
    monkeypatch.setattr(cli.docker, "compose_down", MagicMock())
    monkeypatch.setattr(cli.docker, "remove_images", remove_images)

    rc = cli.main(["finish", m.id])
    assert rc == 0

    remove_images.assert_called_once_with(m.agent_image, m.db_image)


def test_finish_refuses_when_session_is_running(sandbox_home, monkeypatch, capsys):
    m = session.new_session(goal="g", repo="/tmp/r")
    m.status = "running"
    session.save(m)

    # compose ps returns a running service
    monkeypatch.setattr(cli.docker, "compose_ps",
                        lambda **kw: MagicMock(stdout='[{"Service":"agent","State":"running"}]'))

    rc = cli.main(["finish", m.id])
    assert rc != 0
    err = capsys.readouterr().err
    assert "still running" in err


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


def test_start_imports_dump_at_runtime_not_via_image_build(
    sandbox_home, tmp_path, monkeypatch, capsys
):
    """The dump must enter via docker cp + docker exec pg_restore, never via docker build."""
    import subprocess
    src = tmp_path / "src"
    src.mkdir()
    subprocess.run(["git", "-C", str(src), "init", "-b", "main"], check=True)
    subprocess.run(["git", "-C", str(src), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(src), "config", "user.name", "t"], check=True)
    (src / "x").write_text("x")
    subprocess.run(["git", "-C", str(src), "add", "."], check=True)
    subprocess.run(["git", "-C", str(src), "commit", "-m", "init"], check=True)

    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "dummy-token-for-test")

    (tmp_path / "fake.dump").write_bytes(b"DUMP")
    monkeypatch.setattr(cli.dump, "fetch", MagicMock(return_value=(tmp_path / "fake.dump", "etag")))

    build_calls = []
    exec_calls = []
    cp_calls = []

    monkeypatch.setattr(cli.docker, "build", lambda **kw: build_calls.append(kw) or MagicMock(returncode=0))
    monkeypatch.setattr(cli.docker, "compose_up", MagicMock())
    monkeypatch.setattr(cli.docker, "compose_logs_follow", MagicMock(return_value=MagicMock(pid=1)))
    monkeypatch.setattr(cli.docker, "exec_in_container",
                        lambda **kw: exec_calls.append(kw) or MagicMock(returncode=0))
    monkeypatch.setattr(cli.docker, "cp", lambda **kw: cp_calls.append(kw) or MagicMock(returncode=0))
    monkeypatch.setattr(cli, "_wait_for_db_ready", lambda **kw: None)
    monkeypatch.setattr(cli, "_wait_for_proxy_ready", lambda **kw: None)

    rc = cli.main([
        "start", "--repo", str(src), "--goal", "g",
        "--dump-bucket", "b", "--dump-key", "k", "--db-name", "appdb",
    ])
    assert rc == 0

    # Critical invariant: no docker build for a sandbox-db:* image
    sandbox_db_builds = [c for c in build_calls if str(c.get("tag", "")).startswith("sandbox-db")]
    assert sandbox_db_builds == [], f"sandbox-db image was built: {sandbox_db_builds}"

    # Dump was cp'd into the db container
    dump_cps = [c for c in cp_calls if "dump.dump" in str(c.get("dst", ""))]
    assert len(dump_cps) == 1, f"expected 1 dump cp, got {dump_cps}"
    assert "-db-1" in str(dump_cps[0]["dst"])

    # pg_restore was exec'd
    exec_cmds = [c["cmd"][0] for c in exec_calls]
    assert "pg_restore" in exec_cmds

    # Dump cleanup step: either shred or rm
    cleanup_present = any(c["cmd"][0] in ("shred", "rm") for c in exec_calls)
    assert cleanup_present, f"dump cleanup step missing in {exec_cmds}"


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


def test_finish_terminates_follower(sandbox_home, tmp_path, monkeypatch):
    import subprocess

    m = session.new_session(goal="g", repo="/tmp/r")
    m.status = "running"
    m.follower_pid = 9999
    session.save(m)
    sdir = session.session_dir(m.id)
    from sandbox import repo
    repo.init_bare_repo(sdir / "bare.git")

    # Build a valid output bundle (mirrors test_finish_imports_branch_into_bare)
    work = tmp_path / "work"
    work.mkdir()
    subprocess.run(["git", "-C", str(work), "init", "-b", "main"], check=True)
    subprocess.run(["git", "-C", str(work), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(work), "config", "user.name", "t"], check=True)
    (work / "f").write_text("f")
    subprocess.run(["git", "-C", str(work), "add", "."], check=True)
    subprocess.run(["git", "-C", str(work), "commit", "-m", "init"], check=True)
    subprocess.run(["git", "-C", str(work), "checkout", "-b", m.branch], check=True)
    (work / "g").write_text("g")
    subprocess.run(["git", "-C", str(work), "add", "."], check=True)
    subprocess.run(["git", "-C", str(work), "commit", "-m", "change"], check=True)
    bundle_src = tmp_path / "branch.bundle"
    subprocess.run(["git", "-C", str(work), "bundle", "create", str(bundle_src), "main", m.branch], check=True)

    def fake_cp(*, src, dst):
        if "branch.bundle" in str(src):
            import shutil as _sh
            _sh.copy(bundle_src, dst)
    monkeypatch.setattr(cli.docker, "cp", fake_cp)
    monkeypatch.setattr(cli.docker, "compose_ps", lambda **kw: MagicMock(stdout="[]"))
    monkeypatch.setattr(cli.docker, "compose_down", MagicMock())
    monkeypatch.setattr(cli.docker, "remove_images", MagicMock())

    terminate_calls = []
    monkeypatch.setattr(cli.docker, "terminate_pid", lambda pid, **kw: terminate_calls.append(pid))

    rc = cli.main(["finish", m.id])
    assert rc == 0
    assert terminate_calls == [9999]


def test_stop_terminates_follower(sandbox_home, monkeypatch):
    m = session.new_session(goal="g", repo="/tmp/r")
    m.status = "running"
    m.follower_pid = 1234
    session.save(m)

    monkeypatch.setattr(cli.docker, "compose_kill", MagicMock())
    monkeypatch.setattr(cli.docker, "compose_down", MagicMock())
    monkeypatch.setattr(cli.docker, "remove_images", MagicMock())
    monkeypatch.setattr(cli, "_wait_until_stopped", lambda **kw: True)

    terminate_calls = []
    monkeypatch.setattr(cli.docker, "terminate_pid", lambda pid, **kw: terminate_calls.append(pid))

    rc = cli.main(["stop", m.id])
    assert rc == 0
    assert terminate_calls == [1234]


def test_follower_termination_tolerates_missing_pid(sandbox_home, monkeypatch):
    m = session.new_session(goal="g", repo="/tmp/r")
    m.status = "running"
    m.follower_pid = None  # never started
    session.save(m)

    monkeypatch.setattr(cli.docker, "compose_kill", MagicMock())
    monkeypatch.setattr(cli.docker, "compose_down", MagicMock())
    monkeypatch.setattr(cli.docker, "remove_images", MagicMock())
    monkeypatch.setattr(cli, "_wait_until_stopped", lambda **kw: True)

    terminate_calls = []
    monkeypatch.setattr(cli.docker, "terminate_pid", lambda pid, **kw: terminate_calls.append(pid))

    rc = cli.main(["stop", m.id])
    assert rc == 0
    assert terminate_calls == []  # nothing to terminate


def test_list_prints_session_summary(sandbox_home, monkeypatch, capsys):
    import time as _t
    a = session.new_session(goal="goal A", repo="/tmp/a")
    a.status = "running"
    session.save(a)
    b = session.new_session(goal="goal B", repo="/tmp/b")
    b.status = "finished"
    b.finished_at = _t.time()
    session.save(b)

    rc = cli.main(["list"])
    assert rc == 0
    out = capsys.readouterr().out
    # Header row
    assert "ID" in out and "STATUS" in out
    # Both session ids appear
    assert a.id in out
    assert b.id in out
    # Both statuses appear
    assert "running" in out
    assert "finished" in out


def test_list_empty(sandbox_home, capsys):
    rc = cli.main(["list"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "no sessions" in out.lower()


def test_status_renders_timestamps_human_readable(sandbox_home, monkeypatch, capsys):
    m = session.new_session(goal="g", repo="/tmp/r")
    m.status = "running"
    session.save(m)
    monkeypatch.setattr(cli.docker, "compose_ps",
                        lambda **kw: MagicMock(stdout='[{"Service":"agent","State":"running"}]'))

    rc = cli.main(["status", m.id])
    out = capsys.readouterr().out
    assert rc == 0
    # The raw epoch float should not appear in the output
    assert str(m.started_at) not in out
    # ISO-style date prefix appears (year-month-day)
    import re
    assert re.search(r"\d{4}-\d{2}-\d{2}", out), f"no ISO date in output:\n{out}"


def test_status_renders_compose_state_human_readable(sandbox_home, monkeypatch, capsys):
    m = session.new_session(goal="g", repo="/tmp/r")
    m.status = "running"
    session.save(m)
    monkeypatch.setattr(cli.docker, "compose_ps",
                        lambda **kw: MagicMock(stdout='[{"Service":"agent","State":"running"},{"Service":"db","State":"healthy"}]'))

    rc = cli.main(["status", m.id])
    out = capsys.readouterr().out
    assert rc == 0
    # Should contain service=state pairs, not raw JSON
    assert "agent=running" in out
    assert "db=healthy" in out
    assert '"Service"' not in out  # raw JSON shouldn't leak through


def test_main_translates_docker_not_running_to_friendly_error(sandbox_home, monkeypatch, capsys):
    m = session.new_session(goal="g", repo="/tmp/r")
    m.status = "running"
    session.save(m)

    def boom(**kw):
        raise cli.docker.DockerNotRunning("daemon not reachable")
    monkeypatch.setattr(cli.docker, "compose_ps", boom)

    rc = cli.main(["status", m.id])
    err = capsys.readouterr().err
    assert rc != 0
    assert "Docker" in err
    assert ("Desktop" in err or "daemon" in err.lower())


def test_main_translates_no_aws_credentials_to_friendly_error(sandbox_home, tmp_path, monkeypatch, capsys):
    import subprocess
    src = tmp_path / "src"
    src.mkdir()
    subprocess.run(["git", "-C", str(src), "init", "-b", "main"], check=True)
    subprocess.run(["git", "-C", str(src), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(src), "config", "user.name", "t"], check=True)
    (src / "x").write_text("x")
    subprocess.run(["git", "-C", str(src), "add", "."], check=True)
    subprocess.run(["git", "-C", str(src), "commit", "-m", "init"], check=True)

    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "dummy-token-for-test")

    import botocore.exceptions
    def boom(b, k):
        raise botocore.exceptions.NoCredentialsError()
    monkeypatch.setattr(cli.dump, "fetch", boom)

    rc = cli.main([
        "start", "--repo", str(src), "--goal", "g",
        "--dump-bucket", "b", "--dump-key", "k", "--db-name", "appdb",
    ])
    err = capsys.readouterr().err
    assert rc != 0
    assert "AWS" in err
    assert ("credentials" in err.lower() or "AWS_ACCESS_KEY_ID" in err)


def test_main_translates_session_not_found_to_friendly_error(sandbox_home, capsys):
    rc = cli.main(["status", "DOES_NOT_EXIST"])
    err = capsys.readouterr().err
    assert rc != 0
    assert "no session" in err.lower()
    # And no traceback should leak through to stderr
    assert "Traceback" not in err


def test_resolve_allowlist_with_default_group():
    fqdns = cli._resolve_allowlist(groups="default", extra="")
    assert "api.anthropic.com" in fqdns
    # github group uses .github.com (wildcard) instead of bare github.com to
    # avoid Squid 6 rejecting redundant subdomain entries in the same ACL
    assert ".github.com" in fqdns
    assert "pypi.org" in fqdns
    # node group uses .npmjs.org (wildcard) instead of registry.npmjs.org
    assert ".npmjs.org" in fqdns


def test_resolve_allowlist_with_subset_of_groups():
    fqdns = cli._resolve_allowlist(groups="anthropic,github", extra="")
    assert "api.anthropic.com" in fqdns
    assert ".github.com" in fqdns
    assert "pypi.org" not in fqdns
    assert "registry.npmjs.org" not in fqdns


def test_resolve_allowlist_with_extra_domains():
    fqdns = cli._resolve_allowlist(groups="anthropic", extra="data.example.com,assets.example.com")
    assert "api.anthropic.com" in fqdns
    assert "data.example.com" in fqdns
    assert "assets.example.com" in fqdns


def test_resolve_allowlist_dedupes():
    fqdns = cli._resolve_allowlist(groups="anthropic", extra="api.anthropic.com,api.anthropic.com")
    assert len(fqdns) == len(set(fqdns))


def test_resolve_allowlist_rejects_unknown_group():
    with pytest.raises(ValueError, match="unknown egress group"):
        cli._resolve_allowlist(groups="anthropic,does_not_exist", extra="")


def test_resolve_allowlist_handles_empty_groups_and_extras():
    # Empty strings between commas, leading/trailing whitespace
    fqdns = cli._resolve_allowlist(groups="anthropic, , github", extra=" data.example.com, ")
    assert "api.anthropic.com" in fqdns
    assert ".github.com" in fqdns
    assert "data.example.com" in fqdns


def test_start_renders_session_squid_config_from_allowlist_flags(
    sandbox_home, tmp_path, monkeypatch, capsys
):
    import subprocess
    src = tmp_path / "src"
    src.mkdir()
    subprocess.run(["git", "-C", str(src), "init", "-b", "main"], check=True)
    subprocess.run(["git", "-C", str(src), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(src), "config", "user.name", "t"], check=True)
    (src / "x").write_text("x")
    subprocess.run(["git", "-C", str(src), "add", "."], check=True)
    subprocess.run(["git", "-C", str(src), "commit", "-m", "init"], check=True)

    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "dummy-token-for-test")

    (tmp_path / "fake.dump").write_bytes(b"DUMP")
    monkeypatch.setattr(cli.dump, "fetch", MagicMock(return_value=(tmp_path / "fake.dump", "etag")))
    monkeypatch.setattr(cli.docker, "build", MagicMock())
    monkeypatch.setattr(cli.docker, "compose_up", MagicMock())
    monkeypatch.setattr(cli.docker, "compose_logs_follow", MagicMock(return_value=MagicMock(pid=1)))
    monkeypatch.setattr(cli.docker, "exec_in_container", MagicMock(return_value=MagicMock(returncode=0)))
    monkeypatch.setattr(cli.docker, "cp", MagicMock(return_value=MagicMock(returncode=0)))
    monkeypatch.setattr(cli, "_wait_for_db_ready", lambda **kw: None)
    monkeypatch.setattr(cli, "_wait_for_proxy_ready", lambda **kw: None)

    rc = cli.main([
        "start", "--repo", str(src), "--goal", "g",
        "--dump-bucket", "b", "--dump-key", "k", "--db-name", "appdb",
        "--egress-allowlist", "anthropic",
        "--extra-egress-allowlist", "data.example.com",
    ])
    assert rc == 0

    sids = list((sandbox_home / "sessions").iterdir())
    assert len(sids) == 1
    squid_conf = sids[0] / "proxy" / "allowlist.conf"
    assert squid_conf.is_file()
    text = squid_conf.read_text()
    assert "api.anthropic.com" in text
    assert "data.example.com" in text
    assert "github.com" not in text  # not requested


def test_start_installs_proxy_config_before_agent_inputs(sandbox_home, tmp_path, monkeypatch, capsys):
    """The proxy config must be cp'd into the proxy container BEFORE the agent
    gets its repo bundle. Otherwise the agent could start making HTTPS calls
    while the proxy has no allowlist."""
    import subprocess
    src = tmp_path / "src"
    src.mkdir()
    subprocess.run(["git", "-C", str(src), "init", "-b", "main"], check=True)
    subprocess.run(["git", "-C", str(src), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(src), "config", "user.name", "t"], check=True)
    (src / "x").write_text("x")
    subprocess.run(["git", "-C", str(src), "add", "."], check=True)
    subprocess.run(["git", "-C", str(src), "commit", "-m", "init"], check=True)

    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "dummy-token-for-test")

    (tmp_path / "fake.dump").write_bytes(b"DUMP")
    monkeypatch.setattr(cli.dump, "fetch", MagicMock(return_value=(tmp_path / "fake.dump", "etag")))

    cp_call_order = []
    def fake_cp(*, src, dst):
        cp_call_order.append((str(src), str(dst)))
        return MagicMock(returncode=0)
    monkeypatch.setattr(cli.docker, "cp", fake_cp)
    monkeypatch.setattr(cli.docker, "build", MagicMock())
    monkeypatch.setattr(cli.docker, "compose_up", MagicMock())
    monkeypatch.setattr(cli.docker, "compose_logs_follow", MagicMock(return_value=MagicMock(pid=1)))
    monkeypatch.setattr(cli.docker, "exec_in_container", MagicMock(return_value=MagicMock(returncode=0)))
    monkeypatch.setattr(cli, "_wait_for_db_ready", lambda **kw: None)
    monkeypatch.setattr(cli, "_wait_for_proxy_ready", lambda **kw: None)

    rc = cli.main([
        "start", "--repo", str(src), "--goal", "g",
        "--dump-bucket", "b", "--dump-key", "k", "--db-name", "appdb",
    ])
    assert rc == 0

    squid_idx = next(i for i, (s, d) in enumerate(cp_call_order) if "allowlist.conf" in s)
    bundle_idx = next(i for i, (s, d) in enumerate(cp_call_order) if "repo.bundle" in s)
    assert squid_idx < bundle_idx, (
        f"squid config (idx {squid_idx}) must be cp'd before repo.bundle (idx {bundle_idx})"
    )


def test_resolve_host_auth_returns_oauth_token_when_set(monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oauth-fake-123")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    name, value = cli._resolve_host_auth()
    assert name == "CLAUDE_CODE_OAUTH_TOKEN"
    assert value == "sk-ant-oauth-fake-123"


def test_resolve_host_auth_prefers_oauth_token_over_api_key(monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "oauth-tok")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api-99")
    name, value = cli._resolve_host_auth()
    assert name == "CLAUDE_CODE_OAUTH_TOKEN"
    assert value == "oauth-tok"


def test_resolve_host_auth_falls_back_to_api_key(monkeypatch):
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api-77")
    name, value = cli._resolve_host_auth()
    assert name == "ANTHROPIC_API_KEY"
    assert value == "sk-ant-api-77"


def test_resolve_host_auth_treats_empty_string_as_unset(monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api-1")
    name, value = cli._resolve_host_auth()
    # Empty CLAUDE_CODE_OAUTH_TOKEN should be ignored; fall through to API key
    assert name == "ANTHROPIC_API_KEY"


def test_resolve_host_auth_raises_when_neither_set(monkeypatch):
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(cli.HostAuthMissing, match="claude setup-token"):
        cli._resolve_host_auth()
