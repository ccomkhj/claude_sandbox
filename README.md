# claude-code-sandbox

Run Claude Code in an isolated Docker sandbox against a production Postgres dump.

The agent gets a fresh Postgres restored from S3, a copy of your repo on its own branch, and a Claude Code installation with your existing credentials — all without bind-mounting your host repo or credential directory. Long-running goal-mode tasks run detached; you retrieve the agent's branch when it's done.

## Prerequisites

- Docker Desktop, or any Docker daemon with `docker compose` v2
- Python 3.11+
- A Claude Code login on the host (`claude login`) so `~/.claude/.credentials.json` exists
- AWS credentials with read access to the dump bucket

## Install

```sh
git clone <this repo>
cd claude-code-sandbox
python -m venv .venv && source .venv/bin/activate
pip install -e .
```

This installs the `sandbox` CLI on your PATH.

## Use

```sh
# Kick off a long-running goal task
sandbox start \
  --repo ~/code/my-app \
  --goal "refactor X to use the new payments module" \
  --dump-bucket my-dumps \
  --dump-key prod/latest.dump \
  --db-name appdb
# → prints session id, e.g. 01HK3P0000000000000000

# Check on it
sandbox status 01HK3P
sandbox logs 01HK3P -f

# See all sessions
sandbox list

# When it exits, pull the branch back to your local repo
sandbox finish 01HK3P
# → prints:  git fetch ~/.sandbox/sessions/01HK3P.../bare.git sandbox/01HK3P...

# Force-stop one that's stuck
sandbox stop 01HK3P

# Clean up sessions older than 30 days
sandbox prune
```

## What's isolated

- **No broad host filesystem access.** Inputs are copied into containers by the CLI, not exposed through bind mounts. The repo bundle and Claude credentials are copied into the already-running agent container with `docker cp`, then deleted from the host session input directory. Outputs come back via `docker cp` of a git bundle the agent writes on exit. The rendered `compose.yml` has zero host bind-mounts (asserted in tests).
- **Database has no public network.** The Postgres container sits on a `internal: true` Docker network — reachable by the agent at `db:5432`, but cannot exfiltrate. Verified live by the isolation integration test.
- **Per-session ephemerality.** Each session has its own compose project and agent image. The Postgres dump is `docker cp`'d into a stock `postgres:16` container at runtime and wiped immediately after restore — it never lives in an image layer. `sandbox finish` (or `sandbox stop`) tears down Compose resources, terminates the log follower, and removes the per-session agent image.

The agent container intentionally keeps internet egress for Claude Code, GitHub, package registries, and similar tools. The integration test checks that `host.docker.internal` is not reachable on a common host-service path, but this is not a full outbound firewall.

## Testing

```sh
pip install -e '.[dev]'
pytest                          # fast unit tests (~66 tests, < 1s)
pytest --run-integration        # also runs real-Docker tests (~5-15 min first time, ~30s on cached layers)
```

## Design

Full design doc: `docs/superpowers/specs/2026-05-21-claude-code-sandbox-design.md`.
Implementation plan: `docs/superpowers/plans/2026-05-21-claude-code-sandbox.md`.
