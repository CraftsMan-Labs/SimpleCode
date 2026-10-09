# Plane software factory design

## Goal

Turn a Plane work item into a reviewed draft pull request without allowing an
agent to merge code or write directly to a repository's default branch.

The first mandatory human approval is GitHub review of the draft PR. Plane is
the work and status system; SimpleCode is the execution engine; GitHub is the
code-review boundary.

## Lifecycle

1. A human moves a mapped Plane work item to `Ready for Agent`.
2. The factory poller resolves its Plane project through an allowlisted
   project-to-repository mapping and atomically claims the item.
3. The item moves to `In Progress`.
4. SimpleCode runs the unattended delivery pipeline in a named, managed git
   worktree.
5. The publisher accepts only a clean branch with commits ahead of the remote
   base and an exact final `FACTORY_VERDICT: PASS`.
6. The publisher pushes that branch and opens a draft PR.
7. Plane moves to `In Review` and receives the session, branch, and PR URL.
8. Any failure moves the work item to `Needs Attention`; the branch and
   worktree remain available for diagnosis.

`Done` is deliberately outside this MVP. A later GitHub pull-request event
adapter can move an item to `Done` after merge.

## Components and boundaries

- `FactoryConfig` loads Plane connectivity, state names, runtime settings, and
  the repository allowlist. YAML contains an API-key environment-variable name,
  never the key itself.
- `PlaneClient` owns authenticated Plane REST calls and sanitizes errors.
- `SessionStore.factory_jobs` is the durable claim ledger. A Plane
  workspace/project/work-item tuple can be claimed only once.
- `Factory` performs serial polling, lifecycle transitions, pipeline execution,
  verdict checking, and recovery.
- `GitHubRepositoryManager` verifies repository origin, base branch, and host
  cleanliness.
- `GitHubPublisher` validates and pushes only the generated branch, then creates
  a draft PR. It never merges.
- `factory-ticket-delivery` performs explore, build, adversarial review, fix,
  and QA without human gates.

Agents are responsible for understanding, editing, testing, reviewing, and
committing. Deterministic orchestration code owns claims, state transitions,
repository selection, branch validation, pushes, and PR creation.

## Recovery and safety

- Polling is idempotent through the durable claim key.
- A database-backed process lock prevents one factory process from recovering
  another process's live job.
- Each poll reconciles interrupted work. Pre-publication jobs move to
  `Needs Attention`; jobs with a recorded PR retry only the `In Review`
  synchronization.
- Repository names come only from configuration; Plane content cannot select a
  repository or shell command.
- Git and GitHub commands use argument arrays, not a shell.
- Task content is delimited in generated input. It may define ticket scope but
  cannot override factory policy, expose secrets, bypass verification, or
  expand work beyond the allowlisted repository.
- A blocked or missing QA verdict, dirty worktree, no commit, push failure, PR
  failure, or required Plane transition failure cannot produce a successful
  factory result.

## MVP exclusions

- Automatic merge
- Multi-repository work items
- Parallel workers
- Automatic retries of failed or interrupted jobs
- Plane webhook ingestion
- GitHub merge-to-Plane `Done` synchronization

These can be added behind the existing adapter and durable-job boundaries
without changing the delivery pipeline contract.
