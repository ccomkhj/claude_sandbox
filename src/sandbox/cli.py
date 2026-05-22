from __future__ import annotations

import argparse
import json as _json
import shutil
import sys
import time as _time
from pathlib import Path

from sandbox import compose, docker, dump, repo, session


def _agent_image_tag(base: str, sid: str) -> str:
    return f"{base}:{sid}"


def _db_image_tag(sid: str) -> str:
    return f"sandbox-db:{sid.lower()}"


def _cleanup_sensitive_build_inputs(sdir: Path) -> None:
    for rel in (
        "build/agent/repo.bundle",
        "build/agent/.credentials.json",
        "build/db/dump.dump",
        "input/repo.bundle",
        "input/.credentials.json",
    ):
        try:
            (sdir / rel).unlink(missing_ok=True)
        except Exception:
            pass


def _cleanup_session_images(meta: session.Meta) -> None:
    images = [image for image in (meta.agent_image, meta.db_image) if image]
    if images:
        try:
            docker.remove_images(*images)
        except Exception:
            pass


def _cleanup_compose_project(meta: session.Meta) -> None:
    compose_file = session.session_dir(meta.id) / "compose.yml"
    if not compose_file.exists():
        return
    try:
        docker.compose_down(
            project=meta.id.lower(),
            compose_file=compose_file,
            volumes=True,
            rmi_local=True,
        )
    except Exception:
        pass


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="sandbox")
    sub = p.add_subparsers(dest="verb", required=True)

    sp = sub.add_parser("start", help="start a new sandboxed session")
    sp.add_argument("--repo", required=True, help="host path to the repo to copy into the sandbox")
    sp.add_argument("--goal", required=True, help="goal prompt passed to Claude Code")
    sp.add_argument("--dump-bucket", required=True)
    sp.add_argument("--dump-key", required=True)
    sp.add_argument("--db-name", default="appdb")
    sp.add_argument("--agent-image", default="sandbox-agent", help="base name of per-session agent image")

    for verb in ("status", "logs", "finish", "stop"):
        s = sub.add_parser(verb)
        s.add_argument("session", help="session id or unique prefix")
        if verb == "logs":
            s.add_argument("-f", "--follow", action="store_true")

    sub.add_parser("prune", help="remove finished sessions older than 30 days")

    return p


def _images_root() -> Path:
    # repo layout: <repo>/src/sandbox/cli.py and <repo>/images/
    return Path(__file__).resolve().parents[2] / "images"


def cmd_start(args: argparse.Namespace) -> int:
    creds_src = Path.home() / ".claude" / ".credentials.json"
    if not creds_src.is_file():
        print(
            "error: ~/.claude/.credentials.json not found. "
            "Run `claude login` on the host before starting a sandbox.",
            file=sys.stderr,
        )
        return 2

    meta = session.new_session(goal=args.goal, repo=args.repo)
    try:
        return _start_session(meta, args, creds_src)
    except Exception:
        # Mark the partial session failed so prune can reclaim it later.
        import time as _time
        _cleanup_sensitive_build_inputs(session.session_dir(meta.id))
        _cleanup_compose_project(meta)
        _cleanup_session_images(meta)
        try:
            meta.status = "failed"
            meta.finished_at = _time.time()
            session.save(meta)
        except Exception:
            pass
        raise


def _start_session(meta: session.Meta, args: argparse.Namespace, creds_src: Path) -> int:
    sdir = session.session_dir(meta.id)
    project = meta.id.lower()
    meta.agent_image = _agent_image_tag(args.agent_image, meta.id)
    meta.db_image = _db_image_tag(meta.id)
    session.save(meta)

    try:
        # 1. Fetch dump (cached by ETag)
        local_dump, _etag = dump.fetch(args.dump_bucket, args.dump_key)

        # 2. Assemble db build context
        db_build = sdir / "build" / "db"
        db_build.mkdir(parents=True)
        shutil.copy(_images_root() / "db" / "Dockerfile", db_build / "Dockerfile")
        shutil.copy(_images_root() / "db" / "init.sh", db_build / "init.sh")
        shutil.copy(local_dump, db_build / "dump.dump")
        docker.build(context=db_build, tag=meta.db_image)

        # 3. Assemble agent build context
        agent_build = sdir / "build" / "agent"
        agent_build.mkdir(parents=True)
        shutil.copy(_images_root() / "agent" / "Dockerfile", agent_build / "Dockerfile")
        shutil.copy(_images_root() / "agent" / "entrypoint.sh", agent_build / "entrypoint.sh")

        input_dir = sdir / "input"
        input_dir.mkdir(parents=True)
        repo.bundle_host_repo(Path(args.repo), input_dir / "repo.bundle")
        shutil.copy(creds_src, input_dir / ".credentials.json")
        (input_dir / ".credentials.json").chmod(0o600)

        # 4. Bare repo for the user to fetch from
        repo.init_bare_repo(sdir / "bare.git")

        # 5. Render and write compose.yml
        cfg = compose.ComposeConfig(
            session_id=meta.id,
            goal=args.goal,
            db_image=meta.db_image,
            agent_image_name=args.agent_image,
            build_dir_db="./build/db",
            build_dir_agent="./build/agent",
            db_name=args.db_name,
        )
        (sdir / "compose.yml").write_text(compose.render(cfg))

        # 6. Up (compose template lowercases the project name)
        docker.compose_up(project=project, compose_file=sdir / "compose.yml", build=True, detach=True)

        container = docker.container_name(project=project, service="agent")
        docker.cp(
            src=str(input_dir / "repo.bundle"),
            dst=f"{container}:/input/repo.bundle",
        )
        docker.cp(
            src=str(input_dir / ".credentials.json"),
            dst=f"{container}:/input/.credentials.json",
        )
    finally:
        _cleanup_sensitive_build_inputs(sdir)

    # 7. Start log follower
    follower = docker.compose_logs_follow(
        project=project,
        compose_file=sdir / "compose.yml",
        stdout_path=sdir / "logs" / "agent.log",
    )

    meta.status = "running"
    meta.follower_pid = follower.pid
    session.save(meta)

    print(meta.id)
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    meta = session.find(args.session)
    sdir = session.session_dir(meta.id)
    try:
        ps = docker.compose_ps(project=meta.id.lower(), compose_file=sdir / "compose.yml")
        compose_state = ps.stdout
    except Exception as e:
        compose_state = f"<compose ps failed: {e}>"
    print(f"id:      {meta.id}")
    print(f"status:  {meta.status}")
    print(f"goal:    {meta.goal}")
    print(f"repo:    {meta.repo}")
    print(f"branch:  {meta.branch}")
    print(f"started: {meta.started_at}")
    print(f"compose: {compose_state.strip()}")
    return 0


def cmd_logs(args: argparse.Namespace) -> int:
    meta = session.find(args.session)
    log_path = session.session_dir(meta.id) / "logs" / "agent.log"
    if not log_path.is_file():
        print(f"no logs at {log_path}", file=sys.stderr)
        return 1
    if not args.follow:
        sys.stdout.write(log_path.read_text())
        return 0
    # Follow: simple tail -f
    import time as _time
    with log_path.open() as fh:
        fh.seek(0, 2)  # end
        while True:
            line = fh.readline()
            if not line:
                _time.sleep(0.5)
                continue
            sys.stdout.write(line)
            sys.stdout.flush()


def _is_running(project: str, compose_file: Path) -> bool:
    """Return True if the agent service is still running.

    Only the agent service matters for finish/stop decisions — the db service
    stays up indefinitely and must not prevent cmd_finish from proceeding.
    """
    try:
        ps = docker.compose_ps(project=project, compose_file=compose_file)
    except Exception:
        return False
    try:
        rows = _json.loads(ps.stdout or "[]")
    except Exception:
        return False
    if isinstance(rows, dict):
        rows = [rows]
    return any(r.get("Service") == "agent" and r.get("State") == "running" for r in rows)


def cmd_finish(args: argparse.Namespace) -> int:
    meta = session.find(args.session)
    sdir = session.session_dir(meta.id)
    compose_file = sdir / "compose.yml"
    project = meta.id.lower()

    if _is_running(project, compose_file):
        print(
            f"session {meta.id} is still running. Use `sandbox stop {meta.id}` first.",
            file=sys.stderr,
        )
        return 2

    # Pull branch bundle out of the agent container
    bundle_dst = sdir / "branch.bundle"
    container = docker.container_name(project=project, service="agent")
    try:
        docker.cp(src=f"{container}:/output/branch.bundle", dst=bundle_dst)
    except Exception as e:
        print(f"failed to copy branch bundle: {e}", file=sys.stderr)
        return 1

    exit_code = _copy_agent_exit_code(container=container, sdir=sdir)
    base_branch = _copy_agent_base_branch(container=container, sdir=sdir) or "main"

    # Fetch the base branch first so format_patch has a base ref. The bundle
    # carries both refs when the agent's BASE_BRANCH existed (Task 8 contract);
    # if it doesn't, format_patch falls back to --root, which is acceptable.
    try:
        repo.fetch_bundle_into_bare(bundle=bundle_dst, bare=sdir / "bare.git", branch=base_branch)
    except Exception:
        pass  # base ref absent; format_patch will use --root
    repo.fetch_bundle_into_bare(bundle=bundle_dst, bare=sdir / "bare.git", branch=meta.branch)
    (sdir / "patch.diff").write_text(
        repo.format_patch(bare=sdir / "bare.git", branch=meta.branch, base=base_branch)
    )

    # Tear down compose resources, then explicitly remove custom-tagged images.
    docker.compose_down(project=project, compose_file=compose_file, volumes=True, rmi_local=True)
    _cleanup_session_images(meta)

    meta.status = "finished" if exit_code == 0 else "crashed"
    meta.exit_code = exit_code
    meta.base_branch = base_branch
    meta.finished_at = _time.time()
    session.save(meta)

    print(f"branch ready: git fetch {sdir / 'bare.git'} {meta.branch}")
    print(f"patch:        {sdir / 'patch.diff'}")
    return 0


def _copy_agent_output_text(*, container: str, name: str, sdir: Path) -> str | None:
    dst = sdir / name
    try:
        docker.cp(src=f"{container}:/output/{name}", dst=dst)
    except Exception:
        return None
    try:
        return dst.read_text().strip()
    except Exception:
        return None


def _copy_agent_exit_code(*, container: str, sdir: Path) -> int:
    raw = _copy_agent_output_text(container=container, name="exit_code", sdir=sdir)
    try:
        return int(raw or "0")
    except ValueError:
        return 0


def _copy_agent_base_branch(*, container: str, sdir: Path) -> str | None:
    raw = _copy_agent_output_text(container=container, name="base_branch", sdir=sdir)
    if raw and any(ch.isspace() for ch in raw):
        return None
    if raw and len(raw) > 200:
        return None
    return raw or None


def _wait_until_stopped(*, project: str, compose_file: Path, timeout_s: float = 30.0) -> bool:
    start = _time.time()
    while _time.time() - start < timeout_s:
        if not _is_running(project, compose_file):
            return True
        _time.sleep(1.0)
    return False


def cmd_stop(args: argparse.Namespace) -> int:
    meta = session.find(args.session)
    sdir = session.session_dir(meta.id)
    compose_file = sdir / "compose.yml"
    project = meta.id.lower()
    try:
        docker.compose_kill(project=project, compose_file=compose_file, signal="SIGTERM")
    except Exception:
        pass
    if not _wait_until_stopped(project=project, compose_file=compose_file):
        # force teardown if SIGTERM didn't take
        try:
            docker.compose_kill(project=project, compose_file=compose_file, signal="SIGKILL")
        except Exception:
            pass
    docker.compose_down(project=project, compose_file=compose_file, volumes=True, rmi_local=True)
    _cleanup_session_images(meta)
    meta.status = "stopped"
    meta.finished_at = _time.time()
    session.save(meta)
    print(f"stopped {meta.id}")
    return 0


def cmd_prune(args: argparse.Namespace) -> int:
    cutoff = _time.time() - 60 * 60 * 24 * 30  # 30 days
    removed = []
    for meta in session.all_sessions():
        if meta.status in ("finished", "failed", "crashed", "stopped"):
            if meta.finished_at is not None and meta.finished_at < cutoff:
                shutil.rmtree(session.session_dir(meta.id))
                removed.append(meta.id)
    print(f"pruned {len(removed)} session(s)")
    for sid in removed:
        print(f"  {sid}")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    dispatch = {
        "start": cmd_start,
        "status": cmd_status,
        "logs": cmd_logs,
        "finish": cmd_finish,
        "stop": cmd_stop,
        "prune": cmd_prune,
    }
    handler = dispatch.get(args.verb)
    if handler is None:
        print(f"verb {args.verb!r} not implemented yet", file=sys.stderr)
        return 1
    return handler(args)
