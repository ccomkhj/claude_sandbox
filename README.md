# claude-code-sandbox

Run Claude Code in an isolated Docker sandbox against a production Postgres dump.

The agent gets a fresh Postgres restored from S3, a copy of your repo on its own branch, and a Claude Code installation with your existing credentials — all in a container that cannot read your host filesystem and cannot reach host services. Long-running goal-mode tasks run detached; you retrieve the agent's branch when it's done.

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

# When it exits, pull the branch back to your local repo
sandbox finish 01HK3P
# → prints:  git fetch ~/.sandbox/sessions/01HK3P.../bare.git sandbox/01HK3P...

# Force-stop one that's stuck
sandbox stop 01HK3P

# Clean up sessions older than 30 days
sandbox prune
```

## What's isolated

- **No host filesystem access.** Inputs (repo bundle, ~/.claude credentials, Postgres dump) enter the container via Docker build context, not bind mounts. Outputs come back via `docker cp` of a git bundle the agent writes on exit. The rendered `compose.yml` has zero host bind-mounts (asserted in tests).
- **Database has no public network.** The Postgres container sits on a `internal: true` Docker network — reachable by the agent at `db:5432`, but cannot exfiltrate. Verified live by the isolation integration test.
- **Per-session ephemerality.** Each session has its own compose project, db image, and agent image. `sandbox finish` (or `sandbox stop`) tears them all down with `--rmi local`, removing the per-session image that contains the copied credentials.

## Testing

```sh
pip install -e '.[dev]'
pytest                          # fast unit tests (~50 tests, < 1s)
pytest --run-integration        # also runs real-Docker tests (~5-15 min first time, ~30s on cached layers)
```

## Design

Full design doc: `docs/superpowers/specs/2026-05-21-claude-code-sandbox-design.md`.
Implementation plan: `docs/superpowers/plans/2026-05-21-claude-code-sandbox.md`.
