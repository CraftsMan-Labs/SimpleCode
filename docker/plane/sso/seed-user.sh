#!/usr/bin/env bash
# Create or update a Keycloak realm user at runtime.
# Credentials stay in the running Keycloak; nothing is written to git or Plane's DB.
# Usage (via make): EMAIL=you@example.com PASSWORD='...' [USERNAME=] [FIRST=] [LAST=]
set -euo pipefail
cd "$(dirname "$0")/.."

set -a; . ./.env; set +a
: "${EMAIL:?set EMAIL}"
: "${PASSWORD:?set PASSWORD}"
: "${KEYCLOAK_ADMIN_PASSWORD:?run make plane-sso-init first}"

KC_URL="${KEYCLOAK_PUBLIC_URL:-http://localhost:${KEYCLOAK_PORT:-8280}}" \
EMAIL="$EMAIL" PASSWORD="$PASSWORD" \
USERNAME="${USERNAME:-}" FIRST="${FIRST:-}" LAST="${LAST:-}" \
KEYCLOAK_ADMIN_PASSWORD="$KEYCLOAK_ADMIN_PASSWORD" \
python3 - <<'PY'
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

kc = os.environ["KC_URL"].rstrip("/")
email = os.environ["EMAIL"].strip()
password = os.environ["PASSWORD"]
username = (os.environ.get("USERNAME") or email).strip()
first = (os.environ.get("FIRST") or username.split("@", 1)[0]).strip()
last = (os.environ.get("LAST") or "").strip()
admin_pass = os.environ["KEYCLOAK_ADMIN_PASSWORD"]


def req(method, url, data=None, token=None, form=False):
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
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=20) as resp:
            return resp.status, resp.read(), {k.lower(): v for k, v in resp.headers.items()}
    except urllib.error.HTTPError as e:
        return e.code, e.read(), {k.lower(): v for k, v in e.headers.items()}


token = None
last_err = ""
for _ in range(30):
    status, raw, _ = req(
        "POST",
        f"{kc}/realms/master/protocol/openid-connect/token",
        {
            "client_id": "admin-cli",
            "username": "admin",
            "password": admin_pass,
            "grant_type": "password",
        },
        form=True,
    )
    if status == 200:
        token = json.loads(raw)["access_token"]
        break
    last_err = f"{status} {raw[:200]!r}"
    time.sleep(2)
if not token:
    raise SystemExit(f"keycloak admin login failed: {last_err}")


def find():
    q = urllib.parse.urlencode({"email": email, "exact": "true"})
    status, raw, _ = req("GET", f"{kc}/admin/realms/plane/users?{q}", token=token)
    if status != 200:
        raise SystemExit(f"user lookup failed: {status}")
    users = json.loads(raw or b"[]")
    if users:
        return users[0]["id"]
    q = urllib.parse.urlencode({"username": username, "exact": "true"})
    status, raw, _ = req("GET", f"{kc}/admin/realms/plane/users?{q}", token=token)
    if status != 200:
        raise SystemExit(f"user lookup failed: {status}")
    users = json.loads(raw or b"[]")
    return users[0]["id"] if users else None


profile = {
    "username": username,
    "email": email,
    "emailVerified": True,
    "enabled": True,
    "firstName": first,
    "lastName": last,
}

uid = find()
if uid:
    status, raw, _ = req("PUT", f"{kc}/admin/realms/plane/users/{uid}", profile, token=token)
    if status not in (204, 200):
        raise SystemExit(f"user update failed: {status} {raw[:200]!r}")
    action = "updated"
else:
    status, raw, headers = req("POST", f"{kc}/admin/realms/plane/users", profile, token=token)
    if status == 409:
        uid = find()
        action = "updated"
    elif status not in (201, 204):
        raise SystemExit(f"user create failed: {status} {raw[:200]!r}")
    else:
        loc = headers.get("location", "")
        uid = loc.rstrip("/").split("/")[-1] if loc else find()
        action = "created"
if not uid:
    raise SystemExit("could not resolve user id")

status, raw, _ = req(
    "PUT",
    f"{kc}/admin/realms/plane/users/{uid}/reset-password",
    {"type": "password", "value": password, "temporary": False},
    token=token,
)
if status not in (204, 200):
    raise SystemExit(f"password set failed: {status} {raw[:200]!r}")

print(f"{action} keycloak user {username} <{email}> (runtime only; not in git)")
PY
