"""
cinemapilot/infra/grafana_oauth_bootstrap.py

One-time bootstrap script to authenticate with Grafana Cloud MCP server via OAuth 2.1 PKCE
and save the resulting token locally at ~/.cinemapilot/grafana_mcp_token.json.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import sys
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

_CINEMAPILOT_DIR = Path.home() / ".cinemapilot"
_TOKEN_FILE = _CINEMAPILOT_DIR / "grafana_mcp_token.json"


def generate_pkce_pair() -> tuple[str, str]:
    """Generate code_verifier and code_challenge (S256)."""
    code_verifier = secrets.token_urlsafe(64)
    hashed = hashlib.sha256(code_verifier.encode("ascii")).digest()
    code_challenge = base64.urlsafe_b64encode(hashed).decode("ascii").rstrip("=")
    return code_verifier, code_challenge


def register_dcr_client() -> dict:
    """Register dynamic client with Grafana MCP server."""
    reg_url = "https://mcp.grafana.com/mcp/oauth/register"
    payload = {
        "client_name": "CinemaPilot Risk Agent",
        "redirect_uris": ["http://localhost:8080/callback"],
    }
    resp = httpx.post(reg_url, json=payload, timeout=10)
    resp.raise_for_status()
    return resp.json()


def run_oauth_flow() -> dict:
    """Run browser PKCE OAuth flow and return token response dict."""
    print("\n[grafana_oauth_bootstrap] Step 1: Registering Dynamic Client via DCR...")
    client_data = register_dcr_client()
    client_id = client_data["client_id"]
    print(f"  + Registered Client ID: {client_id[:20]}...")

    code_verifier, code_challenge = generate_pkce_pair()

    redirect_uri = "http://localhost:8080/callback"
    state_str = secrets.token_urlsafe(16)

    auth_params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": "grafana:read grafana:write",
        "state": state_str,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    }
    auth_url = f"https://mcp.grafana.com/mcp/oauth/authorize?{urlencode(auth_params)}"

    print("\n" + "=" * 75)
    print("  ACTION REQUIRED: OPEN THE FOLLOWING URL IN YOUR BROWSER TO AUTHORIZE:")
    print("=" * 75)
    print(f"\n{auth_url}\n")
    print("=" * 75)

    auth_code_holder: dict[str, str] = {}

    class CallbackHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            parsed = urlparse(self.path)
            params = parse_qs(parsed.query)
            if "code" in params:
                auth_code_holder["code"] = params["code"][0]
                self.send_response(200)
                self.send_header("Content-type", "text/html")
                self.end_headers()
                self.wfile.write(
                    b"<h2>Authorization Successful!</h2><p>You can close this tab and return to the terminal.</p>"
                )
            else:
                self.send_response(400)
                self.end_headers()
                self.wfile.write(b"Authorization failed or code missing.")

        def log_message(self, format, *args):
            pass

    server = HTTPServer(("localhost", 8080), CallbackHandler)
    server.timeout = 2  # Poll interval
    deadline = time.time() + 600  # 10 minutes timeout
    print("\nWaiting for browser callback on http://localhost:8080/callback (timeout: 10 mins) ...")
    while "code" not in auth_code_holder and time.time() < deadline:
        server.handle_request()
    server.server_close()

    if "code" not in auth_code_holder:
        raise RuntimeError("Failed to capture authorization code from browser callback (timeout).")

    auth_code = auth_code_holder["code"]
    print("\n[grafana_oauth_bootstrap] Step 2: Exchanging code for Bearer Token...")

    token_url = "https://mcp.grafana.com/mcp/oauth/token"
    token_payload = {
        "grant_type": "authorization_code",
        "client_id": client_id,
        "code": auth_code,
        "redirect_uri": redirect_uri,
        "code_verifier": code_verifier,
    }
    token_resp = httpx.post(token_url, data=token_payload, timeout=10)
    token_resp.raise_for_status()
    token_data = token_resp.json()

    print("  + Token Exchange Successful!")
    return token_data


def save_token(token_data: dict) -> Path:
    """Save token dict to ~/.cinemapilot/grafana_mcp_token.json and GCP Secret Manager."""
    import time
    from shared.secret_client import persist_secret
    if "issued_at" not in token_data:
        token_data["issued_at"] = time.time()
    _CINEMAPILOT_DIR.mkdir(parents=True, exist_ok=True)
    with open(_TOKEN_FILE, "w", encoding="utf-8") as f:
        json.dump(token_data, f, indent=2)

    try:
        os.chmod(_TOKEN_FILE, 0o600)
    except Exception:
        pass

    try:
        persist_secret("GRAFANA_MCP_TOKEN", json.dumps(token_data))
    except Exception as exc:
        print(f"[grafana_oauth_bootstrap] Warning: Failed to persist token to Secret Manager: {exc}")

    print(f"\n[grafana_oauth_bootstrap] Token saved to {_TOKEN_FILE}")
    return _TOKEN_FILE


if __name__ == "__main__":
    try:
        tokens = run_oauth_flow()
        save_token(tokens)
        print("\nBootstrap complete! You can now run standalone risk agent tests.")
    except Exception as exc:
        print(f"\nBootstrap Failed: {exc}")
        sys.exit(1)
