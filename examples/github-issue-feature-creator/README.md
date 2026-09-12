# GitHub Issue Feature Creator Pipeline

Design and implement a feature from a GitHub issue. The planner posts a feature design as an issue comment, checkpoints for human review, then the implementer builds it, the tester writes tests, and the reviewer evaluates the result.

## Flow

```
ISSUE.md → [planner] → posts design as GH comment → CHECKPOINT
                                                        ↓
                                        Human reviews design on GitHub
                                                        ↓
                                                  simplecode resume
                                                        ↓
           [implementer] → builds feature → [tester] → writes tests
                                                        ↓
                                              [reviewer] → CHECKPOINT
                                                        ↓
                                        Human reviews verdict
                                                        ↓
                                                  simplecode resume --approve
                                                        ↓
        [executor] → addresses review comments → [retester] → [cleanup]
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
vim examples/github-issue-feature-creator/ISSUE.md

# Run the pipeline — it will checkpoint after the planner posts the design
simplecode run \
  -f examples/github-issue-feature-creator/pipeline.yaml \
  -r claude \
  -w /path/to/target-repo \
  -i examples/github-issue-feature-creator/ISSUE.md \
  --gate-mode checkpoint \
  -t 86400

# Check waiting sessions
simplecode sessions --status waiting

# After reviewing the design on GitHub, resume with feedback or approve:
simplecode resume -s <session-id> --feedback "Use the repository pattern instead of raw queries"
simplecode resume -s <session-id> --approve

# After the reviewer finishes, approve the final verdict:
simplecode resume -s <session-id> --approve
```

## Agents

| Agent | Gate | Permissions | Description |
|-------|------|-------------|-------------|
| planner | human | write | Explores codebase, designs feature, posts plan to GitHub issue |
| implementer | — | write | Builds the feature per PLAN.md |
| tester | — | write | Writes and runs tests for the new feature |
| reviewer | human | read-only | Reviews implementation against the plan |
| executor | — | write | Addresses critical/warning findings from the review |
| retester | — | write | Re-runs tests after review fixes |
| cleanup | — | write | Leaves the tree tidy after the run |
