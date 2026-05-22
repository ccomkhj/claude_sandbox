#!/usr/bin/env bash
set -euo pipefail

: "${GOAL:?GOAL env var required}"
: "${BRANCH:?BRANCH env var required}"

# Shred credentials immediately (we don't use them but the contract is symmetric with the real agent).
shred -u /input/.credentials.json 2>/dev/null || true

git clone /input/repo.bundle /work/repo
cd /work/repo
git config user.email "stub@sandbox.local"
git config user.name "stub agent"

BASE_BRANCH="$(git symbolic-ref --short HEAD 2>/dev/null || echo main)"

git checkout -b "$BRANCH"

mkdir -p /output
finish() {
  rc=$?
  set +e
  cd /work/repo
  git add -A
  git commit -m "stub: $GOAL" >/dev/null 2>&1
  if git show-ref --verify --quiet "refs/heads/${BASE_BRANCH}"; then
    git bundle create /output/branch.bundle "$BASE_BRANCH" "$BRANCH" >/dev/null 2>&1
  else
    git bundle create /output/branch.bundle "$BRANCH" >/dev/null 2>&1
  fi
  echo "$rc" > /output/exit_code
  echo "$BASE_BRANCH" > /output/base_branch
}
trap finish EXIT

# Stub work: write a marker file containing the goal, then exit.
echo "$GOAL" > /work/repo/STUB_OUTPUT.md
