"""Render a local CinemaPilot concept trailer from existing storyboard images.

Example:
  python scripts/render_concept_trailer.py \
    --shot scene_001=/tmp/scene_001.jpg \
    --shot scene_005=/tmp/scene_005.jpg \
    --music /tmp/music_cue.mp3 \
    --output /tmp/cinemapilot-concept-trailer.mp4
"""

from __future__ import annotations

import argparse
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from shared.concept_trailer import (
    TrailerRenderError,
    build_concept_trailer_plan,
    render_concept_trailer,
)


def parse_shot(value: str) -> tuple[str, Path]:
    """Parse ``scene_id=/local/path/to/storyboard.jpg`` CLI input."""
    scene_id, separator, image_path = value.partition("=")
    if not separator or not scene_id or not image_path:
        raise argparse.ArgumentTypeError("Shot must use scene_id=/path/to/storyboard.jpg")
    return scene_id, Path(image_path)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Render a local MP4 animatic from existing CinemaPilot storyboard images."
    )
    parser.add_argument(
        "--shot",
        action="append",
        type=parse_shot,
        required=True,
        help="Storyboard input as scene_id=/absolute/or/relative/image.jpg. Repeat for each shot.",
    )
    parser.add_argument("--music", type=Path, help="Optional local MP3/WAV music cue")
    parser.add_argument("--output", type=Path, required=True, help="Destination .mp4 file")
    parser.add_argument("--seconds-per-shot", type=float, default=2.5)
    args = parser.parse_args()

    scene_assets = [
        {
            "scene_id": scene_id,
            "storyboard_path": image_path,
            "timeline_position": index,
        }
        for index, (scene_id, image_path) in enumerate(args.shot)
    ]
    try:
        plan = build_concept_trailer_plan(
            scene_assets,
            max_shots=len(scene_assets),
            seconds_per_shot=args.seconds_per_shot,
        )
        output = render_concept_trailer(plan, args.output, music_path=args.music)
    except (TrailerRenderError, ValueError) as exc:
        parser.error(str(exc))

    print(f"Concept trailer created: {output}")
    for number, shot in enumerate(plan, start=1):
        print(f"  Shot {number}: {shot.scene_id} / {shot.camera_plan} / {shot.emotional_intent}")


if __name__ == "__main__":
    main()
