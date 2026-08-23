"""Trailer Agent — joins storyboard, direction and music into a trailer asset.

Each storyboard image becomes a Veo image-to-video clip when Gemini access is
available. Otherwise it becomes a clearly-labelled local MP4 placeholder, so the
rest of the production pipeline can be developed and demonstrated locally.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Mapping, Sequence

from shared.concept_trailer import (
    TrailerVideo,
    build_concept_trailer_plan,
    render_trailer_from_videos,
)
from shared.veo_client import VEO_CLIP_DURATION_SECONDS, generate_veo_clip


def construct_veo_prompt(scene: Mapping[str, Any], camera_plan: str, emotional_intent: str) -> str:
    """Build a motion prompt without changing the supplied storyboard's story facts."""
    camera_cues = scene.get("camera_cues") or []
    cues = camera_plan or ", ".join(str(cue) for cue in camera_cues) or "controlled cinematic movement"
    tone = emotional_intent or scene.get("emotional_tone") or "neutral"
    return (
        "Animate the supplied storyboard image as a single cinematic film shot. "
        "Preserve the shown characters, wardrobe, location and visual composition. "
        f"Camera direction: {cues}. Emotional tone: {tone}. "
        "Natural movement and realistic lighting. No on-screen text, subtitles, logos or watermarks."
    )


def generate_trailer(
    scene_assets: Sequence[Mapping[str, Any]],
    output_path: str | Path,
    *,
    music_path: str | Path | None = None,
    gemini_api_key: str | None = None,
    max_shots: int = 3,
) -> dict[str, Any]:
    """Create a trailer from current storyboard assets.

    ``scene_assets`` is the handoff boundary from the Production Graph. It will
    later be filled from storyboard, director and music records; keeping it a
    plain mapping also makes the local demo independent of BigQuery and GCS.
    """
    if max_shots < 1:
        raise ValueError("max_shots must be at least 1")

    api_key = gemini_api_key if gemini_api_key is not None else os.environ.get("GEMINI_API_KEY")
    plan = build_concept_trailer_plan(scene_assets, max_shots=max_shots, seconds_per_shot=VEO_CLIP_DURATION_SECONDS)
    destination = Path(output_path)
    clips_directory = destination.parent / f"{destination.stem}-clips"
    clips: list[TrailerVideo] = []

    source_by_scene = {str(asset["scene_id"]): asset for asset in scene_assets if asset.get("scene_id")}
    for shot in plan:
        source = source_by_scene[shot.scene_id]
        clip = generate_veo_clip(
            shot,
            clips_directory / f"{shot.scene_id}.mp4",
            prompt=construct_veo_prompt(source, shot.camera_plan, shot.emotional_intent),
            gemini_api_key=api_key,
        )
        clips.append(clip)

    trailer_path = render_trailer_from_videos(clips, destination, music_path=music_path)
    placeholders = [clip.scene_id for clip in clips if clip.is_placeholder]
    return {
        "trailer_id": f"tr_{destination.stem}",
        "trailer_path": str(trailer_path),
        "clip_count": len(clips),
        "clips": clips,
        "status": "ready",
        "generation_mode": (
            "local-placeholder" if len(placeholders) == len(clips)
            else "mixed" if placeholders
            else "gemini-veo"
        ),
        "placeholder_scene_ids": placeholders,
    }
