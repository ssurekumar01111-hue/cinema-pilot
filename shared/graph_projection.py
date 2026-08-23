"""Read-only graph projections built from Production Graph records."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def build_scene_edges(scene: Mapping[str, Any]) -> list[dict[str, str]]:
    """Project a scene's stored references into normalized graph edges.

    This is intentionally a read-only projection: scenes remain the source of
    truth, while callers get a consistent shape for graph visualisation and
    explanation without a BigQuery schema migration.
    """
    scene_id = scene.get("scene_id")
    if not isinstance(scene_id, str) or not scene_id:
        raise ValueError("A scene edge projection requires a non-empty scene_id.")

    edges: list[dict[str, str]] = []

    def add_edge(relationship_type: str, target_entity_type: str, target_entity_id: Any) -> None:
        if not isinstance(target_entity_id, str) or not target_entity_id:
            return
        edges.append({
            "source_entity_type": "scene",
            "source_entity_id": scene_id,
            "relationship_type": relationship_type,
            "target_entity_type": target_entity_type,
            "target_entity_id": target_entity_id,
        })

    add_edge("USES_LOCATION", "location", scene.get("location_id"))

    for character_id in dict.fromkeys(scene.get("character_ids") or []):
        add_edge("FEATURES_CHARACTER", "character", character_id)

    for prop_id in dict.fromkeys(scene.get("prop_ids") or []):
        add_edge("USES_PROP", "prop", prop_id)

    return edges
