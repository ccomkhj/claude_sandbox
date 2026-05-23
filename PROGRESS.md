# Progress — claude-code-sandbox v0.2.0

**Status:** v0.2.0 shipped. Tag `v0.2.0` on `main`.

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
| Per-session agent image removed explicitly | `finish` / `stop` call `docker image rm -f` for the custom agent tag (db now uses shared upstream `postgres:16`) |
| Postgres dump never lives in an image layer | v0.2.0: db service uses upstream `postgres:16`; dump enters via `docker cp` at runtime and is wiped after `pg_restore` |

## Test status

```
66 unit tests + 2 integration tests passing.
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
| `images/db/` | _removed in v0.2.0._ The db service uses the upstream `postgres:16` image; the dump is `docker cp`'d in at runtime and `pg_restore`d. |
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

## v0.1.1 — Post-review hardening

Follow-up review after `v0.1.0` found several real gaps and tightened the implementation:

1. **Agent credentials no longer enter the Docker build.** The agent image is static; `repo.bundle` and `.credentials.json` are copied into the running container with `docker cp`, then removed from the host session input directory. The entrypoint waits for those files before starting Claude Code.
2. **Custom-tagged per-session images are removed explicitly.** `finish` and `stop` now call `docker image rm -f` for the recorded agent/db image tags instead of relying on `docker compose down --rmi local`, which does not remove custom-tagged images.
3. **Agent result metadata is consumed.** `finish` copies `/output/exit_code` and `/output/base_branch`, records them in `meta.json`, marks non-zero exits as `crashed`, and scopes `patch.diff` to the actual base branch instead of assuming `main`.

## v0.2.0

Four follow-ups, scoped to a coherent release:

1. **Postgres dump no longer in image layers.** The db service uses upstream `postgres:16`. The CLI brings it up, waits for `pg_isready`, then `docker cp`'s the dump into the running container, runs `pg_restore`, and wipes the dump file. No per-session `sandbox-db:*` image is created. (`images/db/` deleted.)
2. **Log follower lifecycle.** `meta.follower_pid` is now terminated by `finish`, `stop`, and the failed-start cleanup path via new `docker.terminate_pid(pid)` helper (SIGTERM → wait → SIGKILL; tolerates already-gone).
3. **Dead `build_dir_db` field removed** from `ComposeConfig`.
4. **CLI UX:**
   - `sandbox list` verb prints a one-row-per-session table.
   - `status` renders `started_at`/`finished_at` as local ISO timestamps and compose state as `service=state` pairs (instead of raw epoch and raw JSON).
   - Three error wrappers in `cli.main`: `docker.DockerNotRunning` ("Is Docker Desktop running?"), `botocore.NoCredentialsError` ("Set AWS_ACCESS_KEY_ID..."), `LookupError` from `session.find` ("no session matching X" — no traceback).

### v0.2.0 bugs caught by integration test

11. **Upstream `postgres:16` needs env vars the deleted `images/db/Dockerfile` used to provide.** The integration test failed because the db container exited with "superuser password is not specified". The old custom Dockerfile set `POSTGRES_HOST_AUTH_METHOD=trust`, `POSTGRES_DB`, `POSTGRES_USER` via `ENV`. Restored these as a `compose.yml.j2` `environment:` block on the db service, with a unit test asserting both vars are rendered.
12. **E2E "no sandbox-db image exists" assertion was too broad.** Stale `sandbox-db:*` images from v0.1.0 runs would trip the assertion even though the v0.2 run created nothing. Refined to snapshot `sandbox-db:*` image IDs before the run and assert no new ones appeared.

## Layout

```
claude-code-sandbox/
├── pyproject.toml
├── README.md
├── PROGRESS.md                                  (this file)
├── docs/superpowers/
│   ├── specs/
│   │   ├── 2026-05-21-claude-code-sandbox-design.md
│   │   └── 2026-05-23-v0.2.0-design.md
│   └── plans/
│       ├── 2026-05-21-claude-code-sandbox.md
│       └── 2026-05-23-v0.2.0.md
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
│   └── agent-stub/{Dockerfile, entrypoint.sh}
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

## Commit history

- **v0.1.0** — 27 commits from initial spec to tag. Workflow: brainstorming → design spec → implementation plan → 16 TDD tasks (each with implementer + spec reviewer + code-quality reviewer subagents) → README + tag.
- **v0.1.1** — 2 commits landing the post-review hardening pass (docker cp inputs, explicit image cleanup, consume exit_code/base_branch) + docs.
- **v0.2.0** — 11 commits: design spec, plan, and 9 implementation/fix commits delivering runtime dump import, follower lifecycle, dead-field removal, CLI UX.

No `Co-Authored-By` trailers per user preference.

## Follow-up backlog (post v0.2.0)

Items completed in v0.2.0 (struck through). Items 1, 4, 6, 7 remain — they should be addressed before presenting the sandbox as a hardened security boundary.

### P0 — Security boundary clarity

1. **Design an explicit agent egress policy.**
   - Current state: the agent needs public internet egress for Claude Code, GitHub, package registries, and similar tools. The integration test only verifies that the common Docker Desktop host alias path does not reach `host.docker.internal:5432`.
   - Risk: "cannot reach host services" is not a defensible blanket claim without an explicit firewall, proxy, or network policy.
   - Follow up: decide whether the agent should use an allowlisted egress proxy, container firewall rules, or a documented weaker guarantee. Add integration tests for the chosen model.

2. ~~**Move the Postgres dump out of Docker build layers.**~~ Shipped in v0.2.0 — see "v0.2.0" section above.

### P1 — Operational reliability

3. ~~**Stop the background log follower explicitly.**~~ Shipped in v0.2.0.

4. **Add a real-Claude smoke test.**
   - Current state: integration tests use `agent-stub`; the actual `claude` binary path remains manually tested.
   - Risk: package changes, auth format changes, or CLI behavior changes could break real sessions while stub tests still pass.
   - Follow up: add an opt-in smoke test or nightly job that starts a sandbox with a tiny repo/dump and a harmless goal, then verifies the returned branch and exit metadata.

### P2 — Cleanup and ergonomics

5. ~~**Remove dead `build_dir_db` from `ComposeConfig`.**~~ Shipped in v0.2.0.

6. **Make integration fixtures less coupled to one `tmp_path`.**
   - Current state: the fixture repo, tiny dump, and patched image tree share the same test temp root.
   - Risk: low; no observed failures, but filesystem cleanup or path collision bugs would be harder to diagnose.
   - Follow up: split fixture roots or use named subdirectories with stricter cleanup assertions.

7. ~~**Improve CLI operator output.**~~ Shipped in v0.2.0 — `sandbox list`, human-readable timestamps, error wrappers.
