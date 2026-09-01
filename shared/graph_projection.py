"""Read-only graph projections used by the production dashboard."""

from __future__ import annotations

from typing import Any


def build_scene_edges(scene: dict[str, Any]) -> list[dict[str, str]]:
    """Project a scene record into stable, dashboard-friendly graph edges."""
    scene_id = scene.get("scene_id")
    if not scene_id:
        return []

    edges: list[dict[str, str]] = []
    location_id = scene.get("location_id")
    if location_id:
        edges.append({"from": scene_id, "type": "USES_LOCATION", "to": location_id})

    for character_id in dict.fromkeys(scene.get("character_ids") or []):
        if character_id:
            edges.append({"from": scene_id, "type": "FEATURES_CHARACTER", "to": character_id})

    for prop_id in dict.fromkeys(scene.get("prop_ids") or []):
        if prop_id:
            edges.append({"from": scene_id, "type": "USES_PROP", "to": prop_id})

    return edges
