# claude-code-sandbox

Run Claude Code on production-data tasks without giving the agent your host machine or your real database.

## Why

Long-running, autonomous Claude Code goals — refactors, migrations, debugging — usually need a copy of production data to be useful. But running an agent against your live Postgres is reckless, and running it on your laptop gives it your filesystem, your SSH keys, and your whole machine. This repo spawns a per-task sandbox: fresh Postgres dumped from your real DB (or a frozen S3 snapshot), STS-scoped read-only S3 access for app uploads, isolated filesystem, allowlisted internet egress, and the agent's work returned as a reviewable git branch.

## How it works

Each `sandbox start` is one Docker Compose project with three services on three networks:

```
                        agent_net (internal — no direct egress)
                        ┌────────────────────────────────────────┐
                        │  agent  ────►  proxy (Squid allowlist) │
                        └──────┬───────────────────┬─────────────┘
                          db_net (internal)   proxy_egress_net (internet)
                               ┌──┴─────┐
                               │   db   │   ◄── S3 dump cp'd in at runtime
                               └────────┘
```

The CLI on the host assembles each session: pulls the Postgres dump from S3, copies it into a stock `postgres:16` container and runs `pg_restore` (the dump never lives in an image layer), bundles your repo and `docker cp`s it in, renders a per-session Squid allowlist, then starts the agent detached. The agent works on a `sandbox/<id>` branch, you `git fetch` it back when it's done. Containers never bind-mount host paths; auth flows in as a single env var.

## Features

- **Long-running detached sessions.** Kick off, walk away, retrieve when done. ULID session ids; multiple concurrent sessions.
- **Claude subscription auth.** `claude setup-token` once on your host, export `CLAUDE_CODE_OAUTH_TOKEN`, use your Pro/Max/Team quota — no per-token API charges. `ANTHROPIC_API_KEY` falls back to API billing.
- **Postgres dump — live or frozen.** `--postgres-source postgres://…` runs `pg_dump` in a one-shot container against your real DB, caches it on the host (`--max-dump-age` controls TTL). Or `--dump-bucket B --dump-key K` pulls a pre-staged dump from S3 for reproducible CI runs. Either way the dump enters the sandbox via `docker cp` and never lives in an image layer.
- **STS-scoped AWS access for the agent.** `--aws-profile X --s3-buckets b1,b2` calls `sts:GetFederationToken` with a session policy limited to `s3:GetObject` + `s3:ListBucket` on those buckets only. The agent inside the sandbox makes real boto3 calls; your raw credentials never leave the host. Escape hatch: `--aws-unsafe-passthrough` with a stderr warning.
- **Allowlisted egress.** Agent reaches only Anthropic, GitHub, PyPI, npm, and `.amazonaws.com` by default. Customize with `--egress-allowlist anthropic,github,aws` or `--extra-egress-allowlist data.example.com`. Everything else gets a 403 from the Squid proxy.
- **Repo round-trip via git bundle.** Host repo → bundle → cloned in container → agent edits → bundle out → host-side bare repo + `patch.diff`. No bind-mount of your working tree.
- **Per-session ephemerality.** `sandbox finish` (or `stop`) tears down Compose, terminates the log follower, and removes the per-session agent image. `sandbox prune` cleans state dirs after 30 days.

## Install

**Just use it** — install the CLI on your `PATH` via [uv](https://docs.astral.sh/uv/):

```sh
uv tool install git+https://github.com/ccomkhj/claude_sandbox.git
sandbox --help
```

Upgrade with `uv tool upgrade claude-code-sandbox`, uninstall with `uv tool uninstall claude-code-sandbox`.

**Develop on it:**

```sh
git clone https://github.com/ccomkhj/claude_sandbox.git
cd claude_sandbox
uv sync --all-extras           # creates .venv + installs deps + lockfile
uv run sandbox --help          # runs without activating the venv
```

Requires Docker (with `compose` v2), Python 3.11+, uv, and AWS credentials for the dump bucket.

## Use

```sh
# One-time host setup: mint a subscription-billed token
claude setup-token
export CLAUDE_CODE_OAUTH_TOKEN=<value-from-setup-token>

# Kick off a session — live Postgres source (CLI runs pg_dump for you)
sandbox start --repo ~/code/my-app --goal "refactor X to use the new payments module" \
  --postgres-source 'postgres://readonly_user@my-rds.amazonaws.com:5432/appdb'
# PGPASSWORD in env supplies the password; --max-dump-age 1h reuses the cached dump.

# Or use a frozen pg_dump in S3 (CI / reproducible runs)
sandbox start --repo ~/code/my-app --goal "..." \
  --dump-bucket my-dumps --dump-key prod/latest.dump

# Let the agent read your S3 buckets — STS-scoped read-only credentials
sandbox start --repo ~/code/my-app --goal "..." \
  --postgres-source 'postgres://u@my-rds.amazonaws.com/appdb' \
  --aws-profile readonly-prod \
  --s3-buckets user-uploads,exports

# Manage running sessions
sandbox list                    # all sessions
sandbox status <id>             # snapshot of one
sandbox logs <id> -f            # tail logs
sandbox finish <id>             # pull branch back as a patch
sandbox stop <id>               # force-stop a stuck session
sandbox prune                   # drop finished sessions >30 days old
```

## Testing

```sh
uv sync --all-extras
uv run pytest                       # ~120 unit tests, <1s
uv run pytest --run-integration     # +3 real-Docker tests, ~70s on cached layers
uv run pytest --run-smoke           # +1 real-Claude smoke (needs CLAUDE_CODE_OAUTH_TOKEN)
```

## License

Apache License 2.0 — see [`LICENSE`](LICENSE).
