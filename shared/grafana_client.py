"""
shared/grafana_client.py

Shared Grafana Cloud MCP helper for CinemaPilot agents (Location Agent, Risk Agent, etc.).
Loads cached OAuth 2.1 Bearer token from ~/.cinemapilot/grafana_mcp_token.json.
"""

from __future__ import annotations

import base64
import json
import os
from pathlib import Path
import time
from typing import Any, Dict, List, Optional

import httpx
from google.adk.tools.mcp_tool import (
    McpToolset,
    StreamableHTTPConnectionParams,
)

_TOKEN_FILE = Path.home() / ".cinemapilot" / "grafana_mcp_token.json"
_TOKEN_ENDPOINT = "https://mcp.grafana.com/mcp/oauth/token"
_GRAFANA_URL = "https://daringhamster1557.grafana.net"


def _decode_jwt_payload(jwt_str: str) -> Dict[str, Any]:
    """Extract decoded JSON payload from an unverified JWT string."""
    try:
        parts = jwt_str.split(".")
        if len(parts) >= 2:
            payload_b64 = parts[1]
            payload_b64 += "=" * ((4 - len(payload_b64) % 4) % 4)
            return json.loads(base64.urlsafe_b64decode(payload_b64.encode("ascii")))
    except Exception:
        pass
    return {}


def refresh_grafana_token(token_data: Dict[str, Any]) -> Dict[str, Any]:
    """
    Use cached refresh_token to obtain a new access_token (and updated refresh_token).
    Saves the refreshed tokens and issued_at timestamp to ~/.cinemapilot/grafana_mcp_token.json.

    Raises:
        RuntimeError: If refresh fails (e.g. refresh_token expired), prompting re-authorization.
    """
    refresh_token = token_data.get("refresh_token")
    if not refresh_token:
        raise RuntimeError(
            f"Grafana token file ({_TOKEN_FILE}) missing 'refresh_token'. "
            "Please run `python infra/grafana_oauth_bootstrap.py` to authenticate."
        )

    # Resolve client_id from token dict or embedded inside refresh_token JWT
    client_id = token_data.get("client_id")
    if not client_id:
        rt_payload = _decode_jwt_payload(refresh_token)
        client_id = rt_payload.get("client_id")

    if not client_id:
        raise RuntimeError(
            "Could not determine client_id for Grafana token refresh. "
            "Please re-run `python infra/grafana_oauth_bootstrap.py`."
        )

    print("[grafana_client] Refreshing expired Grafana OAuth 2.1 access token...")
    try:
        resp = httpx.post(
            _TOKEN_ENDPOINT,
            data={
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": client_id,
            },
            timeout=15,
        )
        resp.raise_for_status()
        new_token_data = resp.json()
    except Exception as exc:
        raise RuntimeError(
            f"Grafana OAuth token refresh failed ({exc}). The refresh token may have expired. "
            "Please re-run `python infra/grafana_oauth_bootstrap.py` for a fresh browser authorization."
        ) from exc

    # Preserve client_id and record issued_at timestamp
    new_token_data["client_id"] = client_id
    new_token_data["issued_at"] = time.time()
    if "refresh_token" not in new_token_data:
        new_token_data["refresh_token"] = refresh_token

    _TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(_TOKEN_FILE, "w", encoding="utf-8") as f:
        json.dump(new_token_data, f, indent=2)

    try:
        os.chmod(_TOKEN_FILE, 0o600)
    except Exception:
        pass

    print(f"[grafana_client] Successfully refreshed token and saved to {_TOKEN_FILE}")
    return new_token_data


def get_valid_access_token() -> str:
    """
    Load the cached Grafana MCP access token, refreshing proactively if expired or near expiry.

    Returns:
        Valid OAuth 2.1 Bearer access token string.
    """
    if not _TOKEN_FILE.exists():
        raise RuntimeError(
            f"Grafana MCP token file not found at {_TOKEN_FILE}. "
            "Please run `python infra/grafana_oauth_bootstrap.py` first to authenticate."
        )

    try:
        token_data = json.loads(_TOKEN_FILE.read_text(encoding="utf-8"))
    except Exception as exc:
        raise RuntimeError(
            f"Failed to read Grafana MCP token from {_TOKEN_FILE}: {exc}. "
            "Please re-run `python infra/grafana_oauth_bootstrap.py`."
        ) from exc

    access_token = token_data.get("access_token")
    if not access_token:
        token_data = refresh_grafana_token(token_data)
        return token_data["access_token"]

    # Check expiration via JWT exp claim or issued_at + expires_in
    now = time.time()
    exp_time = None

    at_payload = _decode_jwt_payload(access_token)
    if "exp" in at_payload:
        exp_time = float(at_payload["exp"])
    elif "issued_at" in token_data and "expires_in" in token_data:
        exp_time = float(token_data["issued_at"]) + float(token_data["expires_in"])

    # If expired or expiring in less than 60 seconds, refresh proactively
    if exp_time is not None and now >= (exp_time - 60):
        print(f"[grafana_client] Cached access token expired (exp: {exp_time}, now: {now}).")
        token_data = refresh_grafana_token(token_data)
        access_token = token_data["access_token"]

    return access_token


def get_grafana_toolset(tool_filter: Optional[List[str]] = None) -> McpToolset:
    """
    Constructs an ADK McpToolset connected to the Grafana Cloud MCP server.
    Loads and automatically refreshes cached OAuth 2.1 Bearer token from ~/.cinemapilot/grafana_mcp_token.json.

    Args:
        tool_filter: Optional list of tool names to filter (e.g. ['list_datasources', 'query_loki_logs']).
    """
    access_token = get_valid_access_token()

    headers = {
        "Authorization": f"Bearer {access_token}",
        "X-Grafana-URL": _GRAFANA_URL,
    }

    connection_params = StreamableHTTPConnectionParams(
        url="https://mcp.grafana.com/mcp",
        headers=headers,
    )

    return McpToolset(
        connection_params=connection_params,
        tool_filter=tool_filter,
    )
