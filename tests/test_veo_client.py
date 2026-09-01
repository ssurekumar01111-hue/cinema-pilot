from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from google.genai import types

from shared.veo_client import VEO_MODEL, generate_image_to_video


class VeoClientTests(unittest.TestCase):
    def test_image_to_video_uses_prompt_image_and_requested_config(self):
        operation = MagicMock()
        operation.done = True
        operation.response.generated_videos = [MagicMock(video=MagicMock())]
        client = MagicMock()
        client.models.generate_videos.return_value = operation

        client.files.download.return_value = b"video"

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            image_path = root / "panel.webp"
            image_path.write_bytes(b"storyboard-bytes")
            output_path = root / "clip.mp4"
            with patch("google.genai.Client", return_value=client):
                result = generate_image_to_video(image_path, "slow push in", output_path, api_key="test-key")

        self.assertEqual(result, output_path)
        kwargs = client.models.generate_videos.call_args.kwargs
        self.assertEqual(kwargs["model"], VEO_MODEL)
        self.assertEqual(kwargs["prompt"], "slow push in")
        self.assertIsInstance(kwargs["image"], types.Image)
        self.assertEqual(kwargs["image"].image_bytes, b"storyboard-bytes")
        self.assertEqual(kwargs["image"].mime_type, "image/webp")
        self.assertEqual(kwargs["config"].aspect_ratio, "16:9")
        self.assertEqual(kwargs["config"].resolution, "720p")
        self.assertEqual(kwargs["config"].duration_seconds, 8)
        self.assertEqual(kwargs["config"].person_generation, "allow_adult")

    def test_unknown_storyboard_extension_uses_png_mime_type(self):
        operation = MagicMock()
        operation.done = True
        operation.response.generated_videos = [MagicMock(video=MagicMock())]
        client = MagicMock()
        client.models.generate_videos.return_value = operation
        client.files.download.return_value = b"video"

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            image_path = root / "storyboard.asset"
            image_path.write_bytes(b"storyboard-bytes")
            with patch("google.genai.Client", return_value=client):
                generate_image_to_video(image_path, "slow push in", root / "clip.mp4", api_key="test-key")

        image = client.models.generate_videos.call_args.kwargs["image"]
        self.assertIsInstance(image, types.Image)
        self.assertEqual(image.image_bytes, b"storyboard-bytes")
        self.assertEqual(image.mime_type, "image/png")
