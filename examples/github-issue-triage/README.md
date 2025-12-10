# GitHub Issue Triage Pipeline

Investigate a GitHub issue, propose a plan (posted as an issue comment), wait for human approval, then execute the fix and review it.

## Flow

```
ISSUE.md → [investigator] → posts plan as GH comment → CHECKPOINT
                                                            ↓
                                            Human reviews on GitHub
                                                            ↓
                                                      simplecode resume
                                                            ↓
           [executor] → implements the plan → [reviewer] → CHECKPOINT
                                                            ↓
                                            Human reviews verdict
                                                            ↓
                                                      simplecode resume --approve
```

## Prerequisites

- `gh` CLI authenticated (`gh auth login`)
- Target repository cloned locally
- Optional: PostgreSQL for team use (defaults to SQLite at `~/.simplecode/simplecode.db`)

## Usage

```bash
# Optional: set PostgreSQL URL (otherwise SQLite is used automatically)
export SIMPLECODE_DATABASE_URL=postgres://localhost/simplecode

# Edit ISSUE.md with the GitHub issue URL
vim examples/github-issue-triage/ISSUE.md

# Run the pipeline — it will checkpoint after the investigator posts the plan
simplecode run \
  -f examples/github-issue-triage/pipeline.yaml \
  -r claude \
  -w /path/to/target-repo \
  -i examples/github-issue-triage/ISSUE.md \
  --gate-mode checkpoint \
  -t 86400

# Check waiting sessions
simplecode sessions --status waiting

# After reviewing the plan on GitHub, resume with feedback or approve:
simplecode resume -s <session-id> --feedback "Also handle the edge case in parse()"
simplecode resume -s <session-id> --approve

# After the reviewer finishes, approve the final verdict:
simplecode resume -s <session-id> --approve
```

## Agents

| Agent | Gate | Permissions | Description |
|-------|------|-------------|-------------|
| investigator | human | write | Explores codebase, creates PLAN.md, posts to GitHub issue |
| executor | — | write | Implements the plan from PLAN.md |
| reviewer | human | read-only | Reviews changes for correctness, security, edge cases |
