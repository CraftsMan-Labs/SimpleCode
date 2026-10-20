#!/usr/bin/env bash
# Idempotently create Coder's OIDC client in the existing Plane Keycloak realm.
set -euo pipefail
repo_root="$(cd "$(dirname "$0")/../.." && pwd)"

set -a
# shellcheck disable=SC1091
. "$repo_root/docker/coder/.env"
# shellcheck disable=SC1091
. "$repo_root/docker/plane/.env"
set +a

: "${CODER_OIDC_CLIENT_SECRET:?run make coder-init}"
: "${KEYCLOAK_ADMIN_PASSWORD:?run make plane-sso-init}"

KC_URL="${KEYCLOAK_PUBLIC_URL:-http://keycloak.localhost:${KEYCLOAK_PORT:-8280}}" \
CODER_URL="${CODER_ACCESS_URL:-http://coder.localhost:7080}" \
python3 - <<'PY'
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

kc = os.environ["KC_URL"].rstrip("/")
coder = os.environ["CODER_URL"].rstrip("/")
admin_password = os.environ["KEYCLOAK_ADMIN_PASSWORD"]
secret = os.environ["CODER_OIDC_CLIENT_SECRET"]


def request(method, url, data=None, token=None, form=False):
    headers = {}
    body = None
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if data is not None:
        if form:
            body = urllib.parse.urlencode(data).encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        else:
            body = json.dumps(data).encode()
            headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=20) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.read()
    except (urllib.error.URLError, ConnectionResetError, TimeoutError, OSError):
        return 0, b""


token = None
last = ""
for _ in range(45):
    status, raw = request(
        "POST",
        f"{kc}/realms/master/protocol/openid-connect/token",
        {
            "client_id": "admin-cli",
            "username": "admin",
            "password": admin_password,
            "grant_type": "password",
        },
        form=True,
    )
    if status == 200:
        token = json.loads(raw)["access_token"]
        break
    last = f"{status} {raw[:160]!r}"
    time.sleep(2)
if not token:
    raise SystemExit(f"Keycloak admin login failed: {last}")

payload = {
    "clientId": "coder",
    "name": "Coder",
    "enabled": True,
    "protocol": "openid-connect",
    "publicClient": False,
    "clientAuthenticatorType": "client-secret",
    "secret": secret,
    "standardFlowEnabled": True,
    "directAccessGrantsEnabled": False,
    "redirectUris": [f"{coder}/api/v2/users/oidc/callback"],
    "webOrigins": [coder],
    "defaultClientScopes": ["web-origins", "profile", "email", "roles"],
}
query = urllib.parse.urlencode({"clientId": "coder"})
status, raw = request("GET", f"{kc}/admin/realms/plane/clients?{query}", token=token)
if status != 200:
    raise SystemExit(f"Coder client lookup failed: {status} {raw[:200]!r}")
found = json.loads(raw or b"[]")
if found:
    status, raw = request(
        "PUT", f"{kc}/admin/realms/plane/clients/{found[0]['id']}", payload, token=token
    )
    action = "updated"
else:
    status, raw = request("POST", f"{kc}/admin/realms/plane/clients", payload, token=token)
    action = "created"
if status not in (200, 201, 204):
    raise SystemExit(f"Coder client write failed: {status} {raw[:200]!r}")
print(f"{action} Keycloak client coder; callback={coder}/api/v2/users/oidc/callback")
PY
