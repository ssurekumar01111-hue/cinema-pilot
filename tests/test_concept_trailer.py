from __future__ import annotations

from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from shared.concept_trailer import (
    TrailerRenderError,
    build_concept_trailer_plan,
    render_concept_trailer,
)


class ConceptTrailerPlanTests(unittest.TestCase):
    def test_plan_keeps_story_order_and_spreads_four_shots_across_the_story(self) -> None:
        assets = [
            {
                "scene_id": f"scene_{number:03d}",
                "storyboard_path": f"/tmp/{number}.jpg",
                "timeline_position": number,
                "emotional_tone": "tense" if number > 3 else "calm",
            }
            for number in range(1, 7)
        ]

        plan = build_concept_trailer_plan(assets, max_shots=4, seconds_per_shot=2)

        self.assertEqual([shot.scene_id for shot in plan], ["scene_001", "scene_003", "scene_004", "scene_006"])
        self.assertEqual(plan[0].duration_seconds, 2)
        self.assertEqual(plan[-1].emotional_intent, "tense")

    def test_plan_requires_a_storyboard_asset(self) -> None:
        with self.assertRaises(TrailerRenderError):
            build_concept_trailer_plan([{"scene_id": "scene_001"}])


class ConceptTrailerRenderTests(unittest.TestCase):
    def test_renderer_builds_a_local_ffmpeg_command_with_optional_music(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            frame_one = root / "one.jpg"
            frame_two = root / "two.jpg"
            music = root / "cue.mp3"
            output = root / "trailer.mp4"
            for path in (frame_one, frame_two, music):
                path.touch()

            plan = build_concept_trailer_plan(
                [
                    {"scene_id": "scene_001", "storyboard_path": frame_one, "timeline_position": 1},
                    {"scene_id": "scene_005", "storyboard_path": frame_two, "timeline_position": 5},
                ],
                seconds_per_shot=2,
            )

            def fake_run(command, **_kwargs):
                output.touch()
                self.assertIn("-stream_loop", command)
                self.assertIn("concat=n=2:v=1:a=0[video]", command[command.index("-filter_complex") + 1])
                return subprocess.CompletedProcess(command, 0, "", "")

            with patch("shared.concept_trailer.subprocess.run", side_effect=fake_run):
                result = render_concept_trailer(plan, output, music_path=music)

            self.assertEqual(result, output)

    def test_renderer_rejects_missing_storyboard(self) -> None:
        plan = build_concept_trailer_plan(
            [{"scene_id": "scene_001", "storyboard_path": "/missing/frame.jpg"}]
        )
        with self.assertRaises(TrailerRenderError):
            render_concept_trailer(plan, "/tmp/trailer.mp4")
