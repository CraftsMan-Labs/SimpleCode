#!/usr/bin/env bash
set -euo pipefail

# Required env:
#   GITHUB_USER   — your GitHub username (for assignment matching + self-comment skip)
#   GITHUB_REPOS  — comma-separated repos to poll, e.g. owner/repo
#   WORKING_DIR   — local path to the target repo agents work in
#
# Optional env: PORT (8080), RUNTIME (claude), MODEL, TIMEOUT (86400)

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
GITHUB_USER="${GITHUB_USER:?export GITHUB_USER=your-github-username}"
GITHUB_REPOS="${GITHUB_REPOS:?export GITHUB_REPOS=owner/repo}"
WORKING_DIR="${WORKING_DIR:?export WORKING_DIR=/path/to/target-repo}"

MAP="{\"triage\":\"$ROOT/examples/github-issue-triage\",\"feature\":\"$ROOT/examples/github-issue-feature-creator\"}"

args=(
  serve
  --port "${PORT:-8080}"
  --repos "$GITHUB_REPOS"
  --pipeline-map "$MAP"
  --github-user "$GITHUB_USER"
  --default-pipeline "$ROOT/examples/github-issue-feature-creator"
  -r "${RUNTIME:-claude}"
  -w "$WORKING_DIR"
  -t "${TIMEOUT:-86400}"
)
if [[ -n "${MODEL:-}" ]]; then
  args+=(-m "$MODEL")
fi

simplecode "${args[@]}"
