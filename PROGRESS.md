# Progress — claude-code-sandbox v0.1.0 + post-review hardening

**Status:** Initial implementation complete at tag `v0.1.0`; post-review hardening has been implemented locally and verified.

`claude-code-sandbox` runs Claude Code in an isolated Docker sandbox against a production Postgres dump. The agent gets a fresh DB from S3, a copy of your repo on its own branch, and your existing Claude Code credentials without bind-mounting your host repo or credential directory. Long-running goal-mode tasks run detached; you retrieve the agent's branch when it's done.

## What got built

A Python CLI (`sandbox`) driving Docker Compose to spawn one isolated agent session per invocation. Each session is a compose project with two services on a private docker network, plus a host-side state directory the CLI owns.

```
host ~/.sandbox/sessions/<id>/                 sandbox compose project

  meta.json
  input/repo.bundle       -- docker cp -->      agent container
  input/.credentials.json -- docker cp -->        - Claude Code
  logs/agent.log          <-- compose logs        - copied repo branch
  branch.bundle           <-- docker cp           - copied ~/.claude
  bare.git/               <-- git fetch
  patch.diff              <-- git format-patch

  S3 dump cache/build ctx  -- docker build -->   db container
                                                 - postgres
                                                 - db_net is internal
```

CLI verbs implemented: `start`, `status`, `logs [-f]`, `finish`, `stop`, `prune`.

## Validated security properties

The design's promised invariants are checked by live integration tests against a real Docker daemon.

| Invariant | Verified by |
|-----------|-------------|
| Agent runs Claude Code in a Docker sandbox | E2E test (Task 14) — `tests/integration/test_end_to_end.py` |
| Common Docker Desktop host alias is not reachable | Isolation test — `host.docker.internal` curl fails (Task 15); this is not a full outbound firewall |
| Sandbox cannot bind-mount host paths | Compose template snapshot test (Task 3) — `tests/test_compose.py::test_no_host_bind_mounts` |
| DB has no internet egress | Isolation test — `getent hosts example.com` fails inside `db` container (Task 15) |
| Agent CAN reach DB on private network | Isolation test — `psql -h db -c "SELECT 1"` succeeds (Task 15) |
| Repo changes return as a reviewable branch | E2E test bundle round-trip (Task 14) |
| `patch.diff` scopes correctly to `main..agent-branch` | Tightened assertion catches the `--root` fallback (Task 12 fix) |
| Per-session images removed explicitly | `finish` / `stop` call `docker image rm -f` for the custom agent and db image tags |

## Test status

```
50 unit tests + 2 integration tests passing.
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
| `sandbox.docker` | Thin subprocess wrappers around `docker compose ...`, `docker cp`, `docker build`, `docker image rm` |
| `sandbox.cli` | argparse entry point; orchestrates the verbs |
| `images/db/` | `postgres:16` image with dump baked into `/docker-entrypoint-initdb.d/` |
| `images/agent/` | `node:20-slim` + `@anthropic-ai/claude-code`; waits for copied inputs, shreds creds, bundles output |
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

## Post-review hardening

Follow-up review after `v0.1.0` found several real gaps and tightened the implementation:

1. **Agent credentials no longer enter the Docker build.** The agent image is static; `repo.bundle` and `.credentials.json` are copied into the running container with `docker cp`, then removed from the host session input directory. The entrypoint waits for those files before starting Claude Code.
2. **Custom-tagged per-session images are removed explicitly.** `finish` and `stop` now call `docker image rm -f` for the recorded agent/db image tags instead of relying on `docker compose down --rmi local`, which does not remove custom-tagged images.
3. **Agent result metadata is consumed.** `finish` copies `/output/exit_code` and `/output/base_branch`, records them in `meta.json`, marks non-zero exits as `crashed`, and scopes `patch.diff` to the actual base branch instead of assuming `main`.

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

## Follow-up backlog

These are the remaining items to handle after the post-review hardening pass. They are not blocking local use, but they should be addressed before presenting the sandbox as a hardened security boundary.

### P0 — Security boundary clarity

1. **Design an explicit agent egress policy.**
   - Current state: the agent needs public internet egress for Claude Code, GitHub, package registries, and similar tools. The integration test only verifies that the common Docker Desktop host alias path does not reach `host.docker.internal:5432`.
   - Risk: "cannot reach host services" is not a defensible blanket claim without an explicit firewall, proxy, or network policy.
   - Follow up: decide whether the agent should use an allowlisted egress proxy, container firewall rules, or a documented weaker guarantee. Add integration tests for the chosen model.

2. **Move the Postgres dump out of Docker build layers.**
   - Current state: the dump is copied into the db image build context and baked into a custom per-session image. That image is explicitly removed on `finish` / `stop`, but the dump exists in image layers until cleanup runs.
   - Risk: failed cleanup, daemon-layer retention, or image inspection can expose dump contents longer than intended.
   - Follow up: copy the dump into a running db container or use an ephemeral Docker volume/import container flow, then delete host-side dump inputs after restore.

### P1 — Operational reliability

3. **Stop the background log follower explicitly.**
   - Current state: `compose_logs_follow` stores `meta.follower_pid`, but there is no `compose_logs_stop` hook. The follower usually exits when Compose resources disappear, but a failed or abandoned session can leave it running.
   - Risk: stale processes and open log followers accumulate across failed sessions.
   - Follow up: add a small process-management helper that terminates `meta.follower_pid` during `finish`, `stop`, and failed `start` cleanup, with tests for missing/already-exited PIDs.

4. **Add a real-Claude smoke test.**
   - Current state: integration tests use `agent-stub`; the actual `claude` binary path remains manually tested.
   - Risk: package changes, auth format changes, or CLI behavior changes could break real sessions while stub tests still pass.
   - Follow up: add an opt-in smoke test or nightly job that starts a sandbox with a tiny repo/dump and a harmless goal, then verifies the returned branch and exit metadata.

### P2 — Cleanup and ergonomics

5. **Remove dead `build_dir_db` from `ComposeConfig`.**
   - Current state: `build_dir_db` is still passed through config, but the db service uses a prebuilt image in the rendered Compose file.
   - Risk: small readability drag and misleading API surface.
   - Follow up: delete the field and update tests unless the db dump import flow is redesigned to use a Compose build context again.

6. **Make integration fixtures less coupled to one `tmp_path`.**
   - Current state: the fixture repo, tiny dump, and patched image tree share the same test temp root.
   - Risk: low; no observed failures, but filesystem cleanup or path collision bugs would be harder to diagnose.
   - Follow up: split fixture roots or use named subdirectories with stricter cleanup assertions.

7. **Improve CLI operator output.**
   - Current state: `status` prints raw epoch timestamps and raw Compose JSON; common failures still bubble up as Python exceptions.
   - Risk: workable for development, rough for regular use.
   - Follow up: add human-readable timestamps, a concise session list command, and friendly error messages for Docker, S3, Git, and credential failures.
