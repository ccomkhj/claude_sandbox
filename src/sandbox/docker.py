from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Optional


def _run(argv: list[str], *, check: bool = True, capture: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(argv, check=check, capture_output=capture, text=True)


def _compose_base(project: str, compose_file: Path) -> list[str]:
    return ["docker", "compose", "-p", project, "-f", str(compose_file)]


def compose_up(*, project: str, compose_file: Path, build: bool = True, detach: bool = True) -> subprocess.CompletedProcess:
    argv = _compose_base(project, compose_file) + ["up"]
    if detach:
        argv.append("-d")
    if build:
        argv.append("--build")
    return _run(argv)


def compose_down(*, project: str, compose_file: Path, volumes: bool = True, rmi_local: bool = True) -> subprocess.CompletedProcess:
    argv = _compose_base(project, compose_file) + ["down"]
    if volumes:
        argv.append("-v")
    if rmi_local:
        argv += ["--rmi", "local"]
    return _run(argv)


def compose_ps(*, project: str, compose_file: Path) -> subprocess.CompletedProcess:
    return _run(_compose_base(project, compose_file) + ["ps", "--format", "json"])


def compose_kill(*, project: str, compose_file: Path, signal: str = "SIGTERM") -> subprocess.CompletedProcess:
    return _run(_compose_base(project, compose_file) + ["kill", "-s", signal])


def compose_logs_follow(*, project: str, compose_file: Path, stdout_path: Path) -> subprocess.Popen:
    """Spawn a detached `docker compose logs -f` whose stdout/stderr go to a file."""
    argv = _compose_base(project, compose_file) + ["logs", "-f", "--no-color"]
    stdout_path.parent.mkdir(parents=True, exist_ok=True)
    log_file = open(stdout_path, "ab", buffering=0)
    return subprocess.Popen(
        argv,
        stdout=log_file,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )


def cp(*, src: str, dst: Path) -> subprocess.CompletedProcess:
    return _run(["docker", "cp", src, str(dst)])


def build(*, context: Path, tag: str) -> subprocess.CompletedProcess:
    return _run(["docker", "build", "-t", tag, str(context)])


def container_name(*, project: str, service: str, index: int = 1) -> str:
    return f"{project}-{service}-{index}"
