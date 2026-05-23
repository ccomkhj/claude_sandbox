import subprocess
from pathlib import Path

import pytest

from sandbox import docker


def make_fake_run(captured):
    """Return a fake _run that appends argv to `captured` and returns a successful CompletedProcess."""
    def fake(argv, **kwargs):
        captured.append(argv)
        return subprocess.CompletedProcess(argv, 0, "", "")
    return fake


def test_compose_up_argv(monkeypatch):
    captured = []
    monkeypatch.setattr(docker, "_run", make_fake_run(captured))
    docker.compose_up(project="abc", compose_file=Path("/x/compose.yml"), build=True, detach=True)
    assert captured == [[
        "docker", "compose", "-p", "abc", "-f", "/x/compose.yml",
        "up", "-d", "--build",
    ]]


def test_compose_down_argv(monkeypatch):
    captured = []
    monkeypatch.setattr(docker, "_run", make_fake_run(captured))
    docker.compose_down(project="abc", compose_file=Path("/x/compose.yml"), volumes=True, rmi_local=True)
    assert captured == [[
        "docker", "compose", "-p", "abc", "-f", "/x/compose.yml",
        "down", "-v", "--rmi", "local",
    ]]


def test_compose_ps_argv(monkeypatch):
    captured = []
    monkeypatch.setattr(docker, "_run", make_fake_run(captured))
    docker.compose_ps(project="abc", compose_file=Path("/x/compose.yml"))
    assert captured == [[
        "docker", "compose", "-p", "abc", "-f", "/x/compose.yml",
        "ps", "--format", "json",
    ]]


def test_compose_kill_argv(monkeypatch):
    captured = []
    monkeypatch.setattr(docker, "_run", make_fake_run(captured))
    docker.compose_kill(project="abc", compose_file=Path("/x/compose.yml"), signal="SIGTERM")
    assert captured == [[
        "docker", "compose", "-p", "abc", "-f", "/x/compose.yml",
        "kill", "-s", "SIGTERM",
    ]]


def test_docker_cp_argv(monkeypatch):
    captured = []
    monkeypatch.setattr(docker, "_run", make_fake_run(captured))
    docker.cp(src="abc-agent-1:/output/branch.bundle", dst=Path("/host/branch.bundle"))
    assert captured == [["docker", "cp", "abc-agent-1:/output/branch.bundle", "/host/branch.bundle"]]


def test_docker_build_argv(monkeypatch):
    captured = []
    monkeypatch.setattr(docker, "_run", make_fake_run(captured))
    docker.build(context=Path("/x/build/db"), tag="sandbox-db:etag123")
    assert captured == [["docker", "build", "-t", "sandbox-db:etag123", "/x/build/db"]]


def test_remove_images_argv(monkeypatch):
    captured = []
    monkeypatch.setattr(docker, "_run", make_fake_run(captured))
    docker.remove_images("sandbox-agent:abc", "sandbox-db:def")
    assert captured == [[
        "docker", "image", "rm", "-f", "sandbox-agent:abc", "sandbox-db:def",
    ]]


def test_container_name():
    assert docker.container_name(project="abc", service="agent") == "abc-agent-1"
    assert docker.container_name(project="abc", service="db", index=2) == "abc-db-2"


def test_compose_logs_follow_argv_and_kwargs(monkeypatch, tmp_path):
    captured = {}

    class FakeProc:
        pass

    def fake_popen(argv, **kwargs):
        captured["argv"] = argv
        captured["kwargs"] = kwargs
        return FakeProc()

    monkeypatch.setattr(docker.subprocess, "Popen", fake_popen)
    log_path = tmp_path / "logs" / "agent.log"
    proc = docker.compose_logs_follow(
        project="abc",
        compose_file=Path("/x/compose.yml"),
        stdout_path=log_path,
    )
    assert isinstance(proc, FakeProc)
    assert captured["argv"] == [
        "docker", "compose", "-p", "abc", "-f", "/x/compose.yml",
        "logs", "-f", "--no-color",
    ]
    assert captured["kwargs"]["start_new_session"] is True
    assert captured["kwargs"]["stderr"] == subprocess.STDOUT
    # The parent dir was created
    assert log_path.parent.is_dir()


def test_exec_in_container_argv(monkeypatch):
    captured = []
    monkeypatch.setattr(docker, "_run", make_fake_run(captured))
    docker.exec_in_container(container="abc-db-1", cmd=["pg_restore", "--dbname", "appdb", "/tmp/dump.dump"])
    assert captured == [["docker", "exec", "abc-db-1", "pg_restore", "--dbname", "appdb", "/tmp/dump.dump"]]


def test_terminate_pid_sends_sigterm_then_sigkill(monkeypatch):
    import os
    import signal as _signal
    calls = []

    def fake_kill(pid, sig):
        calls.append((pid, sig))
        # Pretend process never exits — always succeeds with signal 0 too.

    # Fast clock so the timeout fires quickly
    fake_time = [0.0]
    def fake_monotonic():
        fake_time[0] += 1.0  # each call advances by 1s
        return fake_time[0]

    monkeypatch.setattr(os, "kill", fake_kill)
    monkeypatch.setattr(docker.time, "monotonic", fake_monotonic)
    monkeypatch.setattr(docker.time, "sleep", lambda s: None)

    docker.terminate_pid(4242, timeout_s=2.0)

    signals = [sig for _pid, sig in calls]
    assert _signal.SIGTERM in signals  # first signal sent
    assert _signal.SIGKILL in signals  # eventually escalated


def test_terminate_pid_exits_early_when_process_already_gone(monkeypatch):
    import os
    calls = []

    def fake_kill(pid, sig):
        calls.append((pid, sig))
        # First call (SIGTERM) succeeds; subsequent probe (signal 0) reports gone
        if len(calls) >= 2:
            raise ProcessLookupError()

    monkeypatch.setattr(os, "kill", fake_kill)
    monkeypatch.setattr(docker.time, "sleep", lambda s: None)
    # monotonic just needs to not infinite-loop
    monkeypatch.setattr(docker.time, "monotonic", lambda: 0.0)

    docker.terminate_pid(4242, timeout_s=5.0)

    # SIGTERM was sent; SIGKILL should NOT be sent (process exited before timeout)
    signals = [sig for _pid, sig in calls]
    import signal as _signal
    assert _signal.SIGKILL not in signals


def test_terminate_pid_tolerates_already_gone(monkeypatch):
    import os
    def fake_kill(pid, sig):
        raise ProcessLookupError()
    monkeypatch.setattr(os, "kill", fake_kill)
    # Should not raise
    docker.terminate_pid(4242)
