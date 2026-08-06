"""
shared/grafana_client.py

Shared Grafana Cloud MCP helper for CinemaPilot agents (Location Agent, Risk Agent, etc.).
Loads cached OAuth 2.1 Bearer token from ~/.cinemapilot/grafana_mcp_token.json.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import List, Optional

from google.adk.tools.mcp_tool import (
    McpToolset,
    StreamableHTTPConnectionParams,
)

_TOKEN_FILE = Path.home() / ".cinemapilot" / "grafana_mcp_token.json"


def get_grafana_toolset(tool_filter: Optional[List[str]] = None) -> McpToolset:
    """
    Constructs an ADK McpToolset connected to the Grafana Cloud MCP server.
    Loads cached OAuth 2.1 Bearer token from ~/.cinemapilot/grafana_mcp_token.json.

    Args:
        tool_filter: Optional list of tool names to filter (e.g. ['list_datasources', 'query_loki_logs']).
    """
    if not _TOKEN_FILE.exists():
        raise RuntimeError(
            f"Grafana MCP token file not found at {_TOKEN_FILE}. "
            "Please run `python infra/grafana_oauth_bootstrap.py` first to authenticate."
        )

    try:
        token_data = json.loads(_TOKEN_FILE.read_text(encoding="utf-8"))
        access_token = token_data.get("access_token")
        if not access_token:
            raise ValueError("Token file missing 'access_token' field.")
    except Exception as exc:
        raise RuntimeError(
            f"Failed to read Grafana MCP token from {_TOKEN_FILE}: {exc}. "
            "Please re-run `python infra/grafana_oauth_bootstrap.py`."
        ) from exc

    headers = {
        "Authorization": f"Bearer {access_token}",
        "X-Grafana-URL": "https://daringhamster1557.grafana.net",
    }

    connection_params = StreamableHTTPConnectionParams(
        url="https://mcp.grafana.com/mcp",
        headers=headers,
    )

    return McpToolset(
        connection_params=connection_params,
        tool_filter=tool_filter,
    )
