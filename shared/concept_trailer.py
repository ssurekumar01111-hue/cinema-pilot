"""Local concept-trailer planning and rendering from existing storyboard stills.

This module deliberately does not call a video model or any Google Cloud service.
It turns already-generated storyboard images into a reviewable MP4 animatic so a
producer can validate the story direction before spending on generated video.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import subprocess
from typing import Any, Mapping, Sequence


class TrailerRenderError(RuntimeError):
    """Raised when a local concept trailer cannot be planned or rendered."""


@dataclass(frozen=True)
class TrailerShot:
    """One storyboard still used as a timed shot in a concept trailer."""

    scene_id: str
    image_path: Path
    duration_seconds: float
    camera_plan: str
    emotional_intent: str


@dataclass(frozen=True)
class TrailerVideo:
    """A video input that can be assembled into the final trailer."""

    scene_id: str
    video_path: Path
    provider: str
    is_placeholder: bool
    fallback_reason: str | None = None


def build_concept_trailer_plan(
    scene_assets: Sequence[Mapping[str, Any]],
    *,
    max_shots: int = 4,
    seconds_per_shot: float = 2.5,
) -> list[TrailerShot]:
    """Select a compact beginning-to-end trailer plan from storyboard assets.

    ``scene_assets`` is intentionally a plain sequence so it can be filled from
    BigQuery later or from local files today. Each usable item needs ``scene_id``
    and ``storyboard_path``. Optional ``timeline_position``, ``scene_number``,
    ``camera_plan`` and ``emotional_tone`` enrich ordering and shot metadata.

    When more than ``max_shots`` scenes are supplied, the first and last scenes
    are always kept and the middle shots are spaced evenly. That produces an
    establishing beat, escalation and payoff rather than an arbitrary sample.
    """
    if max_shots < 1:
        raise ValueError("max_shots must be at least 1")
    if seconds_per_shot <= 0:
        raise ValueError("seconds_per_shot must be greater than 0")

    usable = [
        asset
        for asset in scene_assets
        if asset.get("scene_id") and asset.get("storyboard_path")
    ]
    ordered = sorted(
        usable,
        key=lambda asset: (
            asset.get("timeline_position") is None,
            asset.get("timeline_position", 0),
            asset.get("scene_number") is None,
            asset.get("scene_number", 0),
            str(asset["scene_id"]),
        ),
    )
    if not ordered:
        raise TrailerRenderError("A concept trailer needs at least one storyboard image.")

    if len(ordered) <= max_shots:
        selected = ordered
    elif max_shots == 1:
        selected = [ordered[0]]
    else:
        indices = [round(index * (len(ordered) - 1) / (max_shots - 1)) for index in range(max_shots)]
        selected = [ordered[index] for index in indices]

    return [
        TrailerShot(
            scene_id=str(asset["scene_id"]),
            image_path=Path(str(asset["storyboard_path"])),
            duration_seconds=seconds_per_shot,
            camera_plan=str(asset.get("camera_plan") or "Storyboard hold"),
            emotional_intent=str(asset.get("emotional_tone") or "neutral"),
        )
        for asset in selected
    ]
def render_concept_trailer(
    shots: Sequence[TrailerShot],
    output_path: str | Path,
    *,
    music_path: str | Path | None = None,
    ffmpeg_binary: str = "ffmpeg",
    fps: int = 24,
    width: int = 1280,
    height: int = 720,
) -> Path:
    """Render storyboard stills into a local MP4, optionally under a music cue.

    The output is an animatic, not generated video: every shot is an existing
    storyboard still held for a short, fixed duration. ffmpeg is the only local
    runtime dependency. Nothing is uploaded and no cloud credential is used.
    """
    if not shots:
        raise TrailerRenderError("A concept trailer needs at least one shot.")
    if fps < 1 or width < 1 or height < 1:
        raise ValueError("fps, width and height must be positive")

    for shot in shots:
        if shot.duration_seconds <= 0:
            raise TrailerRenderError(f"Shot '{shot.scene_id}' has a non-positive duration.")
        if not shot.image_path.is_file():
            raise TrailerRenderError(f"Storyboard image not found: {shot.image_path}")

    resolved_music = Path(music_path) if music_path else None
    if resolved_music and not resolved_music.is_file():
        raise TrailerRenderError(f"Music file not found: {resolved_music}")

    destination = Path(output_path)
    if destination.suffix.lower() != ".mp4":
        raise TrailerRenderError("Concept trailer output must have an .mp4 extension.")
    destination.parent.mkdir(parents=True, exist_ok=True)

    command = [ffmpeg_binary, "-y"]
    for shot in shots:
        command.extend([
            "-loop", "1",
            "-framerate", str(fps),
            "-t", str(shot.duration_seconds),
            "-i", str(shot.image_path),
        ])
    if resolved_music:
        command.extend(["-stream_loop", "-1", "-i", str(resolved_music)])

    filter_parts: list[str] = []
    video_labels: list[str] = []
    for index, shot in enumerate(shots):
        label = f"v{index}"
        fade_seconds = min(0.25, shot.duration_seconds / 4)
        fade_out_start = max(0, shot.duration_seconds - fade_seconds)
        filter_parts.append(
            f"[{index}:v]"
            f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
            f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,"
            f"setsar=1,fps={fps},format=yuv420p,"
            f"fade=t=in:st=0:d={fade_seconds},"
            f"fade=t=out:st={fade_out_start}:d={fade_seconds}"
            f"[{label}]"
        )
        video_labels.append(f"[{label}]")
    filter_parts.append(f"{''.join(video_labels)}concat=n={len(shots)}:v=1:a=0[video]")

    command.extend([
        "-filter_complex", ";".join(filter_parts),
        "-map", "[video]",
    ])
    if resolved_music:
        command.extend(["-map", f"{len(shots)}:a:0", "-shortest", "-c:a", "aac", "-b:a", "192k"])
    command.extend([
        "-c:v", "libx264",
        "-pix_fmt", "yuv420p",
        "-movflags", "+faststart",
        str(destination),
    ])

    try:
        subprocess.run(command, check=True, capture_output=True, text=True)
    except FileNotFoundError as exc:
        raise TrailerRenderError(
            "ffmpeg is not installed or is not available on PATH."
        ) from exc
    except subprocess.CalledProcessError as exc:
        details = (exc.stderr or exc.stdout or "ffmpeg failed without output").strip()
        raise TrailerRenderError(f"ffmpeg could not render the concept trailer: {details}") from exc

    if not destination.is_file():
        raise TrailerRenderError("ffmpeg completed but did not create the concept trailer.")
    return destination


def render_veo_placeholder(
    shot: TrailerShot,
    output_path: str | Path,
    *,
    ffmpeg_binary: str = "ffmpeg",
    fps: int = 24,
    width: int = 1280,
    height: int = 720,
) -> TrailerVideo:
    """Create a local MP4 stand-in for one future Veo clip.

    The result deliberately says ``local-placeholder`` rather than claiming a
    Veo request happened. The next trailer step still receives a normal MP4 and
    can be developed without a cloud key or paid generation.
    """
    video_path = render_concept_trailer(
        [shot],
        output_path,
        ffmpeg_binary=ffmpeg_binary,
        fps=fps,
        width=width,
        height=height,
    )
    return TrailerVideo(
        scene_id=shot.scene_id,
        video_path=video_path,
        provider="local-placeholder",
        is_placeholder=True,
    )


def render_trailer_from_videos(
    videos: Sequence[TrailerVideo],
    output_path: str | Path,
    *,
    music_path: str | Path | None = None,
    ffmpeg_binary: str = "ffmpeg",
    fps: int = 24,
    width: int = 1280,
    height: int = 720,
) -> Path:
    """Assemble video clips, including local Veo placeholders, into a trailer."""
    if not videos:
        raise TrailerRenderError("A trailer needs at least one video clip.")
    if fps < 1 or width < 1 or height < 1:
        raise ValueError("fps, width and height must be positive")
    for video in videos:
        if not video.video_path.is_file():
            raise TrailerRenderError(f"Video clip not found: {video.video_path}")

    resolved_music = Path(music_path) if music_path else None
    if resolved_music and not resolved_music.is_file():
        raise TrailerRenderError(f"Music file not found: {resolved_music}")

    destination = Path(output_path)
    if destination.suffix.lower() != ".mp4":
        raise TrailerRenderError("Trailer output must have an .mp4 extension.")
    destination.parent.mkdir(parents=True, exist_ok=True)

    command = [ffmpeg_binary, "-y"]
    for video in videos:
        command.extend(["-i", str(video.video_path)])
    if resolved_music:
        command.extend(["-stream_loop", "-1", "-i", str(resolved_music)])

    filter_parts: list[str] = []
    video_labels: list[str] = []
    for index in range(len(videos)):
        label = f"v{index}"
        filter_parts.append(
            f"[{index}:v]"
            f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
            f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,"
            f"setsar=1,fps={fps},format=yuv420p[{label}]"
        )
        video_labels.append(f"[{label}]")
    filter_parts.append(f"{''.join(video_labels)}concat=n={len(videos)}:v=1:a=0[video]")

    command.extend(["-filter_complex", ";".join(filter_parts), "-map", "[video]"])
    if resolved_music:
        command.extend(["-map", f"{len(videos)}:a:0", "-shortest", "-c:a", "aac", "-b:a", "192k"])
    command.extend([
        "-c:v", "libx264",
        "-pix_fmt", "yuv420p",
        "-movflags", "+faststart",
        str(destination),
    ])

    try:
        subprocess.run(command, check=True, capture_output=True, text=True)
    except FileNotFoundError as exc:
        raise TrailerRenderError(
            "ffmpeg is not installed or is not available on PATH."
        ) from exc
    except subprocess.CalledProcessError as exc:
        details = (exc.stderr or exc.stdout or "ffmpeg failed without output").strip()
        raise TrailerRenderError(f"ffmpeg could not render the trailer: {details}") from exc

    if not destination.is_file():
        raise TrailerRenderError("ffmpeg completed but did not create the trailer.")
    return destination
