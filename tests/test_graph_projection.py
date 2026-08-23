"""Offline tests for the read-only Production Graph edge projection."""

from __future__ import annotations

import unittest

from shared.graph_projection import build_scene_edges


class SceneEdgeProjectionTests(unittest.TestCase):
    def test_projects_location_characters_and_props_in_stable_order(self) -> None:
        edges = build_scene_edges({
            "scene_id": "scene_005",
            "location_id": "loc_sunset_beach",
            "character_ids": ["char_maria", "char_daniel", "char_maria"],
            "prop_ids": ["prop_camera"],
        })

        self.assertEqual(
            edges,
            [
                {
                    "source_entity_type": "scene",
                    "source_entity_id": "scene_005",
                    "relationship_type": "USES_LOCATION",
                    "target_entity_type": "location",
                    "target_entity_id": "loc_sunset_beach",
                },
                {
                    "source_entity_type": "scene",
                    "source_entity_id": "scene_005",
                    "relationship_type": "FEATURES_CHARACTER",
                    "target_entity_type": "character",
                    "target_entity_id": "char_maria",
                },
                {
                    "source_entity_type": "scene",
                    "source_entity_id": "scene_005",
                    "relationship_type": "FEATURES_CHARACTER",
                    "target_entity_type": "character",
                    "target_entity_id": "char_daniel",
                },
                {
                    "source_entity_type": "scene",
                    "source_entity_id": "scene_005",
                    "relationship_type": "USES_PROP",
                    "target_entity_type": "prop",
                    "target_entity_id": "prop_camera",
                },
            ],
        )

    def test_ignores_empty_relationship_ids(self) -> None:
        edges = build_scene_edges({
            "scene_id": "scene_006",
            "location_id": None,
            "character_ids": ["", None],
            "prop_ids": [],
        })

        self.assertEqual(edges, [])

    def test_requires_scene_id(self) -> None:
        with self.assertRaisesRegex(ValueError, "scene_id"):
            build_scene_edges({"location_id": "loc_sunset_beach"})


if __name__ == "__main__":
    unittest.main()
