import subprocess
from pathlib import Path

from sandbox import docker


def test_compose_up_argv(monkeypatch):
    captured = []

    def fake_run(argv, **kwargs):
        captured.append(argv)
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(docker, "_run", fake_run)
    docker.compose_up(project="abc", compose_file=Path("/x/compose.yml"), build=True, detach=True)
    assert captured == [[
        "docker", "compose",
        "-p", "abc",
        "-f", "/x/compose.yml",
        "up", "-d", "--build",
    ]]


def test_compose_down_argv(monkeypatch):
    captured = []
    monkeypatch.setattr(docker, "_run", lambda argv, **k: captured.append(argv) or subprocess.CompletedProcess(argv, 0, "", ""))
    docker.compose_down(project="abc", compose_file=Path("/x/compose.yml"), volumes=True, rmi_local=True)
    assert captured == [[
        "docker", "compose",
        "-p", "abc",
        "-f", "/x/compose.yml",
        "down", "-v", "--rmi", "local",
    ]]


def test_compose_ps_argv(monkeypatch):
    captured = []
    monkeypatch.setattr(docker, "_run", lambda argv, **k: captured.append(argv) or subprocess.CompletedProcess(argv, 0, '[{"Name":"x"}]', ""))
    docker.compose_ps(project="abc", compose_file=Path("/x/compose.yml"))
    assert captured == [[
        "docker", "compose",
        "-p", "abc",
        "-f", "/x/compose.yml",
        "ps", "--format", "json",
    ]]


def test_compose_kill_argv(monkeypatch):
    captured = []
    monkeypatch.setattr(docker, "_run", lambda argv, **k: captured.append(argv) or subprocess.CompletedProcess(argv, 0, "", ""))
    docker.compose_kill(project="abc", compose_file=Path("/x/compose.yml"), signal="SIGTERM")
    assert captured == [[
        "docker", "compose",
        "-p", "abc",
        "-f", "/x/compose.yml",
        "kill", "-s", "SIGTERM",
    ]]


def test_docker_cp_argv(monkeypatch):
    captured = []
    monkeypatch.setattr(docker, "_run", lambda argv, **k: captured.append(argv) or subprocess.CompletedProcess(argv, 0, "", ""))
    docker.cp(src="abc-agent-1:/output/branch.bundle", dst=Path("/host/branch.bundle"))
    assert captured == [["docker", "cp", "abc-agent-1:/output/branch.bundle", "/host/branch.bundle"]]


def test_docker_build_argv(monkeypatch):
    captured = []
    monkeypatch.setattr(docker, "_run", lambda argv, **k: captured.append(argv) or subprocess.CompletedProcess(argv, 0, "", ""))
    docker.build(context=Path("/x/build/db"), tag="sandbox-db:etag123")
    assert captured == [["docker", "build", "-t", "sandbox-db:etag123", "/x/build/db"]]
