# `.simplecode` pipelines

Project and global pipeline registry. Pipelines stay the same unit (`pipeline.yaml` + `prompts/`); `.simplecode` is where SimpleCode looks them up by name.

## Layout

**Project** (walks up from your working directory):

```
.simplecode/
└── pipelines/
    └── feature-dev/
        ├── pipeline.yaml
        ├── setup.sh
        └── prompts/
            ├── planner/
            ├── executor/
            └── reviewer/
```

**Global** (shared across projects):

```
~/.simplecode/
├── simplecode.db      # sessions (existing)
├── repos/             # webhook clones (existing)
└── pipelines/         # named global pipelines
    └── feature-dev/
        └── ...
```

## Starter workflow

```bash
# In the repo you want to work on
cd /path/to/your-repo

# Scaffold a project pipeline
simplecode init feature-dev

# Or a reusable global one
simplecode init feature-dev --global

# See what SimpleCode can find
simplecode list
simplecode list feature-dev

# Run by name (project wins over global on name clash)
echo "Add OAuth2 login" > TASK.md
simplecode run feature-dev -r claude -w . -i TASK.md -t 86400
```

Explicit paths still work:

```bash
simplecode run -f examples/plan-execute-review/pipeline.yaml -w .
simplecode init legacy --path ./my-pipeline
```

## Resolution rules

1. Path to `pipeline.yaml` or a pipeline directory → use as-is
2. Bare name → project `.simplecode/pipelines/<name>/`
3. Else → `~/.simplecode/pipelines/<name>/`

Project root is the nearest ancestor directory that contains `.simplecode/`.

## What to commit

Commit project `.simplecode/pipelines/` so the team shares the same pipelines. Do not commit machine-local DB paths under `~/.simplecode/`.
