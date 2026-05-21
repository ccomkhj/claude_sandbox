# Claude Code Sandbox — Design

**Status:** Approved
**Date:** 2026-05-21
**Owner:** Huijo Kim

## Problem

We want a simple way to run Claude Code on long-running, autonomous tasks (e.g. the goal skill) against a snapshot of production Postgres data, without giving the agent access to the host machine. The current options — running `claude` directly on the host, or pointing it at production — are unacceptable: the host has personal files and credentials, and production is not a safe playground for an autonomous long-task agent.

## Goals

1. Run Claude Code inside a Docker sandbox that cannot read the host filesystem, mount host sockets, or reach host services.
2. Provide the agent a fresh Postgres restored from a production dump pulled from S3, scoped to the session.
3. Copy the user's local repo into the sandbox at start, and bring the agent's changes back as a reviewable git branch.
4. Support detached, long-running sessions with multiple concurrent jobs, each with a stable session id and CLI verbs to inspect and finalize.
5. Be a small, self-contained repo that anyone with Docker + Python can clone and use.

## Non-goals

- Streaming interactive Claude Code sessions (no TTY pass-through). This is for autonomous goals, not pair-programming.
- Air-gapped operation. The container needs egress to `api.anthropic.com` and to the package registries the agent will pull from during the task.
- Multi-machine orchestration. One host, one user, multiple concurrent local sandboxes.
- Production write-back. The agent's output is a git branch the user reviews — it never pushes anywhere remote on its own.

## Architecture

A Python CLI (`sandbox`) drives Docker Compose to spawn one isolated agent session per invocation. Each session is a compose project with two services on a private docker network, plus a host-side state directory the CLI owns.

```
host machine                                       │   sandbox session <id> (compose project)
                                                   │
~/.sandbox/                                        │   ┌─ network: agent_net (bridge, internet egress) ─┐
  bin/sandbox  (Python entry point)                │   │                                                 │
  sessions/<id>/                                   │   │   ┌────────────────────┐                        │
    meta.json                                      │   │   │ agent              │ ──► api.anthropic.com  │
    repo.bundle    ──── git clone ───────────────► │   │   │  • claude code     │ ──► github, pypi, …    │
    bare.git/      ◄─── docker cp bundle, ──────── │   │   │  • copied repo     │                        │
                       then git fetch              │   │   │  • copied ~/.claude│                        │
    logs/                                          │   │   └────────┬───────────┘                        │
    patch.diff     ◄─── git format-patch ───────── │   └────────────┼────────────────────────────────────┘
    compose.yml                                    │                │ db_net (internal: true, no egress)
                                                   │   ┌────────────┴───────────┐
                                                   │   │ db                     │
  S3 dump bucket  ───── pulled at start ─────────► │   │  • postgres            │
                       (build context, host-side)  │   │  • restored from dump  │
                                                   │   └────────────────────────┘
```

### Invariants enforced by the architecture

1. **No host FS access from containers.** The CLI is the only host process that touches user files. Inputs (repo bundle, creds, dump) enter the agent container via the build context. Outputs come back via `docker cp` of a git bundle the agent writes. The rendered `compose.yml` has zero `volumes:` entries with host paths.
2. **DB has no public network.** The `db` service is on `db_net` only, declared `internal: true`. The `agent` service is on both `agent_net` (default bridge, internet egress) and `db_net` (private, internal). The agent can reach Postgres at `db:5432` and reach `api.anthropic.com`; Postgres can reach neither the agent's egress path nor the internet.
3. **Sessions are independent.** The session ULID is the compose project name, so two concurrent sessions share no networks, no volumes, no state.
4. **Detached by default.** `sandbox start` returns a session id immediately; `docker compose up -d` runs the workload; a background follower process tees logs to the host state dir.

### Approaches considered

- **A. Compose-per-session (chosen).** Two services per session, db isolated by `internal: true` network. Clean separation, independently upgradable images, isolation enforceable in `compose.yml`.
- **B. Single fat container.** Postgres + Claude Code in one image. Simpler `docker run`, but Postgres shares a process namespace with the agent and the "DB has no egress" property cannot be expressed in network config alone.
- **C. Long-lived shared DB.** Faster start, but sessions mutate each other's DB state — violates the per-session-fresh requirement.

## Components

| Unit | Responsibility | Depends on |
|------|----------------|------------|
| `sandbox.cli` | Argparse entry point; dispatches to verbs (`start`, `status`, `logs`, `finish`, `stop`, `prune`) | session, repo, dump, docker |
| `sandbox.session` | Owns `~/.sandbox/sessions/<id>/`; reads/writes `meta.json`; allocates session ids (ULID); resolves id-prefix lookups | stdlib |
| `sandbox.compose` | Pure function: `(SessionConfig) → compose.yml string`. No I/O. Snapshot-tested. | jinja2 |
| `sandbox.docker` | Thin subprocess wrapper around `docker compose -p <id> ...` and `docker cp`. One function per docker verb used. | subprocess |
| `sandbox.dump` | Pulls latest dump from S3 to host-side cache, keyed by ETag; returns local path | boto3 |
| `sandbox.repo` | Host-side git: bundle from user repo; init bare repo; fetch from agent's output bundle on finish; `git format-patch` | git CLI |
| `images/agent/` | Dockerfile (Node + claude-code + git + pg client) and `entrypoint.sh` | — |
| `images/db/` | Dockerfile (postgres + restore step) and `entrypoint.sh` | — |

Everything that touches the outside world (docker, S3, git, FS) is in its own module so the rest can be unit-tested without mocks beyond those boundaries.

## Data flow

### `sandbox start --repo ./foo --goal "..." [--dump latest|<s3 key>]`

1. CLI generates session id (ULID) and creates `~/.sandbox/sessions/<id>/`.
2. `dump.fetch()` — downloads from S3 to `~/.sandbox/cache/dumps/` if ETag changed, otherwise reuses cache. Symlinks the dump into the session's build context.
3. `repo.bundle_host_repo(./foo)` writes `repo.bundle` into the build context.
4. CLI copies `~/.claude/.credentials.json` (and `settings.json` if present) into the build context with mode 0600.
5. `git init --bare ~/.sandbox/sessions/<id>/bare.git`.
6. CLI renders `compose.yml` from template, writes to session dir, writes `meta.json` with `status: starting`.
7. CLI runs `docker compose -p <id> up -d --build`. Build copies in dump, bundle, creds. Running containers never bind-mount host paths.
8. CLI spawns detached follower: `docker compose logs -f` → `logs/agent.log` + `logs/db.log`. Follower PID stored in `meta.json`.
9. Returns session id to user. `sandbox start` exits.

### Inside the agent container, entrypoint

1. Wait for `db` healthcheck (`pg_isready`).
2. `git clone /input/repo.bundle /work/repo && cd /work/repo && git checkout -b sandbox/<id>`.
3. `mkdir -p /root/.claude && cp /input/.credentials.json /root/.claude/ && chmod 600 ...` then `shred -u /input/.credentials.json`.
4. `cd /work/repo && exec claude --dangerously-skip-permissions --goal "$GOAL"`.
5. Exit trap (always, any code): `cd /work/repo && git add -A && git commit -m "agent wip" || true && git bundle create /output/branch.bundle sandbox/<id>; echo $? > /output/exit_code`.

### `sandbox status <id>`

Reads `meta.json`; runs `docker compose -p <id> ps` to refresh container state; prints status, runtime, branch, last log line.

### `sandbox logs <id> [-f]`

Tails `logs/agent.log`; `-f` follows.

### `sandbox finish <id>`

1. Verify container exited. Refuse if still running (user should `sandbox stop` first).
2. `docker cp <agent>:/output/branch.bundle ~/.sandbox/sessions/<id>/branch.bundle`.
3. `git -C bare.git fetch ../branch.bundle 'sandbox/<id>:sandbox/<id>'`.
4. `git -C <user-host-repo> remote add sandbox-<id> ~/.sandbox/sessions/<id>/bare.git` (idempotent) and print the one-liner `git fetch sandbox-<id> sandbox/<id>` for the user to run.
5. Write `patch.diff` via `git format-patch` for users who prefer a single file.
6. `docker compose -p <id> down -v` to wipe containers + volumes (creds gone, db gone).
7. Mark `meta.json` status `finished`.

### `sandbox stop <id>`

`docker compose -p <id> kill -s SIGTERM`; wait 30s; `down -v` if still up.

### `sandbox prune`

Removes finished sessions older than 30 days; removes orphaned compose projects whose host state dir is gone.

## Error handling

| Failure | Behavior |
|---------|----------|
| S3 dump fetch fails | Abort before any `docker` call. Session dir removed. Error includes S3 error and credential hint. |
| `docker compose up` build fails | Session marked `failed`; logs preserved; compose project torn down. Exit non-zero. |
| `db` healthcheck never passes | Agent waits up to 5 min then exits. Session marked `failed`. `db` logs preserved. |
| Agent crashes mid-run | Entrypoint exit trap still bundles current branch state. Session marked `crashed`. `sandbox finish` works the same — user gets a partial patch. |
| User Ctrl-C during `start` | SIGINT handler runs `docker compose -p <id> down -v` and removes session dir. |
| Host crashes / CLI process killed | Next CLI invocation detects orphans via `docker compose ls` vs state-dir list. `sandbox prune` cleans them. |
| `~/.claude` creds missing | `sandbox start` refuses; prints how to log in to Claude Code first. |
| Concurrent `sandbox start` | ULID session ids are unique by construction; no global lock needed. |

### Credential hygiene invariants (asserted in tests)

- Creds enter the container only via build context.
- They are moved to `/root/.claude/` and the build-context copy is `shred`-deleted before the agent starts.
- `docker compose -p <id> down -v` removes all session volumes and images that contained the creds.

## Testing

### Unit (no docker, no network)

- `sandbox.compose` — snapshot test the rendered YAML for a fixed input. Assert no host bind-mounts; assert `db_net.internal: true`; assert `db` is not on `agent_net`.
- `sandbox.session` — meta.json state machine transitions (`starting → running → finished | failed | crashed`).
- `sandbox.repo` — real `git` in temp dirs; verify bundle-out / fetch-in round trip.
- `sandbox.dump` — `moto` to fake S3; verify ETag-skip cache.

### Integration (real docker, fake agent)

Build an `images/agent-stub/` variant whose entrypoint writes a marker file, commits, and exits.

- Run full `start → wait → finish` against a tiny fixture dump. Verify the host-side bare repo has the expected commit.
- **Isolation tests:** from inside agent, `curl --max-time 2 http://host.docker.internal/` must fail; `psql -h db -c 'SELECT 1'` must succeed. From `db`, `curl https://example.com` must fail.

### Smoke (manual, nightly in CI when available)

Real `claude` binary, a 50-line fixture repo, 20MB dump, trivial goal ("add a docstring to function X"). Verify a runnable patch comes out.

### Acceptance criteria for v1

- All unit and integration tests green.
- One smoke run produces a non-empty patch.
- Isolation tests pass.

## Open implementation details deferred to the plan

- Exact base image for the agent (likely `node:20-slim` + the official claude-code install path).
- Whether to use `pg_restore` or `psql < dump.sql` (depends on dump format the user chooses to keep in S3).
- The exact Claude Code invocation for "goal" mode — confirm whether it's a skill or a CLI flag in the current Claude Code version.
- Whether `boto3` is the right S3 client or whether to shell out to `aws s3 cp` to avoid a heavy dep.

These get resolved during the plan / first iteration; none of them change the architecture.
