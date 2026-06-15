#!/usr/bin/env bash
set -euo pipefail

MAP='{"triage":"/Users/rishub/Desktop/projects/enterprise/craftsmanlabs/SimpleCode/examples/github-issue-triage","feature":"/Users/rishub/Desktop/projects/enterprise/craftsmanlabs/SimpleCode/examples/github-issue-feature-creator"}'

simplecode serve \
  --port 8080 \
  --pipeline-map "$MAP" \
  --github-user CraftsMan-Labs \
  --default-pipeline /Users/rishub/Desktop/projects/enterprise/craftsmanlabs/SimpleCode/examples/github-issue-feature-creator \
  -r cursor \
  -m composer-2.5-fast \
  -w /Users/rishub/Desktop/projects/enterprise/ \
  -t 86400
