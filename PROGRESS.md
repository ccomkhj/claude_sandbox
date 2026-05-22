# Progress — claude-code-sandbox v0.1.0

**Status:** Initial implementation complete. Tag `v0.1.0` on `main`.

`claude-code-sandbox` runs Claude Code in an isolated Docker sandbox against a production Postgres dump. The agent gets a fresh DB from S3, a copy of your repo on its own branch, and your existing Claude Code credentials — all in a container that cannot read your host filesystem and cannot reach host services. Long-running goal-mode tasks run detached; you retrieve the agent's branch when it's done.

## What got built

A Python CLI (`sandbox`) driving Docker Compose to spawn one isolated agent session per invocation. Each session is a compose project with two services on a private docker network, plus a host-side state directory the CLI owns.

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

CLI verbs implemented: `start`, `status`, `logs [-f]`, `finish`, `stop`, `prune`.

## Validated security properties

The design's promised invariants are checked by live integration tests against a real Docker daemon.

| Invariant | Verified by |
|-----------|-------------|
| Agent runs Claude Code in a Docker sandbox | E2E test (Task 14) — `tests/integration/test_end_to_end.py` |
| Sandbox cannot reach host services | Isolation test — `host.docker.internal` curl fails (Task 15) |
| Sandbox cannot bind-mount host paths | Compose template snapshot test (Task 3) — `tests/test_compose.py::test_no_host_bind_mounts` |
| DB has no internet egress | Isolation test — `getent hosts example.com` fails inside `db` container (Task 15) |
| Agent CAN reach DB on private network | Isolation test — `psql -h db -c "SELECT 1"` succeeds (Task 15) |
| Repo changes return as a reviewable branch | E2E test bundle round-trip (Task 14) |
| `patch.diff` scopes correctly to `main..agent-branch` | Tightened assertion catches the `--root` fallback (Task 12 fix) |
| Per-session ephemerality, creds destroyed | `docker compose down --rmi local` removes the per-session image that contained the copied creds |

## Test status

```
45 unit tests + 2 integration tests passing.
pytest                          # unit tests, < 1s
pytest --run-integration        # also runs real-Docker tests, ~30s on cached layers
```

## Modules

| Module | Responsibility |
|--------|----------------|
| `sandbox.session` | `~/.sandbox/sessions/<id>/` state dir, `meta.json`, ULID ids, prefix lookup |
| `sandbox.compose` | Pure jinja2 renderer for `compose.yml` with isolation invariants baked in |
| `sandbox.repo` | Host-side git: `bundle_host_repo`, `init_bare_repo`, `fetch_bundle_into_bare`, `format_patch` |
| `sandbox.dump` | S3 fetch with bucket-aware hashed cache key; ETag invalidation |
| `sandbox.docker` | Thin subprocess wrappers around `docker compose ...`, `docker cp`, `docker build` |
| `sandbox.cli` | argparse entry point; orchestrates the verbs |
| `images/db/` | `postgres:16` image with dump baked into `/docker-entrypoint-initdb.d/` |
| `images/agent/` | `node:20-slim` + `@anthropic-ai/claude-code`; entrypoint shreds creds, bundles output |
| `images/agent-stub/` | Deterministic stub for integration tests |

## Bugs caught by review during implementation

The plan as written had real gaps. Subagent-driven review (spec compliance + code quality on every task) caught them at build time rather than at first real-world run.

1. **ULID timestamp prefix collision** (Task 2 test). The plan used `a.id[:6]` for prefix lookup tests, but ULIDs encode a millisecond timestamp in the first 10 chars and `python-ulid` produces *monotonic* ULIDs that share 25 chars when generated back-to-back. Switched to a `monkeypatch.setattr(session, "_new_id", ...)` pattern matching the sibling ambiguity test.
2. **Uppercase project name rejected by `docker compose -p`** (Task 3). ULIDs are uppercase by convention, but `docker compose -p` rejects uppercase. Added `| lower` filter in the compose template, normalized at every CLI call site via a local `project = meta.id.lower()`.
3. **`format_patch` happy path was untested** (Task 4). The original `test_format_patch_returns_non_empty` only exercised the `--root` fallback because the bundle in that test only carried `feature`. A new test bundles `--all` so `main` exists in `bare.git`, then asserts only the new file appears in the patch.
4. **S3 cache key ignored bucket** (Task 5). `_safe()` mapped both `/` and `_` to `_`, and the cache filename ignored the bucket entirely — distinct keys could share cache entries. Replaced with a bucket-aware sha256 prefix.
5. **`compose_up` swallowed multi-minute build output** (Task 6). `_run`'s `capture=True` default meant `docker build` produced no output until it finished. Override to `capture=False` for `compose_up` and `compose_down`.
6. **File descriptor leak in `compose_logs_follow`** (Task 6). `subprocess.Popen` dup's the fd but the parent retains an open file handle. Added a `try/finally` to close it.
7. **Orphan "starting" sessions on partial-init failure** (Task 10). If `dump.fetch` or `docker.build` raised after `session.new_session()`, the session dir was left with `status="starting"` forever — `prune` only collects sessions in terminal states. Wrapped the post-`new_session` logic in a try/except that marks `failed` with `finished_at` before re-raising.
8. **Agent's bundle had `main` but `cmd_finish` never fetched it** (Task 12, surfaced by tightened assertion). `format_patch` silently fell back to `--root` and produced a patch that included all base history. Added an explicit `fetch main` from the bundle before fetching the agent branch.
9. **`_is_running` blocked `cmd_finish`** (Task 14, surfaced by E2E run). The db service runs indefinitely (long-lived Postgres), so the original check `any(State == "running")` was always true after the agent exited. Scoped to `Service == "agent"`.
10. **Docker CLI plugin discovery broken when `HOME` is monkeypatched** (Task 14). The `fake_creds` test fixture overrode `HOME`, which broke Docker's `~/.docker/cli-plugins/` lookup, so `docker compose` couldn't even start. Fixed by setting `DOCKER_CONFIG` to the real `~/.docker` before the HOME redirect.

## Layout

```
claude-code-sandbox/
├── pyproject.toml
├── README.md
├── PROGRESS.md                                  (this file)
├── docs/superpowers/
│   ├── specs/2026-05-21-claude-code-sandbox-design.md
│   └── plans/2026-05-21-claude-code-sandbox.md
├── src/sandbox/
│   ├── cli.py
│   ├── session.py
│   ├── compose.py
│   ├── docker.py
│   ├── dump.py
│   ├── repo.py
│   └── templates/compose.yml.j2
├── images/
│   ├── agent/{Dockerfile, entrypoint.sh}
│   ├── agent-stub/{Dockerfile, entrypoint.sh}
│   └── db/{Dockerfile, init.sh}
└── tests/
    ├── conftest.py
    ├── test_session.py
    ├── test_compose.py
    ├── test_repo.py
    ├── test_dump.py
    ├── test_docker.py
    ├── test_cli.py
    └── integration/
        ├── conftest.py
        ├── test_end_to_end.py
        └── test_isolation.py
```

## Commit history (v0.1.0)

27 commits from initial spec to tag. Workflow: brainstorming → design spec → implementation plan → 16 TDD tasks (each with implementer + spec reviewer + code-quality reviewer subagents) → README + tag.

No `Co-Authored-By` trailers per user preference.

## Open follow-ups (not blocking v0.1.0)

- `build_dir_db` in `ComposeConfig` is currently dead — the db service uses a prebuilt image. Drop the field if no Docker `build:` context for db is added later.
- `compose_logs_follow`'s background process has no shutdown signal; relies on the OS to reap when its parent compose project tears down. If a session is created but never finished or stopped, the follower keeps running. Consider a `compose_logs_stop` hook tied to `meta.follower_pid`.
- Smoke test against real `claude` binary (mentioned in spec testing section, currently manual). Worth scripting into a nightly job once CI lands.
- The integration test reuses a single `tmp_path` across the fixture-repo + tiny-dump + assembled-images trees; on a flaky filesystem this could intermittently conflict. Not seen in practice, low priority.
