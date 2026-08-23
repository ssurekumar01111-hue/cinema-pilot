from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from agents.trailer.agent import construct_veo_prompt, generate_trailer
from shared.concept_trailer import TrailerVideo


class TrailerAgentTests(unittest.TestCase):
    def test_prompt_keeps_storyboard_as_the_source_of_truth(self) -> None:
        prompt = construct_veo_prompt(
            {"camera_cues": ["slow push in"], "emotional_tone": "tense"},
            "",
            "tense",
        )
        self.assertIn("Preserve the shown characters", prompt)
        self.assertIn("slow push in", prompt)
        self.assertIn("No on-screen text", prompt)

    def test_agent_uses_placeholder_clips_when_key_is_missing(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "trailer.mp4"
            frame_one = root / "one.jpg"
            frame_two = root / "two.jpg"
            frame_one.touch()
            frame_two.touch()

            def fake_clip(shot, output_path, **_kwargs):
                path = Path(output_path)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.touch()
                return TrailerVideo(shot.scene_id, path, "local-placeholder", True)

            with patch("agents.trailer.agent.generate_veo_clip", side_effect=fake_clip), patch(
                "agents.trailer.agent.render_trailer_from_videos", return_value=output
            ) as assemble:
                result = generate_trailer(
                    [
                        {"scene_id": "scene_001", "storyboard_path": frame_one, "timeline_position": 1},
                        {"scene_id": "scene_005", "storyboard_path": frame_two, "timeline_position": 5},
                    ],
                    output,
                    gemini_api_key=None,
                )

            self.assertEqual(result["generation_mode"], "local-placeholder")
            self.assertEqual(result["placeholder_scene_ids"], ["scene_001", "scene_005"])
            self.assertEqual(assemble.call_count, 1)
