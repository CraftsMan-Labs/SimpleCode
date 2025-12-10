#!/usr/bin/env bash
set -euo pipefail

# Verify gh CLI is authenticated (needed for posting comments)
if ! gh auth status &>/dev/null; then
    echo "Error: gh CLI is not authenticated. Run 'gh auth login' first."
    exit 1
fi
