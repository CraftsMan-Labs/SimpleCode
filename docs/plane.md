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
- Email/password is the only login enabled out of the box. Magic-link needs SMTP; Google/GitHub/GitLab/Gitea need OAuth apps (configure in god mode).
- Upgrading: bump `APP_RELEASE`, then `docker compose -f docker/plane/compose.yml pull && make plane-up`. The `migrator` container runs DB migrations.

## Is LDAP available in self-hosted Plane?

**No, not in Community Edition.** Verified three ways against the `v1.4.2` release:

1. **Running backend:** inside `makeplane/plane-backend:v1.4.2`, there is no LDAP Python package installed and zero `.py` files under `/code/plane` mention LDAP. The instance reports `edition: PLANE_COMMUNITY`.
2. **Source:** the only auth providers in `apps/api/plane/authentication/provider/` are email/password, magic code (email), and OAuth for Google, GitHub, GitLab and Gitea. The only "LDAP" hits in the repo are an i18n string (`auth.json`, a UI label with no backend) and pricing-page copy.
3. **Upstream's own pricing UI** (`apps/web/components/workspace/billing/comparison/plans.tsx`) lists LDAP as an **Enterprise-only, "coming soon"** feature. SAML and OIDC are likewise paid (the "One" plan and up), and are not in the CE image.

### Options if you need directory-based login

- **Put an IdP in front** and use a supported OAuth provider. E.g. run Authentik, Keycloak or Gitea against your LDAP server, then connect Plane through its **Gitea** (or GitLab/GitHub) OAuth integration in god mode. Keycloak/Authentik can mirror a Gitea-style OAuth flow only if you use Gitea itself as the broker, so the cleanest path is Gitea with LDAP authentication enabled, and Plane's Gitea login on top.
- **Buy Plane Enterprise** (self-hosted commercial license) when LDAP ships; it's currently labelled coming soon.
- **Fork/patch**: AGPL-3.0 allows adding an auth provider (the provider interface is small), but you then own the maintenance, and AGPL network-use clauses apply if you expose it to users.
