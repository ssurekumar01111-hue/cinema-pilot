"""
shared/secret_client.py

Centralized Secret Manager client for CinemaPilot.
Retrieves credentials from:
1. Environment variables (e.g., injected by Cloud Run or local dev)
2. GCP Secret Manager (projects/{GCP_PROJECT}/secrets/{SECRET_NAME}/versions/latest)
3. Local file cache (~/.cinemapilot/...) as local development fallback.

Also supports persisting refreshed tokens back to Secret Manager at runtime,
solving the Cloud Run ephemeral container restart challenge.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger("cinemapilot.secrets")

DEFAULT_PROJECT = os.environ.get("GCP_PROJECT", "cinemapilot-2026")


def get_secret(
    secret_id: str,
    default: Optional[str] = None,
    project_id: Optional[str] = None,
) -> Optional[str]:
    """
    Retrieve a secret string.
    Resolution order:
      1. os.environ[secret_id]
      2. GCP Secret Manager (projects/{project_id}/secrets/{secret_id}/versions/latest)
      3. default parameter
    """
    # 1. Environment variable
    env_val = os.environ.get(secret_id)
    if env_val and env_val.strip():
        return env_val.strip()

    # 2. GCP Secret Manager
    proj = project_id or os.environ.get("GCP_PROJECT", DEFAULT_PROJECT)
    try:
        from google.cloud import secretmanager

        client = secretmanager.SecretManagerServiceClient()
        name = f"projects/{proj}/secrets/{secret_id}/versions/latest"
        response = client.access_secret_version(request={"name": name})
        secret_value = response.payload.data.decode("UTF-8").strip()
        if secret_value:
            return secret_value
    except Exception as exc:
        logger.debug(
            "Secret Manager lookup for '%s' in project '%s' bypassed or failed: %s",
            secret_id,
            proj,
            exc,
        )

    return default


def get_secret_json(
    secret_id: str,
    default: Optional[Dict[str, Any]] = None,
    project_id: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """
    Retrieve a secret payload parsed as a JSON dict.
    Resolution order:
      1. os.environ[secret_id] (if JSON string)
      2. GCP Secret Manager
      3. default dict
    """
    raw = get_secret(secret_id, default=None, project_id=project_id)
    if raw:
        try:
            return json.loads(raw)
        except Exception as exc:
            logger.warning("Failed to parse secret '%s' as JSON: %s", secret_id, exc)

    return default


def persist_secret(
    secret_id: str,
    payload_str: str,
    project_id: Optional[str] = None,
) -> bool:
    """
    Add a new version of a secret to GCP Secret Manager.
    Used for persisting updated OAuth refresh tokens across Cloud Run container lifecycles.
    """
    proj = project_id or os.environ.get("GCP_PROJECT", DEFAULT_PROJECT)
    try:
        from google.cloud import secretmanager

        client = secretmanager.SecretManagerServiceClient()
        parent = f"projects/{proj}/secrets/{secret_id}"
        client.add_secret_version(
            request={
                "parent": parent,
                "payload": {"data": payload_str.encode("UTF-8")},
            }
        )
        logger.info(
            "Successfully persisted updated secret version for '%s' in GCP Secret Manager (project: %s)",
            secret_id,
            proj,
        )
        print(f"[secret_client] Persisted refreshed '{secret_id}' version to GCP Secret Manager (project: {proj})")
        return True
    except Exception as exc:
        logger.warning(
            "Could not persist secret '%s' to Secret Manager (project: %s): %s",
            secret_id,
            proj,
            exc,
        )
        print(f"[secret_client] Warning: Failed to persist '{secret_id}' to Secret Manager: {exc}")
        return False
