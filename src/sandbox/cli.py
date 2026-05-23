from __future__ import annotations

import argparse
import json as _json
import os
import shutil
import sys
import time as _time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sandbox import compose, docker, dump, repo, session

EGRESS_ALLOWLIST_GROUPS: dict[str, tuple[str, ...]] = {
    "anthropic": (
        "api.anthropic.com",
        "console.anthropic.com",
    ),
    "github": (
        # .github.com covers github.com and all subdomains (api.github.com etc.)
        # Squid 6 rejects redundant subdomain entries in the same ACL, so we
        # use only the wildcard forms here.
        ".github.com",
        ".githubusercontent.com",
    ),
    "python": (
        "pypi.org",
        "files.pythonhosted.org",
        "pythonhosted.org",
    ),
    "node": (
        # .npmjs.org covers registry.npmjs.org and all subdomains.
        ".npmjs.org",
    ),
    "aws": (
        # .amazonaws.com covers s3.amazonaws.com, sts.amazonaws.com, etc.
        ".amazonaws.com",
    ),
}
EGRESS_ALLOWLIST_GROUPS["default"] = tuple(
    fqdn
    for group in ("anthropic", "github", "python", "node", "aws")
    for fqdn in EGRESS_ALLOWLIST_GROUPS[group]
)


class HostAuthMissing(RuntimeError):
    """Raised when no usable Claude credentials are available in host env."""


class _AwsFlagError(RuntimeError):
    """Raised when AWS CLI flags are used incorrectly; cmd_start converts this to rc=2."""


def _parse_duration(s: str) -> timedelta:
    """Parse forms like '30s', '5m', '1h'. Raise ValueError on garbage."""
    s = s.strip().lower()
    if not s:
        raise ValueError(f"unparseable duration {s!r}; expected '30s', '5m', '1h'")
    try:
        if s.endswith("s"):
            return timedelta(seconds=int(s[:-1]))
        if s.endswith("m"):
            return timedelta(minutes=int(s[:-1]))
        if s.endswith("h"):
            return timedelta(hours=int(s[:-1]))
    except ValueError:
        pass
    raise ValueError(f"unparseable duration {s!r}; expected forms like '30s', '5m', '1h'")


def _resolve_host_auth() -> tuple[str, str]:
    """Return (env_var_name, token_value) for the agent container.

    Prefers CLAUDE_CODE_OAUTH_TOKEN (Claude subscription via `claude setup-token`).
    Falls back to ANTHROPIC_API_KEY (pay-per-token API billing).
    Raises HostAuthMissing if neither is set.
    """
    oauth = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", "").strip()
    if oauth:
        return ("CLAUDE_CODE_OAUTH_TOKEN", oauth)
    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if api_key:
        return ("ANTHROPIC_API_KEY", api_key)
    raise HostAuthMissing(
        "No Claude credentials found in env. Run `claude setup-token` on the host "
        "to mint a subscription token, then `export CLAUDE_CODE_OAUTH_TOKEN=...`. "
        "Or set ANTHROPIC_API_KEY for pay-per-token API billing."
    )


def _agent_image_tag(base: str, sid: str) -> str:
    return f"{base}:{sid}"


def _db_image_tag(sid: str) -> str:
    # v0.2.0: db is upstream postgres:16, no per-session image
    return "postgres:16"


def _cleanup_sensitive_build_inputs(sdir: Path) -> None:
    for rel in (
        "build/agent/repo.bundle",
        "input/repo.bundle",
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


def _terminate_follower(meta: session.Meta) -> None:
    if meta.follower_pid:
        try:
            docker.terminate_pid(meta.follower_pid)
        except Exception:
            pass


def _render_proxy_config(*, allowlist_fqdns: list[str], sdir: Path) -> Path:
    """Render squid.conf into sdir/proxy/allowlist.conf and return the path."""
    proxy_dir = sdir / "proxy"
    proxy_dir.mkdir(parents=True, exist_ok=True)
    config_path = proxy_dir / "allowlist.conf"
    config_path.write_text(compose.render_squid_config(allowlist_fqdns=allowlist_fqdns))
    return config_path


def _wait_for_proxy_ready(*, container: str, timeout_s: float = 60.0) -> None:
    """Poll docker inspect for the proxy container's healthcheck status."""
    deadline = _time.time() + timeout_s
    while _time.time() < deadline:
        try:
            result = docker._run(
                ["docker", "inspect", "--format", "{{.State.Health.Status}}", container]
            )
            if result.stdout.strip() == "healthy":
                return
        except Exception:
            pass
        _time.sleep(1.0)
    raise RuntimeError(f"proxy container {container} never became healthy")


def _wait_for_db_ready(*, container: str, db_name: str, timeout_s: float = 60.0) -> None:
    deadline = _time.time() + timeout_s
    last_err = None
    while _time.time() < deadline:
        try:
            probe = docker.exec_in_container(
                container=container,
                cmd=["pg_isready", "-U", "postgres", "-d", db_name],
            )
            if probe.returncode == 0:
                return
        except Exception as e:
            last_err = e
        _time.sleep(1.0)
    raise RuntimeError(f"db container {container} never became ready: {last_err}")


def _import_dump_at_runtime(*, container: str, local_dump: Path, db_name: str) -> None:
    """Copy the dump into the running db container, restore it, then delete it."""
    docker.cp(src=str(local_dump), dst=f"{container}:/tmp/dump.dump")
    restore = docker.exec_in_container(
        container=container,
        cmd=[
            "pg_restore",
            "--username", "postgres",
            "--dbname", db_name,
            "--no-owner", "--no-acl",
            "--clean", "--if-exists",
            "--exit-on-error",
            "/tmp/dump.dump",
        ],
    )
    if restore.returncode != 0:
        raise RuntimeError(f"pg_restore failed: rc={restore.returncode}")
    # Best-effort wipe; tolerate `shred` not being present in the image.
    try:
        docker.exec_in_container(container=container, cmd=["shred", "-u", "/tmp/dump.dump"])
    except Exception:
        docker.exec_in_container(container=container, cmd=["rm", "-f", "/tmp/dump.dump"])


def _fmt_ts(epoch):
    if epoch is None:
        return "-"
    return datetime.fromtimestamp(epoch, tz=timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S")


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


def _resolve_allowlist(*, groups: str, extra: str) -> list[str]:
    """Resolve comma-separated group names + extra FQDNs into a deduped, ordered list.

    `groups` is a comma-separated subset of EGRESS_ALLOWLIST_GROUPS keys.
    `extra` is comma-separated FQDNs to append.
    Whitespace around entries is stripped; empty entries are ignored.
    Order is preserved, duplicates dropped.
    Raises ValueError for an unknown group name.
    """
    seen: list[str] = []
    seen_set: set[str] = set()
    for group in (g.strip() for g in groups.split(",") if g.strip()):
        if group not in EGRESS_ALLOWLIST_GROUPS:
            raise ValueError(
                f"unknown egress group {group!r}. "
                f"Known groups: {sorted(EGRESS_ALLOWLIST_GROUPS)}"
            )
        for fqdn in EGRESS_ALLOWLIST_GROUPS[group]:
            if fqdn not in seen_set:
                seen.append(fqdn)
                seen_set.add(fqdn)
    for fqdn in (f.strip() for f in extra.split(",") if f.strip()):
        if fqdn not in seen_set:
            seen.append(fqdn)
            seen_set.add(fqdn)
    return seen


def _mint_scoped_s3_token(*, profile, buckets, duration_seconds=3600):
    from sandbox import aws as aws_mod
    return aws_mod.mint_scoped_s3_token(profile=profile, buckets=buckets, duration_seconds=duration_seconds)


def _resolve_aws_credentials(args) -> dict | None:
    """Resolve AWS credentials for the agent based on CLI flags.

    Returns None if no AWS flags are set.
    Returns an STS-scoped credentials dict by default.
    Returns raw profile credentials if --aws-unsafe-passthrough is set (with stderr warning).
    Exits non-zero (sys.exit(2)) if --aws-profile is set without --s3-buckets or --aws-unsafe-passthrough.
    """
    if not args.aws_profile and not args.aws_unsafe_passthrough:
        return None

    if args.aws_unsafe_passthrough:
        import boto3
        session = boto3.Session(profile_name=args.aws_profile) if args.aws_profile else boto3.Session()
        frozen = session.get_credentials().get_frozen_credentials()
        print(
            "WARNING: --aws-unsafe-passthrough is set. The agent will receive your raw "
            f"AWS credentials with whatever permissions profile {args.aws_profile or '<default>'} has. "
            "If you only need read access, use --s3-buckets to scope via STS instead.",
            file=sys.stderr,
        )
        return {
            "access_key_id": frozen.access_key,
            "secret_access_key": frozen.secret_key,
            "session_token": frozen.token,
            "region": session.region_name or "us-east-1",
        }

    # STS path: --aws-profile without --aws-unsafe-passthrough → require --s3-buckets
    buckets = [b.strip() for b in args.s3_buckets.split(",") if b.strip()]
    if not buckets:
        print(
            "error: --aws-profile requires --s3-buckets b1,b2,… to scope the STS session, "
            "or --aws-unsafe-passthrough to skip scoping (not recommended)",
            file=sys.stderr,
        )
        raise _AwsFlagError()
    return _mint_scoped_s3_token(profile=args.aws_profile, buckets=buckets)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="sandbox")
    sub = p.add_subparsers(dest="verb", required=True)

    sp = sub.add_parser("start", help="start a new sandboxed session")
    sp.add_argument("--repo", required=True, help="host path to the repo to copy into the sandbox")
    sp.add_argument("--goal", required=True, help="goal prompt passed to Claude Code")
    sp.add_argument("--dump-bucket", default=None, help="S3 bucket containing a frozen pg_dump (mutually exclusive with --postgres-source)")
    sp.add_argument("--dump-key", default=None, help="S3 key of the pg_dump within --dump-bucket")
    sp.add_argument(
        "--postgres-source",
        default=None,
        help="Live Postgres connection URL; CLI runs pg_dump in a one-shot container against it. "
             "Mutually exclusive with --dump-bucket/--dump-key. Password may be embedded in the URL or "
             "set via PGPASSWORD env. Cached for --max-dump-age.",
    )
    sp.add_argument(
        "--max-dump-age",
        default="1h",
        help="When using --postgres-source, reuse a cached dump newer than this. "
             "Forms: 30s, 5m, 2h. Default: 1h.",
    )
    sp.add_argument("--db-name", default="appdb")
    sp.add_argument("--agent-image", default="sandbox-agent", help="base name of per-session agent image")
    sp.add_argument(
        "--egress-allowlist",
        default="default",
        help="comma-separated egress allowlist groups (default: 'default'). "
             "Known groups: anthropic, github, python, node, default.",
    )
    sp.add_argument(
        "--extra-egress-allowlist",
        default="",
        help="comma-separated extra FQDNs to append to the allowlist",
    )
    sp.add_argument(
        "--aws-profile",
        default=None,
        help="AWS profile name to draw credentials from. By default the agent is given STS-scoped read-only credentials (requires --s3-buckets).",
    )
    sp.add_argument(
        "--s3-buckets",
        default="",
        help="Comma-separated bucket names the agent should be allowed to read. Required when --aws-profile is set, unless --aws-unsafe-passthrough is also given.",
    )
    sp.add_argument(
        "--aws-unsafe-passthrough",
        action="store_true",
        help="Pass raw profile credentials to the agent without STS scoping. NOT RECOMMENDED — the agent receives your full profile permissions. Use only if your environment cannot call sts:GetFederationToken.",
    )

    for verb in ("status", "logs", "finish", "stop"):
        s = sub.add_parser(verb)
        s.add_argument("session", help="session id or unique prefix")
        if verb == "logs":
            s.add_argument("-f", "--follow", action="store_true")

    sub.add_parser("prune", help="remove finished sessions older than 30 days")
    sub.add_parser("list", help="list all sessions")

    return p


def _images_root() -> Path:
    # repo layout: <repo>/src/sandbox/cli.py and <repo>/images/
    return Path(__file__).resolve().parents[2] / "images"


def cmd_start(args: argparse.Namespace) -> int:
    # Validate dump source flags BEFORE any state is created
    if args.postgres_source and (args.dump_bucket or args.dump_key):
        print(
            "error: --postgres-source is mutually exclusive with --dump-bucket/--dump-key",
            file=sys.stderr,
        )
        return 2
    if not args.postgres_source and not (args.dump_bucket and args.dump_key):
        print(
            "error: must specify either --postgres-source URL or both --dump-bucket B --dump-key K",
            file=sys.stderr,
        )
        return 2

    try:
        auth_env_name, auth_env_value = _resolve_host_auth()
    except HostAuthMissing as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    try:
        aws_credentials = _resolve_aws_credentials(args)
    except _AwsFlagError:
        return 2

    meta = session.new_session(goal=args.goal, repo=args.repo)
    try:
        return _start_session(meta, args, auth_env_name, auth_env_value, aws_credentials)
    except Exception:
        # Mark the partial session failed so prune can reclaim it later.
        import time as _time
        _cleanup_sensitive_build_inputs(session.session_dir(meta.id))
        _cleanup_compose_project(meta)
        _cleanup_session_images(meta)
        _terminate_follower(meta)
        try:
            meta.status = "failed"
            meta.finished_at = _time.time()
            session.save(meta)
        except Exception:
            pass
        raise


def _start_session(
    meta: session.Meta,
    args: argparse.Namespace,
    auth_env_name: str,
    auth_env_value: str,
    aws_credentials: dict | None = None,
) -> int:
    sdir = session.session_dir(meta.id)
    project = meta.id.lower()
    meta.agent_image = _agent_image_tag(args.agent_image, meta.id)
    meta.db_image = None  # v0.2.0: db uses shared upstream postgres:16, nothing per-session to remove
    session.save(meta)

    try:
        # 1. Fetch dump (cached by ETag)
        if args.postgres_source:
            max_age = _parse_duration(args.max_dump_age)
            local_dump, _etag = dump.fetch_from_postgres_url(args.postgres_source, max_age=max_age)
        else:
            local_dump, _etag = dump.fetch(args.dump_bucket, args.dump_key)

        # 2. Assemble agent build context (db is now upstream postgres:16; no build)
        agent_build = sdir / "build" / "agent"
        agent_build.mkdir(parents=True)
        shutil.copy(_images_root() / "agent" / "Dockerfile", agent_build / "Dockerfile")
        shutil.copy(_images_root() / "agent" / "entrypoint.sh", agent_build / "entrypoint.sh")

        input_dir = sdir / "input"
        input_dir.mkdir(parents=True)
        repo.bundle_host_repo(Path(args.repo), input_dir / "repo.bundle")

        # 3. Resolve allowlist and render proxy config BEFORE compose up
        allowlist_fqdns = _resolve_allowlist(
            groups=args.egress_allowlist,
            extra=args.extra_egress_allowlist,
        )
        proxy_config_path = _render_proxy_config(allowlist_fqdns=allowlist_fqdns, sdir=sdir)

        # 4. Build the proxy image (idempotent across sessions — shared tag, layers cached)
        docker.build(context=_images_root() / "proxy", tag="sandbox-proxy:latest")

        # 5. Bare repo for the user to fetch from
        repo.init_bare_repo(sdir / "bare.git")

        # 6. Render and write compose.yml (db_image is upstream postgres:16)
        cfg = compose.ComposeConfig(
            session_id=meta.id,
            goal=args.goal,
            db_image="postgres:16",
            agent_image_name=args.agent_image,
            build_dir_agent="./build/agent",
            db_name=args.db_name,
            proxy_image="sandbox-proxy:latest",
            auth_env_name=auth_env_name,
            auth_env_value=auth_env_value,
            aws_credentials=aws_credentials,
        )
        (sdir / "compose.yml").write_text(compose.render(cfg))

        # 7. Up (compose template lowercases the project name)
        # The proxy container starts but its entrypoint blocks waiting for the config file.
        docker.compose_up(project=project, compose_file=sdir / "compose.yml", build=True, detach=True)

        # 8. Install proxy config into the proxy container; proxy then starts squid
        proxy_container = docker.container_name(project=project, service="proxy")
        docker.cp(src=str(proxy_config_path), dst=f"{proxy_container}:/etc/squid/conf.d/allowlist.conf")
        _wait_for_proxy_ready(container=proxy_container)

        # 9. Import dump into running db container BEFORE handing inputs to agent
        db_container = docker.container_name(project=project, service="db")
        _wait_for_db_ready(container=db_container, db_name=args.db_name)
        _import_dump_at_runtime(container=db_container, local_dump=local_dump, db_name=args.db_name)

        # 10. Hand repo bundle to running agent container.
        # `docker cp` lands files owned by root:root inside the container, but the
        # agent process runs as the unprivileged `node` user (required because
        # claude --dangerously-skip-permissions refuses to run as root). Chown
        # the files after the copy so the entrypoint can read them.
        container = docker.container_name(project=project, service="agent")
        docker.cp(src=str(input_dir / "repo.bundle"), dst=f"{container}:/input/repo.bundle")
        # `docker cp` lands files as root:root inside the container. The real
        # agent image runs as the unprivileged `node` user (claude
        # --dangerously-skip-permissions refuses to run as root), so we need
        # to chown the inputs to node. The stub image runs as root and has no
        # `node` user — best-effort the chown so the stub path still works.
        try:
            docker.exec_in_container(
                container=container,
                cmd=["chown", "node:node", "/input/repo.bundle"],
                user="root",
            )
        except Exception:
            pass
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
        rows = _json.loads(ps.stdout or "[]")
        if isinstance(rows, dict):
            rows = [rows]
        compose_state = ", ".join(f"{r.get('Service', '?')}={r.get('State', '?')}" for r in rows) or "(no services)"
    except docker.DockerNotRunning:
        raise
    except Exception as e:
        compose_state = f"<compose ps failed: {e}>"
    print(f"id:       {meta.id}")
    print(f"status:   {meta.status}")
    print(f"goal:     {meta.goal}")
    print(f"repo:     {meta.repo}")
    print(f"branch:   {meta.branch}")
    print(f"started:  {_fmt_ts(meta.started_at)}")
    print(f"finished: {_fmt_ts(meta.finished_at)}")
    print(f"compose:  {compose_state}")
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    rows = session.all_sessions()
    if not rows:
        print("no sessions")
        return 0
    print(f"{'ID':<26}  {'STATUS':<10}  {'BRANCH':<30}  {'STARTED':<19}  GOAL")
    for m in rows:
        goal_short = (m.goal[:38] + "…") if len(m.goal) > 38 else m.goal
        print(f"{m.id:<26}  {m.status:<10}  {m.branch:<30}  {_fmt_ts(m.started_at):<19}  {goal_short}")
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
    _terminate_follower(meta)
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
    _terminate_follower(meta)
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
        "list": cmd_list,
    }
    handler = dispatch.get(args.verb)
    if handler is None:
        print(f"verb {args.verb!r} not implemented yet", file=sys.stderr)
        return 1
    try:
        return handler(args)
    except docker.DockerNotRunning as e:
        print(
            "error: Docker daemon not reachable. Is Docker Desktop running?\n"
            f"  detail: {e}",
            file=sys.stderr,
        )
        return 3
    except LookupError as e:
        print(f"error: {e}", file=sys.stderr)
        return 4
    except Exception as e:
        try:
            import botocore.exceptions as _be
            if isinstance(e, _be.NoCredentialsError):
                print(
                    "error: AWS credentials not found. Set AWS_ACCESS_KEY_ID and "
                    "AWS_SECRET_ACCESS_KEY in your environment, or configure "
                    "~/.aws/credentials.",
                    file=sys.stderr,
                )
                return 5
        except ImportError:
            pass
        raise
