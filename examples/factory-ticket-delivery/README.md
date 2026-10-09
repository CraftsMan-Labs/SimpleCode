# Factory Ticket Delivery Pipeline

Deliver **one ticket** through an unattended harness chain:

```
Explore → Build → Adversarial → Fix → QA
```

This is the per-ticket factory chain with **no human gate**. A parent harness
reads the final machine-readable verdict and decides whether to close or retry.

## Flow

```
TICKET.md ──→ explorer (map codebase → implementation brief)
                 │
                 └──→ builder (implement → test → commit)
                           │
                           └──→ adversarial (attack → fixer-ready packet)
                                     │
                                     └──→ fixer (apply must-fix or no-op → test → commit)
                                               │
                                               └──→ qa (evidence → FACTORY_VERDICT)
```

SimpleCode is linear. Each agent's first prompt receives `-i` files (explorer
only) or prior agents' `output.md` files under `## Context from previous agents`.
Prompts 2+ share that agent's session history. Later agents never see `TICKET.md`
again — they must use the explorer restatement and later status blocks.

## Agents

| Agent | Permissions | Gate | Role |
|-------|-------------|------|------|
| `explorer` | read-only | none | Inspect first; produce a scoped brief |
| `builder` | write | none | Implement the ticket, run tests, and commit |
| `adversarial` | read-only | none | Attack the work; emit an actionable fixer packet |
| `fixer` | write | none | Apply must-fix items if any, then retest and commit |
| `qa` | read-only | none | Score evidence and emit `FACTORY_VERDICT` |

There is no human gate on any agent.

## Usage

1. Prepare one ticket file (usually copied from `spec-to-tickets` output):

```bash
cp TICKET.md ~/projects/myapp/TICKET.md
# Or paste a single ticket body into TICKET.md
```

2. Run the pipeline:

```bash
simplecode run \
  -f pipeline.yaml \
  -r claude \
  -w ~/projects/myapp \
  -i ~/projects/myapp/TICKET.md \
  -t 86400
```

3. Read the QA `output.md` last line. A parent harness can parse it without a human:

```text
FACTORY_VERDICT: PASS
```

or

```text
FACTORY_VERDICT: BLOCKED
```

## Status lines to watch

| Line | Meaning |
|------|---------|
| `EXPLORE STATUS: READY` | Builder may start from the brief |
| `BUILD STATUS: COMPLETE` / `BLOCKED` | Implementation + test + commit outcome |
| `ADVERSARIAL STATUS: CLEAR` / `FINDINGS` | Attack result; FINDINGS includes must-fix items |
| `FIX STATUS: NOOP` / `COMPLETE` / `BLOCKED` | Repair outcome |
| `FACTORY_VERDICT: PASS` / `BLOCKED` | Unattended close recommendation |

## Context handoffs

Each agent writes a structured `output.md` that the next agent consumes. Required
handoff content:

- **explorer** — ticket restatement, acceptance criteria, ordered sketch
- **builder** — files changed, tests run, commit hash, remaining risks
- **adversarial** — numbered must-fix items (path, change, verify command)
- **fixer** — items applied or skipped, new commit hash, test results
- **qa** — rubric + evidence, then exactly one `FACTORY_VERDICT` line

## Important limitation

This chain is still **linear**. The fixer is the single in-pipeline repair pass
after adversarial review. If QA returns `FACTORY_VERDICT: BLOCKED`, SimpleCode
does **not** loop back to builder or fixer.

Typical recovery for a parent factory:

1. Parse the QA blockers from `qa/output.md`
2. Re-run this pipeline with the same ticket plus repair notes as an extra `-i`
3. Close the ticket only when the last line is `FACTORY_VERDICT: PASS`

## Notes

- Keep one ticket per run. Do not feed the whole backlog.
- Model/runtime stay configurable at invocation time.
- Pair with `../spec-to-tickets` when you still need an agreed spec before cutting work.
- Pair with `../ticket-delivery` when you want a human QA gate instead of this unattended verdict.
