"""Gitea-API shim in front of Keycloak, so Plane CE's "Continue with Gitea" login
can authenticate against Keycloak (and, through Keycloak, LDAP).

Plane CE has no generic OIDC. Its Gitea provider calls fixed paths on GITEA_HOST:
  GET  /login/oauth/authorize   (browser redirect)
  POST /login/oauth/access_token
  GET  /api/v1/user
  GET  /api/v1/user/emails
This translates those to Keycloak's OIDC endpoints and reshapes the responses.
Stdlib only. Never logs tokens or request bodies.
"""

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# Browser-facing Keycloak URL (where the user's browser is sent to log in).
KC_PUBLIC = os.environ["KC_PUBLIC_URL"].rstrip("/")
# Container-network Keycloak URL (server-to-server token/userinfo calls).
KC_INTERNAL = os.environ["KC_INTERNAL_URL"].rstrip("/")
REALM = os.environ.get("KC_REALM", "plane")
SCOPE = os.environ.get("KC_SCOPE", "openid email profile")
PORT = int(os.environ.get("PORT", "8181"))

OIDC = f"/realms/{REALM}/protocol/openid-connect"


def _kc(path, data=None, headers=None):
    req = urllib.request.Request(KC_INTERNAL + OIDC + path, data=data, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):  # path only, never query strings (may hold codes)
        print("shim:", self.command, self.path.split("?")[0], flush=True)

    def _send(self, status, body, ctype="application/json", extra=None):
        if not isinstance(body, bytes):
            body = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _userinfo(self):
        auth = self.headers.get("Authorization", "")
        status, body = _kc("/userinfo", headers={"Authorization": auth, "Accept": "application/json"})
        if status != 200:
            self._send(401, {"message": "invalid token"})
            return None
        return json.loads(body)

    def do_GET(self):
        url = urllib.parse.urlparse(self.path)
        if url.path == "/login/oauth/authorize":
            q = dict(urllib.parse.parse_qsl(url.query))
            q["scope"] = SCOPE
            q["response_type"] = "code"
            self._send(302, b"", "text/plain", {"Location": f"{KC_PUBLIC}{OIDC}/auth?{urllib.parse.urlencode(q)}"})
        elif url.path == "/api/v1/user":
            u = self._userinfo()
            if u is not None:
                self._send(200, {
                    "id": u["sub"],
                    "login": u.get("preferred_username", u["sub"]),
                    "full_name": u.get("name", ""),
                    "email": u.get("email", ""),
                    "avatar_url": "",
                })
        elif url.path == "/api/v1/user/emails":
            u = self._userinfo()
            if u is not None:
                email = u.get("email")
                # Plane refuses unverified emails (account-takeover guard); pass Keycloak's flag through.
                self._send(200, [{"email": email, "primary": True, "verified": bool(u.get("email_verified"))}] if email else [])
        elif url.path == "/healthz":
            self._send(200, {"ok": True})
        else:
            self._send(404, {"message": "not found"})

    def do_POST(self):
        if self.path.split("?")[0] != "/login/oauth/access_token":
            return self._send(404, {"message": "not found"})
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        status, resp = _kc(
            "/token",
            data=body,
            headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
        )
        self._send(status, resp)


if __name__ == "__main__":
    print(f"shim: listening on :{PORT}, keycloak public={KC_PUBLIC} internal={KC_INTERNAL} realm={REALM}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
