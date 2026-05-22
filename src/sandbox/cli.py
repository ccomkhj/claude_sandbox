from __future__ import annotations

import argparse
import json as _json
import shutil
import sys
import time as _time
from pathlib import Path

from sandbox import compose, docker, dump, repo, session


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

    # 1. Fetch dump (cached by ETag)
    local_dump, etag = dump.fetch(args.dump_bucket, args.dump_key)

    # 2. Assemble db build context
    db_build = sdir / "build" / "db"
    db_build.mkdir(parents=True)
    shutil.copy(_images_root() / "db" / "Dockerfile", db_build / "Dockerfile")
    shutil.copy(_images_root() / "db" / "init.sh", db_build / "init.sh")
    shutil.copy(local_dump, db_build / "dump.dump")
    db_image = f"sandbox-db:{etag}"
    docker.build(context=db_build, tag=db_image)

    # 3. Assemble agent build context
    agent_build = sdir / "build" / "agent"
    agent_build.mkdir(parents=True)
    shutil.copy(_images_root() / "agent" / "Dockerfile", agent_build / "Dockerfile")
    shutil.copy(_images_root() / "agent" / "entrypoint.sh", agent_build / "entrypoint.sh")

    repo.bundle_host_repo(Path(args.repo), agent_build / "repo.bundle")
    shutil.copy(creds_src, agent_build / ".credentials.json")
    (agent_build / ".credentials.json").chmod(0o600)

    # 4. Bare repo for the user to fetch from
    repo.init_bare_repo(sdir / "bare.git")

    # 5. Render and write compose.yml
    cfg = compose.ComposeConfig(
        session_id=meta.id,
        goal=args.goal,
        db_image=db_image,
        agent_image_name=args.agent_image,
        build_dir_db="./build/db",
        build_dir_agent="./build/agent",
        db_name=args.db_name,
    )
    (sdir / "compose.yml").write_text(compose.render(cfg))

    # 6. Up (compose template lowercases the project name)
    docker.compose_up(project=project, compose_file=sdir / "compose.yml", build=True, detach=True)

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
    return any(r.get("State") == "running" for r in rows)


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

    # Fetch into bare and write a single-file patch for convenience
    repo.fetch_bundle_into_bare(bundle=bundle_dst, bare=sdir / "bare.git", branch=meta.branch)
    (sdir / "patch.diff").write_text(repo.format_patch(bare=sdir / "bare.git", branch=meta.branch))

    # Tear down compose project; this also removes per-session images so creds + db disappear
    docker.compose_down(project=project, compose_file=compose_file, volumes=True, rmi_local=True)

    meta.status = "finished"
    meta.finished_at = _time.time()
    session.save(meta)

    print(f"branch ready: git fetch {sdir / 'bare.git'} {meta.branch}")
    print(f"patch:        {sdir / 'patch.diff'}")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    dispatch = {
        "start": cmd_start,
        "status": cmd_status,
        "logs": cmd_logs,
        "finish": cmd_finish,
    }
    handler = dispatch.get(args.verb)
    if handler is None:
        print(f"verb {args.verb!r} not implemented yet", file=sys.stderr)
        return 1
    return handler(args)
