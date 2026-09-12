#!/usr/bin/env bash
# poll merged into serve — use serve.sh with --repos (set GITHUB_REPOS).
exec "$(dirname "$0")/serve.sh"
