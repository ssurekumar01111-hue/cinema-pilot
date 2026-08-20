"""
scripts/test_secret_manager.py

Verifies that CinemaPilot can resolve all required credentials directly from
GCP Secret Manager when local environment variables are cleared.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# Add repo root to sys.path
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# Unset environment variables to force Secret Manager resolution
for key in ["GEMINI_API_KEY", "GRAFANA_CLOUD_OTLP_TOKEN", "GRAFANA_MCP_TOKEN"]:
    if key in os.environ:
        del os.environ[key]

print("=" * 75)
print("  CINEMAPILOT — GCP SECRET MANAGER INTEGRATION TEST")
print("=" * 75)

# 1. Test GEMINI_API_KEY from Secret Manager
from shared.secret_client import get_secret, get_secret_json, persist_secret

gemini_key = get_secret("GEMINI_API_KEY")
assert gemini_key and len(gemini_key) > 20, "Failed to fetch GEMINI_API_KEY from Secret Manager"
print(f"[OK] 1. GEMINI_API_KEY resolved from Secret Manager (prefix: {gemini_key[:8]}...)")

# 2. Test GRAFANA_CLOUD_OTLP_TOKEN from Secret Manager
otlp_token = get_secret("GRAFANA_CLOUD_OTLP_TOKEN")
assert otlp_token and len(otlp_token) > 20, "Failed to fetch GRAFANA_CLOUD_OTLP_TOKEN from Secret Manager"
print(f"[OK] 2. GRAFANA_CLOUD_OTLP_TOKEN resolved from Secret Manager (prefix: {otlp_token[:8]}...)")

# 3. Test GRAFANA_MCP_TOKEN from Secret Manager
mcp_token_data = get_secret_json("GRAFANA_MCP_TOKEN")
assert mcp_token_data and "access_token" in mcp_token_data, "Failed to fetch/parse GRAFANA_MCP_TOKEN from Secret Manager"
print(f"[OK] 3. GRAFANA_MCP_TOKEN JSON resolved from Secret Manager (client_id: {mcp_token_data.get('client_id', 'N/A')[:25]}...)")

# 4. Test telemetry._load_credentials
from shared.telemetry import _load_credentials
token, instance_id, endpoint = _load_credentials()
assert token is not None, "Telemetry failed to load OTLP token from Secret Manager"
print(f"[OK] 4. Telemetry initialized credentials from Secret Manager -> Instance {instance_id}")

# 5. Test grafana_client.get_valid_access_token
from shared.grafana_client import get_valid_access_token
access_token = get_valid_access_token()
assert access_token and len(access_token) > 50, "Failed to obtain valid Grafana access token via Secret Manager"
print(f"[OK] 5. Grafana Client successfully acquired access token ({len(access_token)} chars)")

print("=" * 75)
print("ALL SECRET MANAGER CHECKS PASSED SUCCESSFULLY")
print("=" * 75)
