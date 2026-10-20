#!/usr/bin/env bash
# Wire 9Router dashboard login to the Plane Keycloak realm. Idempotent.
# Uses 9Router's native OIDC settings (PATCH /api/settings) — does not patch 9Router.
# The 9router Keycloak client is created via the admin API so an existing realm volume still works.
set -euo pipefail
cd "$(dirname "$0")/.."

set -a; . ./.env; set +a
: "${KEYCLOAK_ADMIN_PASSWORD:?run make plane-sso-init first}"

if ! grep -q '^NINEROUTER_OIDC_CLIENT_SECRET=' .env; then
  printf 'NINEROUTER_OIDC_CLIENT_SECRET=%s\n' "$(openssl rand -hex 24)" >> .env
  echo "appended NINEROUTER_OIDC_CLIENT_SECRET to docker/plane/.env"
  set -a; . ./.env; set +a
fi

STACK_ENV="$(cd .. && pwd)/.env"
NINEROUTER_PASSWORD="123456"
if [ -f "$STACK_ENV" ]; then
  # shellcheck disable=SC1090
  NINEROUTER_PASSWORD="$(python3 -c '
import pathlib,re
p=pathlib.Path("'"$STACK_ENV"'")
m=re.search(r"^INITIAL_PASSWORD=(.*)$", p.read_text(), re.M)
print((m.group(1).strip() if m else "123456") or "123456")
')"
fi

KC_URL="${KEYCLOAK_PUBLIC_URL:-http://keycloak.localhost:${KEYCLOAK_PORT:-8280}}" \
NINEROUTER_URL="${NINEROUTER_PUBLIC_URL:-http://localhost:20128}" \
NINEROUTER_OIDC_ISSUER="${NINEROUTER_OIDC_ISSUER:-http://keycloak.localhost:8280/realms/plane}" \
NINEROUTER_OIDC_CLIENT_SECRET="$NINEROUTER_OIDC_CLIENT_SECRET" \
NINEROUTER_PASSWORD="$NINEROUTER_PASSWORD" \
KEYCLOAK_ADMIN_PASSWORD="$KEYCLOAK_ADMIN_PASSWORD" \
python3 - <<'PY'
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

kc = os.environ["KC_URL"].rstrip("/")
nr = os.environ["NINEROUTER_URL"].rstrip("/")
issuer = os.environ["NINEROUTER_OIDC_ISSUER"].rstrip("/")
secret = os.environ["NINEROUTER_OIDC_CLIENT_SECRET"]
nr_pass = os.environ["NINEROUTER_PASSWORD"]
admin_pass = os.environ["KEYCLOAK_ADMIN_PASSWORD"]
client_id = "9router"


def req(method, url, data=None, token=None, form=False, cookie=None):
    headers = {}
    body = None
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if cookie:
        headers["Cookie"] = cookie
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
            raw = resp.read()
            hdrs = {k.lower(): v for k, v in resp.headers.items()}
            set_cookie = resp.headers.get("Set-Cookie") or resp.headers.get("set-cookie")
            return resp.status, raw, hdrs, set_cookie
    except urllib.error.HTTPError as e:
        return e.code, e.read(), {k.lower(): v for k, v in e.headers.items()}, None
    except (urllib.error.URLError, ConnectionResetError, TimeoutError, OSError) as e:
        return 0, str(e).encode(), {}, None


def wait(url, ok=lambda s, b: s == 200):
    last = ""
    for _ in range(45):
        status, raw, _, _ = req("GET", url)
        if ok(status, raw):
            return
        last = f"{status} {raw[:160]!r}"
        time.sleep(2)
    raise SystemExit(f"timeout waiting for {url}: {last}")


wait(f"{kc}/realms/plane/.well-known/openid-configuration")
wait(f"{nr}/api/auth/status")

token = None
last_err = ""
for _ in range(15):
    status, raw, _, _ = req(
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

redirects = [
    f"{nr}/api/auth/oidc/callback",
    "http://127.0.0.1:20128/api/auth/oidc/callback",
]
origins = [nr, "http://127.0.0.1:20128"]
payload = {
    "clientId": client_id,
    "name": "9Router",
    "enabled": True,
    "protocol": "openid-connect",
    "publicClient": False,
    "clientAuthenticatorType": "client-secret",
    "secret": secret,
    "standardFlowEnabled": True,
    "directAccessGrantsEnabled": False,
    "redirectUris": redirects,
    "webOrigins": origins,
    "defaultClientScopes": ["web-origins", "profile", "email", "roles"],
}

q = urllib.parse.urlencode({"clientId": client_id})
status, raw, _, _ = req("GET", f"{kc}/admin/realms/plane/clients?{q}", token=token)
if status != 200:
    raise SystemExit(f"client lookup failed: {status} {raw[:200]!r}")
found = json.loads(raw or b"[]")
if found:
    cid = found[0]["id"]
    status, raw, _, _ = req(
        "PUT", f"{kc}/admin/realms/plane/clients/{cid}", payload, token=token
    )
    if status not in (204, 200):
        raise SystemExit(f"client update failed: {status} {raw[:200]!r}")
    action = "updated"
else:
    status, raw, _, _ = req(
        "POST", f"{kc}/admin/realms/plane/clients", payload, token=token
    )
    if status not in (201, 204):
        raise SystemExit(f"client create failed: {status} {raw[:200]!r}")
    action = "created"

status, raw, _, set_cookie = req(
    "POST",
    f"{nr}/api/auth/login",
    {"password": nr_pass},
)
if status != 200:
    raise SystemExit(
        f"9router login failed ({status}). Set INITIAL_PASSWORD in docker/.env to the dashboard password."
    )
cookie = None
if set_cookie:
    cookie = set_cookie.split(";", 1)[0]
# urllib may not expose set-cookie on 200; fall back to auth_token from a second login via CookieJar
if not cookie:
    import http.cookiejar
    from urllib.request import HTTPCookieProcessor, build_opener

    jar = http.cookiejar.CookieJar()
    opener = build_opener(HTTPCookieProcessor(jar))
    body = json.dumps({"password": nr_pass}).encode()
    r = urllib.request.Request(
        f"{nr}/api/auth/login",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    opener.open(r, timeout=20)
    cookies = [f"{c.name}={c.value}" for c in jar]
    cookie = "; ".join(cookies)
if not cookie:
    raise SystemExit("9router login did not return a session cookie")

status, raw, _, _ = req(
    "PATCH",
    f"{nr}/api/settings",
    {
        "authMode": "both",
        "ssoType": "oidc",
        "oidcIssuerUrl": issuer,
        "oidcClientId": client_id,
        "oidcClientSecret": secret,
        "oidcScopes": "openid profile email",
        "oidcLoginLabel": "Sign in with SSO",
    },
    cookie=cookie,
)
if status not in (200, 204):
    raise SystemExit(f"9router settings patch failed: {status} {raw[:200]!r}")

status, raw, _, _ = req("GET", f"{nr}/api/auth/status")
info = json.loads(raw) if raw else {}
if not info.get("oidcConfigured"):
    raise SystemExit(f"9router OIDC still not configured: {info}")

print(f"{action} keycloak client {client_id}; 9router OIDC -> {issuer}")
PY
