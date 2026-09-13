# Ticket Delivery Pipeline

Deliver **one ticket** through a fixed harness chain:

```
Explore → Build → Adversarial → QA
```

This is the per-ticket factory chain. The meta harness / human closes the ticket only after QA recommends `PASS`.

## Flow

```
TICKET.md ──→ explorer (map codebase → implementation brief)
                 │
                 └──→ builder (implement ticket → verify)
                           │
                           └──→ adversarial (attack the work)
                                     │
                                     └──→ qa (rubric score → PASS/BLOCKED)
                                               │
                                               └─ [GATE: human reviews close packet]
```

## Agents

| Agent | Permissions | Gate | Role |
|-------|-------------|------|------|
| `explorer` | read-only | none | Inspect first; produce a scoped brief |
| `builder` | write | none | Implement only the ticket and gather evidence |
| `adversarial` | read-only | none | Attack missed criteria, regressions, and quality debt |
| `qa` | read-only | human | Score Guideline / Task / Quality / Clean code; recommend close |

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

3. At the **qa gate**, review the close packet:
   - Type feedback if QA should dig into a specific residual
   - Type `PNS` to accept the recommendation and finish the run

## Status lines to watch

| Line | Meaning |
|------|---------|
| `EXPLORE STATUS: READY` | Builder may start |
| `BUILD STATUS: COMPLETE` / `BLOCKED` | Implementation outcome |
| `ADVERSARIAL STATUS: CLEAR` / `FINDINGS` | Attack result |
| `QA VERDICT: PASS` / `BLOCKED` | Close recommendation |

## Important limitation

SimpleCode runs this as a **linear** pipeline. If QA returns `BLOCKED`, the chain reports failure reasons but does **not** automatically loop back to builder.

Typical recovery:

1. Capture the QA blockers
2. Fix manually or start a fresh `ticket-delivery` run with the same ticket plus repair notes as an extra `-i`
3. Close the ticket only when QA returns `PASS`

## Notes

- Keep one ticket per run. Do not feed the whole backlog.
- Model/runtime stay configurable at invocation time.
- Pair with `../spec-to-tickets` when you still need an agreed spec before cutting work.
