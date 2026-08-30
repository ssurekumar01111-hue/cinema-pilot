from __future__ import annotations

import sys
import types
import unittest
from datetime import datetime, timezone


# Change Detection normally imports the production telemetry runtime. Routing
# tests deliberately replace it before import so they remain fully offline.
telemetry = types.ModuleType("shared.telemetry")
telemetry.instrument_agent = lambda _name: lambda func: func
telemetry.record_affected_agents_count = lambda *_args, **_kwargs: None
sys.modules.setdefault("shared.telemetry", telemetry)

from agents.change_detection.agent import (  # noqa: E402
    _compute_triggered_agents,
    _describe_routing_decision,
    detect_changes,
)
from shared.graph_projection import build_scene_edges  # noqa: E402


class RoutingReasonTests(unittest.TestCase):
    def test_location_route_keeps_trailer_after_storyboard_and_music(self):
        route = _compute_triggered_agents("scene", {"location_id"})
        self.assertEqual(route, ["budget", "location", "storyboard", "schedule", "music", "risk", "trailer"])
        self.assertEqual(route.index("trailer") > route.index("storyboard"), True)
        self.assertEqual(route.index("trailer") > route.index("music"), True)

    def test_reason_is_returned_and_saved_in_routing_event(self):
        class FakeGraph:
            events = [{
                "event_id": "event-1", "actor_agent": "user_producer", "entity_type": "scene", "entity_id": "scene_005",
                "before_state": '{"location_id":"loc_old"}', "after_state": '{"location_id":"loc_new"}',
            }]
            logged = []

            def get_events_since(self, _since):
                return self.events

            def log_event(self, **kwargs):
                self.logged.append(kwargs)

        import agents.change_detection.agent as change_module
        original = change_module.ProductionGraphClient
        fake = FakeGraph()
        change_module.ProductionGraphClient = lambda: fake
        try:
            decisions = detect_changes(datetime.now(timezone.utc))
        finally:
            change_module.ProductionGraphClient = original

        self.assertIn("routed to", decisions[0]["reason"])
        self.assertEqual(fake.logged[0]["after_state"]["routing_reason"], decisions[0]["reason"])

    def test_reason_explains_no_route(self):
        reason = _describe_routing_decision("prop", ["name"], [])
        self.assertIn("no downstream", reason)


class SceneProjectionTests(unittest.TestCase):
    def test_scene_edges_are_read_only_and_deduplicated(self):
        scene = {
            "scene_id": "scene_005", "location_id": "loc_beach",
            "character_ids": ["char_a", "char_a", "char_b"], "prop_ids": ["prop_map", "prop_map"],
        }
        self.assertEqual(build_scene_edges(scene), [
            {"from": "scene_005", "type": "USES_LOCATION", "to": "loc_beach"},
            {"from": "scene_005", "type": "FEATURES_CHARACTER", "to": "char_a"},
            {"from": "scene_005", "type": "FEATURES_CHARACTER", "to": "char_b"},
            {"from": "scene_005", "type": "USES_PROP", "to": "prop_map"},
        ])
