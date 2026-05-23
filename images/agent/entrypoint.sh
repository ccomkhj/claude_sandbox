#!/usr/bin/env bash
set -euo pipefail

: "${GOAL:?GOAL env var required}"
: "${BRANCH:?BRANCH env var required}"

deadline=$((SECONDS + 120))
while [[ ! -s /input/repo.bundle ]]; do
  if (( SECONDS >= deadline )); then
    echo "timed out waiting for sandbox inputs" >&2
    exit 2
  fi
  sleep 1
done

# Clone repo bundle and check out the agent's branch.
git clone /input/repo.bundle /work/repo
cd /work/repo
git config user.email "agent@sandbox.local"
git config user.name "sandbox agent"

# Capture the base branch (typically `main`) so format_patch can scope the diff.
BASE_BRANCH="$(git symbolic-ref --short HEAD 2>/dev/null || echo main)"

git checkout -b "$BRANCH"

mkdir -p /output
finish() {
  rc=$?
  set +e
  cd /work/repo
  git add -A
  git commit -m "agent wip" >/dev/null 2>&1
  # Include base branch in bundle so host-side format_patch has a base ref.
  if git show-ref --verify --quiet "refs/heads/${BASE_BRANCH}"; then
    git bundle create /output/branch.bundle "$BASE_BRANCH" "$BRANCH" >/dev/null 2>&1
  else
    git bundle create /output/branch.bundle "$BRANCH" >/dev/null 2>&1
  fi
  echo "$rc" > /output/exit_code
  echo "$BASE_BRANCH" > /output/base_branch
}
trap finish EXIT

exec claude --dangerously-skip-permissions -p "$GOAL"
