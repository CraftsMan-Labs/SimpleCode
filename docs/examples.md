# Examples

Real-world pipeline examples you can copy and adapt.

## Ready-to-use examples

| Pipeline | Path | What it does |
|----------|------|--------------|
| Plan → Execute → Review | [`examples/plan-execute-review/`](../examples/plan-execute-review/) | Classic 3-agent feature loop |
| Spec → Tickets | [`examples/spec-to-tickets/`](../examples/spec-to-tickets/) | Grill an ambiguous task into an agreed spec, then cut tickets |
| Ticket Delivery | [`examples/ticket-delivery/`](../examples/ticket-delivery/) | Deliver one ticket: Explore → Build → Adversarial → QA |
| Factory Ticket Delivery | [`examples/factory-ticket-delivery/`](../examples/factory-ticket-delivery/) | Unattended delivery with fix and machine-readable QA |
| Kernel Optimization | [`examples/kernel-optimization/`](../examples/kernel-optimization/) | Compile, validate, benchmark, and optimize a compute kernel |

### Factory handoff (recommended)

Use the two factory chains as **separate** pipelines:

```
TASK.md ──→ spec-to-tickets ──→ agreed spec + tickets
                                      │
                                      ▼ pick one ticket into TICKET.md
                                ticket-delivery ──→ QA PASS/BLOCKED
```

```bash
# 1) Align on the work and cut tickets
simplecode run \
  -f examples/spec-to-tickets/pipeline.yaml \
  -r claude \
  -w ~/projects/myapp \
  -i TASK.md \
  -t 86400

# 2) Save one ticket body to TICKET.md, then deliver it
simplecode run \
  -f examples/ticket-delivery/pipeline.yaml \
  -r claude \
  -w ~/projects/myapp \
  -i TICKET.md \
  -t 86400
```

Model and runtime stay configurable at invocation time (`-r`, `-m`, or per-agent YAML overrides). Do not hard-code vendor models into these examples unless you intentionally want a fixed routing policy.

`ticket-delivery` is linear: a `QA VERDICT: BLOCKED` result reports blockers but does not auto-loop back to the builder. Re-run delivery (or a repair pass) after fixing.

See each example's README for agent details and status lines.

---

## 1. Feature Development Pipeline

Plan, implement, review, and fix — the full SDLC loop.

**pipeline.yaml:**

```yaml
name: feature-dev
description: Full feature development cycle

setup: setup.sh

agents:
  - id: planner
    prompts: prompts/planner/
    gate: human
    permissions: read-only
    model: opus
    timeout: 86400

  - id: executor
    prompts: prompts/executor/
    permissions: write
    model: sonnet
    timeout: 86400

  - id: reviewer
    prompts: prompts/reviewer/
    gate: human
    permissions: read-only
    model: opus

  - id: review-tasks
    prompts: prompts/review-tasks/
    permissions: read-only

  - id: fixer
    prompts: prompts/fixer/
    permissions: write
    model: sonnet
    timeout: 86400
```

**prompts/planner/1.md:**

```markdown
Scan this codebase thoroughly. Identify:
- Tech stack and frameworks
- Project structure and entry points
- All API endpoints
- Key data models
- Existing test coverage
```

**prompts/planner/2.md:**

```markdown
Based on the task in the context and your codebase scan, create a detailed
implementation plan.

For each change:
1. File path
2. What to modify and why
3. Order of implementation
4. Dependencies between changes
5. Risk assessment
```

**prompts/planner/3.md:**

```markdown
Summarise the plan as a clean, reviewable document. Include:
- Overview (1 paragraph)
- Step-by-step plan (numbered)
- Files to modify (table)
- Risks and mitigations
- Estimated complexity
```

**prompts/executor/1.md:**

```markdown
Execute the implementation plan from the previous agent.
Follow the plan step by step. For each step:
1. Read the relevant files
2. Make the changes
3. Verify the changes compile/lint
```

**prompts/executor/2.md:**

```markdown
Run the test suite. Fix any test failures caused by your changes.
If no tests exist for the new code, write them.
```

**prompts/reviewer/1.md:**

```markdown
Review all changes made by the executor agent. Check for:
- Correctness: does the code do what the plan specified?
- Edge cases: are error paths handled?
- Security: any vulnerabilities introduced?
- Style: does it match the codebase conventions?
- Tests: are the new tests meaningful?
```

**prompts/reviewer/2.md:**

```markdown
Create a structured code review. For each finding:
- File and line reference
- Severity: critical / warning / suggestion
- Description of the issue
- Suggested fix
```

**prompts/review-tasks/1.md:**

```markdown
Based on the code review, create a prioritised to-do list.
Format as a checklist:
- [ ] Critical fixes first
- [ ] Then warnings
- [ ] Then suggestions

For each item, include the file path and what specifically to change.
```

**prompts/fixer/1.md:**

```markdown
Work through the to-do list from the review. Fix each item in priority order.
```

**prompts/fixer/2.md:**

```markdown
Run the test suite again. Verify all fixes are correct and no regressions.
Summarise what was fixed.
```

**Run it:**

```bash
uv run simplecode run \
  -f feature-dev/pipeline.yaml \
  -r claude \
  -w ~/projects/myapp \
  -t 86400 \
  -i TASK.md
```

---

## 2. QA Testing Pipeline

Scan a codebase and generate a comprehensive test plan.

```yaml
name: qa-testing
description: Generate and execute a QA test plan

agents:
  - id: scanner
    prompts: prompts/scanner/
    permissions: read-only
    model: sonnet

  - id: planner
    prompts: prompts/planner/
    gate: human
    permissions: read-only
    model: opus

  - id: tester
    prompts: prompts/tester/
    permissions: write
    model: sonnet
    timeout: 86400
```

**prompts/scanner/1.md:**

```markdown
Scan this codebase. For each API endpoint and UI route, list:
- Method, path, handler file
- Input validation
- Auth requirements
- Error handling
```

**prompts/planner/1.md:**

```markdown
Based on the codebase scan, create test cases. For each:
- Test ID (TC-001, TC-002, ...)
- Description
- Type (API / UI / Unit)
- Steps to execute
- Expected result
- Priority (P0 / P1 / P2)

Include both happy-path and error-path scenarios.
```

**prompts/tester/1.md:**

```markdown
Write and run the test cases from the test plan.
Use the project's existing test framework.
```

**setup.sh:**

```bash
#!/usr/bin/env bash
set -euo pipefail
pip install playwright
playwright install chromium
```

---

## 3. Bug Investigation Pipeline

Investigate and fix a bug with structured analysis.

```yaml
name: bug-fix
description: Investigate, fix, and verify a bug

agents:
  - id: investigator
    prompts: prompts/investigator/
    gate: human
    permissions: read-only
    model: opus

  - id: fixer
    prompts: prompts/fixer/
    permissions: write
    model: sonnet
    timeout: 86400
```

**prompts/investigator/1.md:**

```markdown
Investigate the bug described in the context. Find:
1. Reproduction steps
2. Root cause (file, line, logic error)
3. Impact assessment
4. Suggested fix approach
```

**prompts/fixer/1.md:**

```markdown
Implement the fix based on the investigation.
Then write a regression test that would catch this bug.
```

**prompts/fixer/2.md:**

```markdown
Run the full test suite. Verify the fix works and nothing else broke.
Summarise the fix and test results.
```

**Run it:**

```bash
uv run simplecode run \
  -f bug-fix/pipeline.yaml \
  -r claude \
  -w ~/projects/myapp \
  -i BUG-REPORT.md \
  -t 86400
```
