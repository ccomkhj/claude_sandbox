from __future__ import annotations

import os
import signal as _signal
import subprocess
import time
from pathlib import Path


class DockerNotRunning(RuntimeError):
    """Raised when the Docker daemon is not reachable."""


def _run(argv: list[str], *, check: bool = True, capture: bool = True) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(argv, check=check, capture_output=capture, text=True)
    except subprocess.CalledProcessError as e:
        stderr = (e.stderr or "").lower()
        if any(s in stderr for s in (
            "cannot connect to the docker daemon",
            "is the docker daemon running",
            "daemon socket",
        )):
            raise DockerNotRunning(
                e.stderr.strip() if e.stderr else "Docker daemon not reachable"
            ) from e
        raise


def _compose_base(project: str, compose_file: Path) -> list[str]:
    return ["docker", "compose", "-p", project, "-f", str(compose_file)]


def compose_up(*, project: str, compose_file: Path, build: bool = True, detach: bool = True) -> subprocess.CompletedProcess:
    argv = _compose_base(project, compose_file) + ["up"]
    if detach:
        argv.append("-d")
    if build:
        argv.append("--build")
    return _run(argv, capture=False)


def compose_down(*, project: str, compose_file: Path, volumes: bool = True, rmi_local: bool = True) -> subprocess.CompletedProcess:
    argv = _compose_base(project, compose_file) + ["down"]
    if volumes:
        argv.append("-v")
    if rmi_local:
        argv += ["--rmi", "local"]
    return _run(argv, capture=False)


def compose_ps(*, project: str, compose_file: Path) -> subprocess.CompletedProcess:
    return _run(_compose_base(project, compose_file) + ["ps", "--format", "json"])


def compose_kill(*, project: str, compose_file: Path, signal: str = "SIGTERM") -> subprocess.CompletedProcess:
    return _run(_compose_base(project, compose_file) + ["kill", "-s", signal])


def compose_logs_follow(*, project: str, compose_file: Path, stdout_path: Path) -> subprocess.Popen:
    """Spawn a detached `docker compose logs -f` whose stdout/stderr go to a file."""
    argv = _compose_base(project, compose_file) + ["logs", "-f", "--no-color"]
    stdout_path.parent.mkdir(parents=True, exist_ok=True)
    log_file = open(stdout_path, "ab", buffering=0)
    try:
        proc = subprocess.Popen(
            argv,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    finally:
        log_file.close()  # Popen dup'd the fd into the child; parent doesn't need it
    return proc


def cp(*, src: str, dst: Path | str) -> subprocess.CompletedProcess:
    return _run(["docker", "cp", src, str(dst)])


def build(*, context: Path, tag: str) -> subprocess.CompletedProcess:
    return _run(["docker", "build", "-t", tag, str(context)])


def remove_images(*images: str) -> subprocess.CompletedProcess:
    if not images:
        return subprocess.CompletedProcess([], 0, "", "")
    return _run(["docker", "image", "rm", "-f", *images], check=False)


def container_name(*, project: str, service: str, index: int = 1) -> str:
    return f"{project}-{service}-{index}"


def exec_in_container(*, container: str, cmd: list[str]) -> subprocess.CompletedProcess:
    return _run(["docker", "exec", container, *cmd])


def terminate_pid(pid: int, timeout_s: float = 5.0) -> None:
    """SIGTERM the process, wait up to timeout_s for it to exit, then SIGKILL.

    Tolerates ProcessLookupError at every step (process already gone).
    """
    try:
        os.kill(pid, _signal.SIGTERM)
    except ProcessLookupError:
        return
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)  # probe — does process still exist?
        except ProcessLookupError:
            return
        time.sleep(0.2)
    try:
        os.kill(pid, _signal.SIGKILL)
    except ProcessLookupError:
        pass
