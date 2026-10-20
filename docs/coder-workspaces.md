# Coder workspaces

SimpleCode can run unattended factory agents in short-lived
[Coder](https://coder.com/) workspaces. Coder supplies the workspace lifecycle
and remote shell; the factory remains responsible for the queue, Git bundle
transfer, validation, GitHub publication, and Plane state.

The repository includes a Linux-only local development bundle. It runs a pinned
Coder control plane with PostgreSQL and provisions workspace containers from
the locally built `simplecode-agent:latest` image.

## Start locally

Prerequisites:

- Linux Docker Engine with Compose v2
- `openssl`, `stat`, and the [Coder CLI](https://coder.com/docs/install/cli)
- at least 2 CPU cores and 4 GB free memory

Start the complete local coding stack, including the workspace image, Plane,
Keycloak, Coder, 9Router, and CocoIndex:

```bash
make stack-up
```

`stack-up` runs the idempotent initialization and SSO configuration steps.
`coder-init` creates the shared `simplecode-stack` and `simplecode-sso` Docker
networks and writes generated database/OIDC secrets to the ignored
`docker/coder/.env`. Coder is available at <http://coder.localhost:7080>.
On the first run only, create or choose a Keycloak user, sign in to Coder with
**Sign in with Keycloak**, then authenticate the CLI and publish the reference
template. Coder grants its owner role to the first account that signs in:

```bash
make plane-sso-user EMAIL=you@example.com PASSWORD='...'
coder login http://coder.localhost:7080
make coder-template-push
```

The `simplecode-factory` template creates an ephemeral container attached to
`simplecode-stack`, with its Coder agent rooted at `/workspace/repo`. It can
reach the shared model gateway at `http://9router:20128`; it does not receive
the host's GitHub, Plane, Coder session, or SSH credentials. Coder injects only
its own agent token, scoped with `no_user_data`, so the agent can connect back
to the control plane.

Useful operations:

```bash
make coder-logs
make stack-ps
make stack-down
```

The control plane has `/var/run/docker.sock` mounted so its local Terraform
provisioner can create workspace containers. That is a development convenience
with host-level impact. Do not copy this Compose design to a production host;
use a dedicated Coder deployment and an approved Kubernetes or cloud template.

## Keycloak authentication

Coder supports generic [OIDC login](https://coder.com/docs/admin/users/oidc-auth/)
with Keycloak. The standard stack command starts the existing Plane SSO stack,
creates Coder's Keycloak client, and enables its OIDC overlay:

```bash
make stack-up
```

This command:

1. starts LDAP and Keycloak from the Plane SSO bundle;
2. idempotently creates the confidential `coder` client in realm `plane`;
3. configures the exact callback
   `http://coder.localhost:7080/api/v2/users/oidc/callback`; and
4. starts Coder with Keycloak OIDC enabled.

Both browsers and containers use the issuer
`http://keycloak.localhost:8280/realms/plane`. Browsers resolve the reserved
`.localhost` name to loopback, while Compose assigns the same hostname to
Keycloak on `simplecode-sso`. This keeps discovery, authorization, and token
validation on one issuer without disabling issuer checks.

Create a login with `make plane-sso-user`, then choose **Sign in with
Keycloak** in Coder. To refresh only the Keycloak client after changing URLs,
run:

```bash
make coder-sso-config
```

Basic OIDC login does not require a Premium license. Advanced IdP group, role,
and organization synchronization does; this local bundle deliberately does not
configure those features. Built-in password authentication is disabled in the
SSO overlay, so Keycloak is the normal interactive login path. Coder retains
password access for owner accounts as an upstream break-glass lockout safeguard;
that exception cannot be disabled with `CODER_DISABLE_PASSWORD_AUTH`.

## Connect the software factory

Create an API token for the dedicated factory user after signing in to Coder.
Export it only in the factory worker's environment:

```bash
export CODER_SESSION_TOKEN='...'
```

Configure a factory with the published template:

```yaml
workspace:
  provider: coder
  coder:
    url: http://coder.localhost:7080
    token_env: CODER_SESSION_TOKEN
    template: simplecode-factory
    provision_timeout: 600
    cleanup_timeout: 600
    stop_after: 2h
    max_bundle_mb: 512
```

The factory creates a fresh workspace for each attempt, imports the selected
branch with a Git bundle, runs the pipeline through `coder ssh`, verifies and
imports the result bundle locally, and deletes the workspace. GitHub publishing
continues on the factory worker, so GitHub credentials never enter Coder.

## Configuration and recovery

- Copy `docker/coder/.env.example` only through `make coder-init`; `.env` files
  are ignored and must not be committed.
- Override pinned images or ports in `docker/coder/.env` when necessary.
- If Docker reports a socket permission error, rerun `make coder-init` after
  correcting Docker access; `CODER_DOCKER_GID` must match the socket group.
- If OIDC reports an invalid redirect, rerun `make coder-sso-config` and confirm
  `CODER_ACCESS_URL` still matches the browser URL exactly.
- If the issuer cannot be reached, confirm the `simplecode-sso` network exists
  and Keycloak has the `keycloak.localhost` alias with `docker network inspect`.
- `make coder-down` preserves PostgreSQL and Coder home volumes. Remove those
  named volumes manually only when intentionally resetting local Coder data.
