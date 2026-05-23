# claude-code-sandbox

Run Claude Code in an isolated Docker sandbox against a production Postgres dump.

The agent gets a fresh Postgres restored from S3, a copy of your repo on its own branch, and a Claude Code installation with your existing credentials — all without bind-mounting your host repo or credential directory. Long-running goal-mode tasks run detached; you retrieve the agent's branch when it's done.

## Prerequisites

- Docker Desktop, or any Docker daemon with `docker compose` v2
- Python 3.11+
- Claude auth — **one of**:
  - **Recommended (subscription billing).** Run `claude setup-token` once on your host to mint a long-lived OAuth token from your Pro/Max/Team subscription. Then `export CLAUDE_CODE_OAUTH_TOKEN=<value>` in the shell where you run `sandbox`. The agent inside the sandbox picks up the token from env and authenticates against your subscription quota — no per-token API charges.
  - **Fallback (pay-per-token API billing).** Set `ANTHROPIC_API_KEY` in env. Used only if `CLAUDE_CODE_OAUTH_TOKEN` is unset.
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

# Restrict egress (default: anthropic + github + python + node)
sandbox start \
  --repo ~/code/my-app \
  --goal "..." \
  --dump-bucket my-dumps --dump-key prod/latest.dump \
  --egress-allowlist anthropic,github \
  --extra-egress-allowlist data.example.com

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

- **No broad host filesystem access.** Inputs are copied into containers by the CLI, not exposed through bind mounts. The repo bundle is copied into the already-running agent container with `docker cp`, then deleted from the host session input directory. The Claude auth token enters as a container environment variable (set per-session by the CLI from your host's `CLAUDE_CODE_OAUTH_TOKEN` or `ANTHROPIC_API_KEY`) — your host `~/.claude/` directory is never read or copied. Outputs come back via `docker cp` of a git bundle the agent writes on exit. The rendered `compose.yml` has zero host bind-mounts (asserted in tests).
- **Database has no public network.** The Postgres container sits on a `internal: true` Docker network — reachable by the agent at `db:5432`, but cannot exfiltrate. Verified live by the isolation integration test.
- **Per-session ephemerality.** Each session has its own compose project and agent image. The Postgres dump is `docker cp`'d into a stock `postgres:16` container at runtime and wiped immediately after restore — it never lives in an image layer. `sandbox finish` (or `sandbox stop`) tears down Compose resources, terminates the log follower, and removes the per-session agent image.
- **Allowlisted egress through a per-session proxy (v0.3.0).** The agent network is `internal: true` — the agent has **no direct path to the public internet**. Egress flows through a per-session Squid proxy on its own bridge network; only FQDNs on the rendered allowlist are reachable. The default allowlist covers Anthropic API, GitHub, PyPI, and npm. Customize with `--egress-allowlist anthropic,github` (subsetting) or `--extra-egress-allowlist data.example.com,assets.example.com` (one-off additions). Verified live by the isolation integration test: allowlisted hosts reachable, `example.com` returns Squid's 403.

## Testing

```sh
pip install -e '.[dev]'
pytest                          # fast unit tests (~90 tests, < 1s)
pytest --run-integration        # also runs real-Docker tests (~5-15 min first time, ~45s on cached layers)
pytest --run-smoke              # also exercises the real `claude` binary; needs ANTHROPIC_API_KEY
                                # (or a fresh ~/.claude/.credentials.json on Linux)
```

## Design

Full design doc: `docs/superpowers/specs/2026-05-21-claude-code-sandbox-design.md`.
Implementation plan: `docs/superpowers/plans/2026-05-21-claude-code-sandbox.md`.
