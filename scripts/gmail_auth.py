"""One-off: get a read-only Gmail refresh token on your laptop, then copy it to the Sprite.

1. In Google Cloud, create an OAuth client of type "Desktop app" and enable the Gmail API.
2. uv run python scripts/gmail_auth.py CLIENT_ID CLIENT_SECRET   (opens a browser)
3. sprite file push gmail.json /home/sprite/.fineprint/gmail.json
   sprite exec -- chmod 600 /home/sprite/.fineprint/gmail.json && rm gmail.json

Scope is gmail.readonly; Fine Print reads only sender and subject headers.
"""

from __future__ import annotations

import base64
import hashlib
import http.server
import json
import os
import secrets
import socket
import sys
import urllib.parse
import webbrowser
from pathlib import Path

import httpx

SCOPE = "https://www.googleapis.com/auth/gmail.readonly"


def main(client_id: str, client_secret: str) -> None:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    redirect = f"http://127.0.0.1:{port}/"
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = secrets.token_urlsafe(16)
    url = "https://accounts.google.com/o/oauth2/v2/auth?" + urllib.parse.urlencode(
        {
            "client_id": client_id, "redirect_uri": redirect, "response_type": "code", "scope": SCOPE,
            "access_type": "offline", "prompt": "consent", "state": state,
            "code_challenge": challenge, "code_challenge_method": "S256",
        }
    )
    result: dict[str, str] = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            qs = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(self.path).query))
            result.update(qs)
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"Fine Print: you can close this tab.")

        def log_message(self, *args) -> None:
            pass

    print(f"Opening browser. If it doesn't open, visit:\n{url}")
    webbrowser.open(url)
    with http.server.HTTPServer(("127.0.0.1", port), Handler) as server:
        server.handle_request()

    if result.get("state") != state or "code" not in result:
        sys.exit(f"Authorisation failed: {result.get('error', 'state mismatch')}")
    resp = httpx.post(
        "https://oauth2.googleapis.com/token",
        data={
            "client_id": client_id, "client_secret": client_secret, "code": result["code"],
            "code_verifier": verifier, "grant_type": "authorization_code", "redirect_uri": redirect,
        },
    )
    resp.raise_for_status()
    token = resp.json()
    if "refresh_token" not in token:
        sys.exit("Google returned no refresh token; revoke the app's access and try again.")
    out = Path("gmail.json")
    out.write_text(json.dumps({"client_id": client_id, "client_secret": client_secret, "refresh_token": token["refresh_token"]}))
    os.chmod(out, 0o600)
    print(f"Wrote {out.resolve()} (mode 600). Push it to the Sprite, then delete it here.")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    main(sys.argv[1], sys.argv[2])
