"""Gemini Veo image-to-video client with a local MP4 fallback.

The fallback is intentionally labelled as a placeholder. It lets the Trailer
Agent exercise the same clip-to-trailer flow before paid Veo access is enabled.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import time

from shared.concept_trailer import TrailerShot, TrailerVideo, render_veo_placeholder


VEO_MODEL = "veo-3.1-generate-preview"
VEO_CLIP_DURATION_SECONDS = 8


def generate_veo_clip(
    shot: TrailerShot,
    output_path: str | Path,
    *,
    prompt: str,
    gemini_api_key: str | None,
    timeout_seconds: int = 900,
) -> TrailerVideo:
    """Generate an 8-second Veo clip, or create an explicit local fallback.

    The Veo request is image-to-video: the storyboard image is the first frame.
    It uses only documented Veo 3.1 parameters for this path.
    """
    output = Path(output_path)
    veo_shot = replace(shot, duration_seconds=VEO_CLIP_DURATION_SECONDS)
    if not gemini_api_key:
        return _render_fallback(veo_shot, output, "GEMINI_API_KEY is not configured")

    try:
        from google import genai
        from google.genai import types

        client = genai.Client(api_key=gemini_api_key)
        image = types.Image.from_file(location=str(shot.image_path))
        operation = client.models.generate_videos(
            model=VEO_MODEL,
            source=types.GenerateVideosSource(prompt=prompt, image=image),
            config=types.GenerateVideosConfig(
                aspect_ratio="16:9",
                resolution="720p",
                duration_seconds=VEO_CLIP_DURATION_SECONDS,
                number_of_videos=1,
                person_generation="allow_adult",
            ),
        )

        deadline = time.monotonic() + timeout_seconds
        while not operation.done:
            if time.monotonic() >= deadline:
                raise TimeoutError(f"Veo operation exceeded {timeout_seconds} seconds")
            time.sleep(10)
            operation = client.operations.get(operation)

        generated = operation.response.generated_videos[0]
        client.files.download(file=generated.video)
        output.parent.mkdir(parents=True, exist_ok=True)
        generated.video.save(str(output))
        if not output.is_file():
            raise RuntimeError("Veo completed without writing an MP4 file")
        return TrailerVideo(
            scene_id=shot.scene_id,
            video_path=output,
            provider="gemini-veo-3.1",
            is_placeholder=False,
        )
    except Exception as exc:
        return _render_fallback(veo_shot, output, f"Veo unavailable: {type(exc).__name__}")


def _render_fallback(shot: TrailerShot, output_path: Path, reason: str) -> TrailerVideo:
    video = render_veo_placeholder(shot, output_path)
    return replace(video, fallback_reason=reason)
