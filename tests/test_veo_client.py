from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from PIL import Image

from shared.veo_client import VEO_MODEL, generate_image_to_video


class VeoClientTests(unittest.TestCase):
    def test_image_to_video_uses_prompt_image_and_requested_config(self):
        operation = MagicMock()
        operation.done = True
        operation.response.generated_videos = [MagicMock(video=MagicMock())]
        client = MagicMock()
        client.models.generate_videos.return_value = operation

        def download(*, file, destination):
            Path(destination).write_bytes(b"video")

        client.files.download.side_effect = download

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            image_path = root / "panel.png"
            Image.new("RGB", (32, 18), "black").save(image_path)
            output_path = root / "clip.mp4"
            with patch("google.genai.Client", return_value=client):
                result = generate_image_to_video(image_path, "slow push in", output_path, api_key="test-key")

        self.assertEqual(result, output_path)
        kwargs = client.models.generate_videos.call_args.kwargs
        self.assertEqual(kwargs["model"], VEO_MODEL)
        self.assertEqual(kwargs["prompt"], "slow push in")
        self.assertIn("image", kwargs)
        self.assertEqual(kwargs["config"].aspect_ratio, "16:9")
        self.assertEqual(kwargs["config"].resolution, "720p")
        self.assertEqual(kwargs["config"].duration_seconds, 8)
        self.assertEqual(kwargs["config"].person_generation, "allow_adult")
