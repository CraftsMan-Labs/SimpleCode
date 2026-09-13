# Spec → Tickets Pipeline

Turn an ambiguous task into an **agreed spec**, then cut that work into independently verifiable tickets.

This is the pre-factory chain. No code changes happen here.

## Flow

```
TASK.md ──→ spec-interviewer (explore → interview → agreed spec)
                │
                └─ [GATE: human answers questions / approves the spec]
                        │
                        └──→ ticket-writer (cut tickets → fitness review)
```

## Agents

| Agent | Permissions | Gate | Role |
|-------|-------------|------|------|
| `spec-interviewer` | read-only | human | Grill the task one decision at a time until `SPEC STATUS: AGREED` |
| `ticket-writer` | read-only | none | Convert only the approved spec into ordered tickets |

## Usage

1. Write the task:

```bash
cp TASK.md ~/projects/myapp/TASK.md
# Edit TASK.md with the real request and open questions
```

2. Run the pipeline:

```bash
simplecode run \
  -f pipeline.yaml \
  -r claude \
  -w ~/projects/myapp \
  -i ~/projects/myapp/TASK.md \
  -t 86400
```

3. At the **spec-interviewer gate**:
   - Answer questions as feedback (one decision at a time is fine)
   - Type `PNS` only when the agreed spec looks right

4. Save the ticket you want to execute next into `TICKET.md`, then run the delivery chain:

```bash
simplecode run \
  -f ../ticket-delivery/pipeline.yaml \
  -r claude \
  -w ~/projects/myapp \
  -i TICKET.md \
  -t 86400
```

## Artifacts

| Output | Meaning |
|--------|---------|
| `spec-interviewer/output.md` | Interview trail + agreed spec |
| `ticket-writer/output.md` | Ordered tickets + handoff block |

Look for:

- `SPEC STATUS: AGREED`
- `TICKETS STATUS: READY`

## Notes

- Model and runtime stay configurable at invocation time (`-r`, `-m`, or per-agent overrides).
- This chain stops at tickets. Closing work happens in `ticket-delivery`.
