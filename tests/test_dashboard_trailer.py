from __future__ import annotations

import unittest
from fastapi.testclient import TestClient

import dashboard.backend as backend


class DashboardGraph:
    def get_scene(self, scene_id):
        return {"scene_id": scene_id, "location_id": "loc_beach", "character_ids": [], "prop_ids": []} if scene_id == "scene_005" else None

    def get_trailer(self):
        return {"trailer_id": "tr_project", "status": "ready", "generation_mode": "mixed", "gs_uri": "gs://assets/trailer/production/latest.mp4"}

    def get_location(self, _location_id): return None
    def get_budget_lines_for_entity(self, _scene_id): return []
    def get_schedule_blocks_for_scene(self, _scene_id): return []
    def get_risk_flags_for_entity(self, _entity_id): return []
    def get_storyboard(self, _storyboard_id): return None
    def get_music_cue(self, _music_id): return None
    def get_director_note(self, _note_id): return None
    def get_producer_overview(self, _overview_id): return None
    def get_explanation(self, _explanation_id): return None
    def get_scene_edges(self, _scene_id): return [{"from": "scene_005", "type": "USES_LOCATION", "to": "loc_beach"}]


class DashboardStorage:
    def download_asset_bytes(self, asset_type, entity_id, filename):
        return b"mp4", "video/mp4"


class DashboardTrailerTests(unittest.TestCase):
    def setUp(self):
        self.original_graph = backend._graph_client
        self.original_storage = backend._storage_client
        self.original_generator = backend.generate_production_trailer
        backend._graph_client = DashboardGraph()
        backend._storage_client = DashboardStorage()
        self.client = TestClient(backend.app)

    def tearDown(self):
        backend._graph_client = self.original_graph
        backend._storage_client = self.original_storage
        backend.generate_production_trailer = self.original_generator

    def test_get_trailer_and_media_proxy(self):
        trailer = self.client.get("/api/trailer")
        self.assertEqual(trailer.status_code, 200)
        self.assertEqual(trailer.json()["media_url"], "/api/media/trailer/production/latest.mp4")
        media = self.client.get("/api/media/trailer/production/latest.mp4")
        self.assertEqual(media.status_code, 200)
        self.assertEqual(media.headers["content-type"], "video/mp4")

    def test_scene_detail_exposes_read_only_edges(self):
        response = self.client.get("/api/scene/scene_005")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["scene_edges"][0]["type"], "USES_LOCATION")

    def test_generate_uses_selected_scene_and_returns_metadata(self):
        calls = []

        def fake_generator(scene_id, **_kwargs):
            calls.append(scene_id)
            return {"trailer_id": "tr_project", "status": "ready", "generation_mode": "local-placeholder", "gs_uri": "gs://assets/trailer/production/next.mp4"}

        backend.generate_production_trailer = fake_generator
        response = self.client.post("/api/trailer/generate", json={"source_scene_id": "scene_005"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(calls, ["scene_005"])
        self.assertEqual(response.json()["generation_mode"], "local-placeholder")
