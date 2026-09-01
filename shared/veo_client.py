"""Thin Gemini Developer API client for Veo image-to-video generation."""

from __future__ import annotations

from pathlib import Path
import time
from typing import Callable


VEO_MODEL = "veo-3.1-generate-preview"
VEO_TIMEOUT_SECONDS = 7 * 60


class VeoGenerationError(RuntimeError):
    """A Veo request did not complete with a downloadable video."""


def generate_image_to_video(
    image_path: Path,
    prompt: str,
    output_path: Path,
    *,
    api_key: str,
    timeout_seconds: int = VEO_TIMEOUT_SECONDS,
    poll_interval_seconds: int = 10,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> Path:
    """Generate one eight-second 720p 16:9 Veo clip from an existing storyboard frame.

    This follows Gemini's image-to-video API: ``prompt=`` and ``image=`` are
    separate inputs.  Captions are deliberately not part of the prompt; they
    are applied after Veo by the local ffmpeg renderer.
    """
    if not api_key:
        raise VeoGenerationError("GEMINI_API_KEY is not configured")

    try:
        from google import genai
        from google.genai import types
    except ImportError as exc:
        raise VeoGenerationError("Veo dependencies are not installed") from exc

    try:
        client = genai.Client(api_key=api_key)
        image_bytes = Path(image_path).read_bytes()
        suffix = Path(image_path).suffix.lower()
        mime_type = "image/jpeg" if suffix in (".jpg", ".jpeg") else "image/png"
        image_input = types.Image(image_bytes=image_bytes, mime_type=mime_type)

        operation = client.models.generate_videos(
            model=VEO_MODEL,
            prompt=prompt,
            image=image_input,
            config=types.GenerateVideosConfig(
                aspect_ratio="16:9",
                resolution="720p",
                duration_seconds=8,
                person_generation="allow_adult",
            ),
        )

        deadline = clock() + timeout_seconds
        while not operation.done:
            if clock() >= deadline:
                raise VeoGenerationError("Veo request exceeded the seven-minute wait limit")
            sleep(poll_interval_seconds)
            operation = client.operations.get(operation)

        generated = getattr(getattr(operation, "response", None), "generated_videos", None) or []
        if not generated or not getattr(generated[0], "video", None):
            raise VeoGenerationError("Veo completed without a generated video")

        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        video_bytes = client.files.download(file=generated[0].video)
        if not video_bytes:
            raise VeoGenerationError("Veo download returned an empty clip")
        output_path.write_bytes(video_bytes)
        return output_path
    except VeoGenerationError:
        raise
    except Exception as exc:
        raise VeoGenerationError(str(exc)) from exc
