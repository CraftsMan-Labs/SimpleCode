#!/usr/bin/env bash
# Point Plane's "Gitea" login at the Keycloak shim. Idempotent.
# Equivalent to filling the Gitea form in http://localhost:8180/god-mode/authentication/gitea
set -euo pipefail
cd "$(dirname "$0")/.."

set -a; . ./.env; set +a
: "${KEYCLOAK_CLIENT_SECRET:?run make plane-sso-init first}"

PLANE_SSO_HOST="http://sso.localhost:${SSO_SHIM_PORT:-8181}" \
PLANE_SSO_SECRET="$KEYCLOAK_CLIENT_SECRET" \
docker compose -f compose.yml -f sso/compose.yml exec -T \
  -e PLANE_SSO_HOST -e PLANE_SSO_SECRET api python manage.py shell <<'PY'
import os
from plane.license.models import InstanceConfiguration
from plane.license.utils.encryption import encrypt_data
from plane.utils.cache import invalidate_cache_directly

wanted = {
    "IS_GITEA_ENABLED": ("1", False),
    "GITEA_HOST": (os.environ["PLANE_SSO_HOST"], False),
    "GITEA_CLIENT_ID": ("plane", False),
    "GITEA_CLIENT_SECRET": (os.environ["PLANE_SSO_SECRET"], True),
}
for key, (value, encrypted) in wanted.items():
    InstanceConfiguration.objects.update_or_create(
        key=key,
        defaults={"value": encrypt_data(value) if encrypted else value,
                  "category": "GITEA", "is_encrypted": encrypted},
    )
# /api/instances/ and /configurations/ responses are cached for 2h; god mode invalidates them on save.
invalidate_cache_directly(path="/api/instances/", user=False)
invalidate_cache_directly(path="/api/instances/configurations/", user=False)
print("configured gitea login ->", os.environ["PLANE_SSO_HOST"])
PY
