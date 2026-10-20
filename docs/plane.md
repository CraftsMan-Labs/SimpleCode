# Self-hosted Plane

[Plane](https://github.com/makeplane/plane) (Community Edition, AGPL-3.0) runs as its own Docker Compose project, separate from the [agent stack](agent-stack.md). Compose file adapted from upstream's `deployments/cli/community`.

| Item | Value |
|------|-------|
| Compose project | `simplecode-plane` (`docker/plane/compose.yml`) |
| App URL | http://localhost:8180 |
| Instance admin ("god mode") | http://localhost:8180/god-mode/ |
| Public pages | http://localhost:8180/spaces/ |
| Release | `APP_RELEASE` in `docker/plane/.env` (default pinned to `v1.4.2`) |
| Bind | `127.0.0.1` only (`PLANE_HOST_BIND`) |

Port `8180` was chosen because `80`, `8088`, `8090` etc. are commonly in use. Change `LISTEN_HTTP_PORT`, `WEB_URL` and `CORS_ALLOWED_ORIGINS` together if you move it.

## Run

For the complete local development environment, use the repository-level
command. It includes Plane and its Keycloak SSO layer:

```bash
make stack-up
```

The narrower Plane-only commands remain available for maintenance:

```bash
make plane-up      # creates docker/plane/.env with fresh secrets on first run, then starts
make plane-ps
make plane-logs
make plane-down    # keeps data volumes; add `-v` to the compose command to wipe
```

Then open http://localhost:8180/god-mode/ and create the instance admin, then create a workspace at http://localhost:8180.

Services: web, space, admin, live, api, worker, beat-worker, migrator, Postgres 15, Valkey (Redis), RabbitMQ, a MinIO-compatible object store (`silo`) and the Caddy proxy. Expect roughly 2-3 GB RAM.

`docker/plane/.env` is gitignored; `docker/plane/.env.example` is the template.

### Gotchas

- `CERT_ACME_CA` must be non-empty (set in `.env.example`); an empty value makes Caddy crash-loop with `acme_ca ... wrong argument count`.
- Plane-only mode enables email/password by default. The repository's standard
  `make stack-up` SSO configuration disables email/password and magic-link login,
  leaving the Keycloak-backed SSO button as the interactive login path.
- Upgrading: bump `APP_RELEASE`, then `docker compose -f docker/plane/compose.yml pull && make plane-up`. The `migrator` container runs DB migrations.

## Is LDAP available in self-hosted Plane?

**No, not in Community Edition.** Verified three ways against the `v1.4.2` release:

1. **Running backend:** inside `makeplane/plane-backend:v1.4.2`, there is no LDAP Python package installed and zero `.py` files under `/code/plane` mention LDAP. The instance reports `edition: PLANE_COMMUNITY`.
2. **Source:** the only auth providers in `apps/api/plane/authentication/provider/` are email/password, magic code (email), and OAuth for Google, GitHub, GitLab and Gitea. The only "LDAP" hits in the repo are an i18n string (`auth.json`, a UI label with no backend) and pricing-page copy.
3. **Upstream's own pricing UI** (`apps/web/components/workspace/billing/comparison/plans.tsx`) lists LDAP as an **Enterprise-only, "coming soon"** feature. SAML and OIDC are likewise paid (the "One" plan and up), and are not in the CE image.

## LDAP login via Keycloak (verified working)

Plane CE can't speak OIDC/SAML/LDAP, but it has a "Continue with Gitea" login whose host is configurable. `docker/plane/sso/` adds a small shim that answers Plane's Gitea calls and forwards them to Keycloak, which authenticates against LDAP:

```
browser -> sso-edge :8180 -> Plane Caddy -> sso.localhost:8181 (shim) -> Keycloak :8280 -> OpenLDAP
              label rewrite                  Gitea-style API              OIDC + LDAP federation
```

```bash
make plane-sso-up      # OpenLDAP + Keycloak + shim, then points Plane's Gitea login at the shim
make plane-sso-user EMAIL=you@example.com PASSWORD='...'   # runtime Keycloak user; not committed
make stack-sso         # reconfigure 9Router OIDC without restarting the full stack
make plane-sso-logs
make plane-sso-down
```

| Piece | Where |
|-------|-------|
| Plane login | http://localhost:8180 -> **Sign in with SSO** (`PLANE_SSO_LOGIN_LABEL`) |
| Keycloak admin | http://keycloak.localhost:8280 (`admin` / `KEYCLOAK_ADMIN_PASSWORD` in `docker/plane/.env`) |
| Realm / client | realm `plane`, client `plane` (`sso/keycloak/realm-plane.json`) |
| Demo LDAP users | `alice` / `alice-pass`, `bob` / `bob-pass` (`sso/ldap/users.ldif`, dev only) |
| Extra Keycloak user | `make plane-sso-user EMAIL=... PASSWORD=...` (runtime only; not in git) |
| 9Router SSO | http://localhost:20128 → **Sign in with SSO** (`make stack-up`; `make stack-sso` reconfigures it) |
| LDAP admin | `cn=admin,dc=example,dc=org`, password `LDAP_ADMIN_PASSWORD` |

**Tested end to end** (headless Chromium, Plane v1.4.2, Keycloak 26.8): alice and bob each log in with their LDAP password and Plane creates `alice@example.org` / `bob@example.org` with the name taken from LDAP. A wrong password and an unknown user are both rejected at Keycloak and create no Plane account.

### How it fits together

- `sso/shim/shim.py` (stdlib Python) implements the four calls Plane's Gitea provider makes: `/login/oauth/authorize` (redirect), `/login/oauth/access_token`, `/api/v1/user`, `/api/v1/user/emails`. It maps Keycloak's `userinfo` to Gitea's JSON shape and passes through `email_verified`.
- Plane refuses unverified emails (account-takeover guard in `gitea.py`), so Keycloak's LDAP provider has `trustEmail=true` and users need a `mail` attribute in LDAP.
- Official Plane images are never patched or rebuilt. Login is wired through Plane's own instance settings; the visible "Gitea" label is rewritten only at `sso-edge`.
- `sso/configure-plane.sh` writes `IS_GITEA_ENABLED`, `GITEA_HOST`, `GITEA_CLIENT_ID`, `GITEA_CLIENT_SECRET` into Plane's instance configuration (what god mode's Gitea form does) and invalidates the 2-hour `/api/instances/` cache. You can also enter the same values by hand in god mode.
- The same configurator sets Plane's supported `ENABLE_EMAIL_PASSWORD=0` and
  `ENABLE_MAGIC_LINK_LOGIN=0` instance settings, preventing local or magic-link
  authentication while the SSO layer is active.
- `PLANE_SSO_LOGIN_LABEL` (default `SSO`) is an sso-edge setting, not a Plane one. It rewrites the login-button suffix (`Continue with Gitea` → `Continue with SSO`) and swaps the teapot SVG for a lock. God-mode still says Gitea because that is the provider Plane exposes.
- `GITEA_HOST` must work from both the browser (redirect) and Plane's `api` container (token exchange). Locally that's `http://sso.localhost:8181`: browsers resolve `*.localhost` to loopback, and the compose network gives the shim that name as an alias. The system resolver on the host does not resolve it, so use a browser, not host-side `curl`, to test the login.
- Keycloak pins its issuer to `http://keycloak.localhost:8280`. The reserved
  `.localhost` name resolves in browsers, and the Compose network supplies the
  same alias and port to 9Router and Coder. The Plane shim redirects browsers to
  that public URL and uses `keycloak:8280` only for back-channel calls, so issuer
  validation does not need to be disabled.

### Limitations / before real use

- Dev setup: Keycloak runs `start-dev` (embedded H2, HTTP), demo passwords are in `users.ldif`, and everything is bound to loopback. For production use Keycloak with Postgres and TLS, a real LDAP/AD (edit `components` in the realm JSON), real hostnames for `KEYCLOAK_PUBLIC_URL` and the shim, and remove the demo users.
- Login only: no LDAP group-to-Plane-role sync, and signing out of Plane doesn't end the Keycloak session (so the next "Continue with SSO" may skip the password prompt).
- Edits to `realm-plane.json` only apply on a fresh Keycloak volume (`--import-realm` ignores existing realms). Use `make plane-sso-down` then remove the `simplecode-plane_keycloak_data` volume, or change settings in the Keycloak console.
- The shim is custom, security-relevant glue: keep it on loopback or behind your reverse proxy with TLS, and review it before exposing it.

### Other options

- **Gitea as the broker**: LDAP -> Gitea -> Plane's Gitea login. No custom code, but you run Gitea.
- **Plane paid editions** advertise native OIDC/SAML; LDAP is listed as Enterprise "coming soon". This repo does not patch Plane to add them.
