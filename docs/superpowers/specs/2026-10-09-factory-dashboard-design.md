# Factory dashboard design

## Goal

Give the operator of `simplecode factory` a local, dependency-free view of
recent jobs and durable progress without exposing ticket bodies, model output,
credentials, or a new control surface.

The dashboard is observational. Plane remains the work and status system,
SimpleCode remains the execution engine, and GitHub remains the code-review
boundary.

## Architecture

Continuous `simplecode factory` mode starts two components in one process:

1. `Factory` polls Plane and executes one claimed work item at a time.
2. `DashboardServer` serves an embedded HTML page and JSON API from a background
   thread.

Both components use independent `SessionStore` connections to the same SQLite
or PostgreSQL database. Request handlers open and close a store connection per
request so SQLite connections are never shared between HTTP threads. The
dashboard binds to `127.0.0.1`; the CLI default is port `8090`.

The page has no external JavaScript, CSS, images, or runtime dependencies. It
renders untrusted labels with DOM `textContent`, validates pull-request links,
and runs under a nonce-based Content Security Policy.

## Durable event flow

Factory and pipeline boundaries pass through `FactoryProgressTracker`:

1. The tracker updates the current summary fields in `factory_jobs`.
2. It appends a structured row to `factory_events`.
3. At factory milestones and agent boundaries, it best-effort synchronizes the
   existing Plane progress comment.

`factory_jobs` is the dashboard snapshot and durable claim ledger. It records
status, repository, branch, session, pull-request URL, current stage, current
agent and prompt, the Plane progress-comment ID, and last activity time.

`factory_events` is append-only progress history ordered by its monotonic event
ID. Events cover claims, checkout/setup, pipeline and session boundaries,
agent/prompt/gate boundaries, verdict validation, publication, Plane review
synchronization, recovery, completion, and failure.

Only short factory-authored event labels and structured counters enter the
tracker. Plane ticket bodies, comments, prompt text, agent output, stdout, and
model reasoning do not enter dashboard responses. Event messages and metadata
are retained for internal durability but deliberately omitted from the HTTP
API.

## HTTP API and polling

The server accepts only `GET` and `HEAD`:

- `GET /` returns the embedded dashboard.
- `GET /health` returns `{"status":"ok"}` after a database connection succeeds.
- `GET /api/snapshot` returns summary counts and up to 100 recent jobs.
- `GET /api/jobs/<url-encoded-external-key>` returns one job and up to 500
  ordered events.

All other methods return `405 Method Not Allowed`; unknown resources return
`404`. Responses use `Cache-Control: no-store`, `X-Content-Type-Options:
nosniff`, `X-Frame-Options: DENY`, and `Referrer-Policy: no-referrer`.

The browser requests a fresh snapshot and selected-job detail every two
seconds. Polling is intentionally simple: the durable database, not an
in-memory event bus or browser connection, is the source of truth.

## Plane progress comment

Each factory job stores one Plane progress-comment ID. The first visible
milestone creates the comment; later factory milestones and agent boundaries
update that same comment. Prompt boundaries remain durable events but do not
write to Plane, limiting API traffic.

Comment synchronization is best-effort and cannot erase a persisted event or
fail the factory job. A transient update error keeps the same comment ID and
does not create another comment. If Plane explicitly reports that the stored
comment no longer exists, the tracker creates one replacement and stores its
new ID.

The comment contains only status, stage, current agent/prompt when applicable,
session, branch, pull-request URL, and sanitized error text.

## Trust and security boundaries

- The CLI always binds the dashboard to loopback. There is no supported public
  bind option.
- The dashboard has no authentication or TLS. Any local user or process able to
  reach `127.0.0.1:8090` can read its approved fields.
- It is not safe to expose the port through a reverse proxy, SSH tunnel shared
  with untrusted users, container port publication, or a non-loopback bind
  without adding authentication and transport security.
- The HTTP surface is read-only and cannot claim work, change Plane state,
  start agents, publish branches, or mutate the database.
- The API allowlists serialized fields. It excludes Plane API keys, ticket
  bodies/comments, event messages/metadata, prompt content, agent output, model
  reasoning, and the stored Plane comment ID.
- Error text is sanitized and truncated again at serialization time. URLs are
  exposed as links only when they are absolute credential-free HTTP(S) URLs.
- Plane content remains untrusted. Text rendering does not use `innerHTML`, and
  the page denies framing, forms, plugins, and third-party network access.

Loopback is an exposure reduction, not an authentication mechanism. Operators
must treat repository names, branch names, work-item titles/references, session
IDs, and sanitized failures as locally visible operational metadata.

## Lifecycle and failure behavior

The dashboard starts only in continuous mode. `--once` performs one recovery
and polling cycle without starting HTTP. `--no-dashboard` disables HTTP while
leaving factory execution unchanged.

If the requested port is unavailable or dashboard startup otherwise fails, the
CLI prints a sanitized warning and continues factory polling. On normal exit,
factory failure, or interruption, the CLI closes the dashboard and store.

The dashboard is not a liveness dependency for execution. A request-time
database failure returns `503` without exposing exception details. Browser
polling reports data offline and retries on the next two-second interval.
Stopping or refreshing the page does not affect a job.

Factory lifecycle behavior remains unchanged:

- interrupted pre-publication jobs move to `Needs Attention` and are not rerun;
- published jobs retry only Plane `In Review` synchronization;
- completed, failed, and cancelled jobs are terminal;
- durable events survive process and dashboard restarts.

## Decisions

- **Embedded server over a separate service:** one command and no deployment
  dependency for a local operator tool.
- **Database polling over WebSockets/SSE:** restart-safe, backend-neutral, and
  sufficient at a two-second local cadence.
- **Summary row plus append-only events:** fast list rendering with an
  auditable ordered history.
- **One Plane comment over comment-per-event:** useful remote visibility
  without notification noise or API amplification.
- **Boundary-only Plane updates:** prompt events remain inspectable locally
  while Plane writes stay bounded.
- **Read-only, loopback, no auth:** acceptable for the local MVP only; remote or
  multi-user deployment requires an authenticated frontend.
- **Fail-open dashboard startup:** observability failure must not stop delivery.
- **No ticket or model payloads:** operational progress is useful without
  expanding the sensitive-data boundary.
