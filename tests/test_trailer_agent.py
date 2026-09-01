from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
import subprocess

from agents.trailer.agent import _caption_words, generate_production_trailer
from shared.cascade_dispatch import CascadeDispatchError, dispatch_routing_decision
from shared.concept_trailer import TrailerShot, render_trailer, select_timeline_shots


class FakeGraph:
    def __init__(self):
        self.states = []
        self.events = []
        self.latest = None
        self.scenes = [
            {"scene_id": f"scene_{number:03d}", "timeline_position": number, "emotional_tone": "tense", "camera_cues": ["wide"]}
            for number in range(1, 6)
        ]

    def get_trailer(self, _trailer_id="tr_project"):
        return self.latest

    def list_scenes(self):
        return self.scenes

    def get_storyboard(self, storyboard_id):
        scene_id = storyboard_id.removeprefix("sb_")
        return {"storyboard_id": storyboard_id, "scene_id": scene_id, "gs_uri": f"gs://assets/storyboard/{scene_id}.png"}

    def get_voice_preview(self, voice_preview_id):
        if voice_preview_id == "vp_scene_001":
            return {"dialogue_lines": '[{"line":"Run before the tide turns now"}]'}
        return None

    def get_director_note(self, _director_note_id):
        return {"camera_plan": "slow lateral move"}

    def get_music_cue(self, music_cue_id):
        if music_cue_id == "mc_scene_003":
            return {"status": "completed", "gs_uri": "gs://assets/music/scene_003.mp3"}
        return None

    def upsert_trailer(self, record):
        self.latest = dict(record)
        self.states.append(dict(record))

    def log_event(self, **event):
        self.events.append(event)


class FakeStorage:
    def __init__(self):
        self.uploads = []

    def download_gs_uri(self, uri):
        if uri.endswith(".mp3"):
            return b"music", "audio/mpeg"
        return b"storyboard", "image/png"

    def upload_asset(self, *args):
        self.uploads.append(args)
        return "gs://assets/trailer/production/trailer.mp4"


class TrailerAgentTests(unittest.TestCase):
    def test_selects_first_middle_last_and_marks_mixed_fallback(self):
        graph = FakeGraph()
        storage = FakeStorage()
        rendered = []

        def veo(image_path, _prompt, output_path, *, api_key):
            if "scene_003" in image_path.name:
                raise RuntimeError("quota test failure")
            Path(output_path).write_bytes(b"veo")
            return Path(output_path)

        def renderer(shots, output_path, music_path=None):
            rendered.extend(shots)
            self.assertIsNotNone(music_path)
            for shot in shots:
                shot.caption_rendered = bool(shot.caption_words)
            Path(output_path).write_bytes(b"mp4")
            return Path(output_path)

        record = generate_production_trailer(
            "scene_003", graph_client=graph, storage_client=storage,
            secret_getter=lambda _name: "test-key", veo_generator=veo, trailer_renderer=renderer,
        )

        self.assertEqual(record["source_scene_ids"], ["scene_001", "scene_003", "scene_005"])
        self.assertEqual(record["generation_mode"], "mixed")
        self.assertEqual(record["fallback_scene_ids"], ["scene_003"])
        self.assertEqual(record["captioned_scene_ids"], ["scene_001"])
        self.assertEqual(record["music_source_scene_id"], "scene_003")
        self.assertEqual([shot.media_kind for shot in rendered], ["video", "image", "video"])
        self.assertEqual(graph.states[0]["status"], "generating")
        self.assertEqual(graph.states[-1]["status"], "ready")
        self.assertEqual(graph.events[-1]["actor_agent"], "trailer_agent")

    def test_missing_key_uses_real_storyboard_fallback(self):
        graph = FakeGraph()
        storage = FakeStorage()

        def renderer(_shots, output_path, music_path=None):
            for shot in _shots:
                shot.caption_rendered = bool(shot.caption_words)
            Path(output_path).write_bytes(b"mp4")
            return Path(output_path)

        record = generate_production_trailer(
            "scene_003", graph_client=graph, storage_client=storage,
            secret_getter=lambda _name: None, trailer_renderer=renderer,
        )
        self.assertEqual(record["generation_mode"], "local-placeholder")
        self.assertEqual(record["fallback_scene_ids"], ["scene_001", "scene_003", "scene_005"])
        self.assertIn("not configured", record["error"])

    def test_caption_words_are_optional_and_limited(self):
        self.assertEqual(_caption_words(None), ())
        self.assertEqual(_caption_words({"dialogue_lines": [{"line": "one two three four five six seven eight nine"}]}),
                         ("one", "two", "three", "four", "five", "six", "seven", "eight"))

    def test_dispatcher_preserves_automatic_route_order_and_rejects_unknown_agents(self):
        calls = []
        automatic_agents = ["budget", "location", "storyboard", "schedule", "music", "risk"]
        result = dispatch_routing_decision(
            {"triggered_agents": automatic_agents},
            {name: lambda name=name: calls.append(name) for name in automatic_agents},
        )
        self.assertEqual(calls, automatic_agents)
        self.assertEqual([name for name, _ in result], calls)
        with self.assertRaises(CascadeDispatchError):
            dispatch_routing_decision({"triggered_agents": ["music", "missing"]}, {"music": lambda: None})

    def test_renderer_strips_clip_audio_and_places_one_word_per_second(self):
        commands = []

        def runner(command, **_kwargs):
            commands.append(command)

        with tempfile.TemporaryDirectory() as raw:
            work_dir = Path(raw)
            image = work_dir / "panel.png"
            image.write_bytes(b"image")
            output = work_dir / "out.mp4"
            render_trailer([TrailerShot("scene_001", image, "image", ("Hold", "fast"))], output, runner=runner)

            clip_command = commands[0]
            self.assertIn("-an", clip_command)
            filter_value = clip_command[clip_command.index("-vf") + 1]
            self.assertIn("between(t,0,1)", filter_value)
            self.assertIn("between(t,1,2)", filter_value)

    def test_renderer_keeps_video_when_local_ffmpeg_has_no_drawtext(self):
        calls = []

        def runner(command, **_kwargs):
            calls.append(command)
            if len(calls) == 1:
                raise subprocess.CalledProcessError(8, command, stderr="No such filter: 'drawtext'")

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            image = root / "panel.png"
            image.write_bytes(b"image")
            shot = TrailerShot("scene_001", image, "image", ("Hold",))
            render_trailer([shot], root / "out.mp4", runner=runner)

        self.assertEqual(len(calls), 3)  # failed captioned clip / retry / concat
        self.assertFalse(shot.caption_rendered)


class TrailerSelectionTests(unittest.TestCase):
    def test_timeline_selection_is_first_middle_last(self):
        shots = [{"id": index} for index in range(5)]
        self.assertEqual(select_timeline_shots(shots), [{"id": 0}, {"id": 2}, {"id": 4}])
