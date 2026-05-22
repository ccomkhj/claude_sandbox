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
