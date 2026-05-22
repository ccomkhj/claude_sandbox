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
    fake_cp = MagicMock()
    fake_logs = MagicMock(return_value=MagicMock(pid=4242))
    monkeypatch.setattr(cli.docker, "build", fake_build)
    monkeypatch.setattr(cli.docker, "compose_up", fake_up)
    monkeypatch.setattr(cli.docker, "cp", fake_cp)
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
    assert meta.agent_image == f"sandbox-agent:{sid}"
    assert meta.db_image == f"sandbox-db:{sid.lower()}"

    sdir = session.session_dir(sid)
    assert (sdir / "compose.yml").exists()
    assert not (sdir / "build" / "agent" / "repo.bundle").exists()
    assert not (sdir / "build" / "agent" / ".credentials.json").exists()
    assert not (sdir / "build" / "db" / "dump.dump").exists()
    assert (sdir / "bare.git" / "HEAD").exists()

    fake_dump.assert_called_once_with("dumps", "prod/latest.dump")
    fake_build.assert_called_once()
    fake_up.assert_called_once()
    cp_sources = [call.kwargs["src"] for call in fake_cp.call_args_list]
    assert str(sdir / "input" / "repo.bundle") in cp_sources
    assert str(sdir / "input" / ".credentials.json") in cp_sources
    fake_logs.assert_called_once()


def test_start_aborts_when_creds_missing(sandbox_home, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HOME", str(tmp_path / "empty_home"))
    rc = cli.main([
        "start", "--repo", str(tmp_path), "--goal", "g",
        "--dump-bucket", "b", "--dump-key", "k", "--db-name", "appdb",
    ])
    assert rc != 0
    assert "credentials" in capsys.readouterr().err.lower()


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

    creds = tmp_path / "creds_home" / ".claude"
    creds.mkdir(parents=True)
    (creds / ".credentials.json").write_text("{}")
    monkeypatch.setenv("HOME", str(tmp_path / "creds_home"))

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

    creds = tmp_path / "creds_home" / ".claude"
    creds.mkdir(parents=True)
    (creds / ".credentials.json").write_text("{}")
    monkeypatch.setenv("HOME", str(tmp_path / "creds_home"))

    (tmp_path / "fake.dump").write_bytes(b"x")
    monkeypatch.setattr(cli.dump, "fetch", MagicMock(return_value=(tmp_path / "fake.dump", "e")))
    monkeypatch.setattr(cli.docker, "build", MagicMock())
    monkeypatch.setattr(cli.docker, "compose_up", MagicMock())
    monkeypatch.setattr(cli.docker, "cp", MagicMock(side_effect=RuntimeError("cp boom")))
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
    remove_images.assert_called_once_with(m.agent_image, m.db_image)
    sdir = session.session_dir(m.id)
    assert not (sdir / "input" / "repo.bundle").exists()
    assert not (sdir / "input" / ".credentials.json").exists()


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

    creds = tmp_path / "creds_home" / ".claude"
    creds.mkdir(parents=True)
    (creds / ".credentials.json").write_text("{}")
    monkeypatch.setenv("HOME", str(tmp_path / "creds_home"))

    (tmp_path / "fake.dump").write_bytes(b"x")
    monkeypatch.setattr(cli.dump, "fetch", MagicMock(return_value=(tmp_path / "fake.dump", "e")))
    monkeypatch.setattr(cli.docker, "build", MagicMock())
    fake_up = MagicMock()
    fake_cp = MagicMock()
    fake_logs = MagicMock(return_value=MagicMock(pid=1))
    monkeypatch.setattr(cli.docker, "compose_up", fake_up)
    monkeypatch.setattr(cli.docker, "cp", fake_cp)
    monkeypatch.setattr(cli.docker, "compose_logs_follow", fake_logs)

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
