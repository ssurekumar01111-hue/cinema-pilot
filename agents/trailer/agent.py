"""Production Trailer Agent.

The agent turns existing storyboard assets into the latest project trailer. A
dashboard click is the only normal entrypoint, so a scene edit never spends
Veo quota by itself. Veo improves each visual clip when configured; the same
storyboard images remain an honest, playable local fallback.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import mimetypes
from pathlib import Path
import tempfile
from typing import Any, Callable

from shared.asset_storage import AssetStorageClient
from shared.concept_trailer import TrailerShot, render_trailer, select_timeline_shots
from shared.graph_client import ProductionGraphClient
from shared.secret_client import get_secret
from shared.veo_client import generate_image_to_video


TRAILER_ID = "tr_project"
TRAILER_ENTITY_ID = "tr_project"


def _caption_words(voice_preview: dict | None) -> tuple[str, ...]:
    """Return the first available voice line as at most eight readable words."""
    if not voice_preview:
        return ()
    dialogue_lines = voice_preview.get("dialogue_lines")
    if isinstance(dialogue_lines, str):
        try:
            dialogue_lines = json.loads(dialogue_lines)
        except json.JSONDecodeError:
            return ()
    if not isinstance(dialogue_lines, list):
        return ()

    for item in dialogue_lines:
        line = item.get("line") if isinstance(item, dict) else item if isinstance(item, str) else None
        if isinstance(line, str) and line.strip():
            return tuple(line.strip().split()[:8])
    return ()


def build_veo_prompt(scene: dict[str, Any], director_note: dict[str, Any] | None) -> str:
    """Build an image-to-video prompt that preserves the supplied storyboard frame."""
    camera_plan = (director_note or {}).get("camera_plan") or ", ".join(scene.get("camera_cues") or [])
    camera_plan = camera_plan or "a restrained slow push-in"
    tone = scene.get("emotional_tone") or "the established scene mood"
    return " ".join([
        "Animate this existing cinematic storyboard frame into one continuous eight-second 16:9 shot.",
        f"Camera movement: {camera_plan}.",
        f"Emotional intent and lighting: {tone}.",
        "Keep the subject, wardrobe, hair, location, props, composition, light direction, color tone, and motion direction continuous with the supplied frame.",
        "Use subtle environmental motion only where it belongs in the frame.",
        "Do not add dialogue captions, titles, logos, watermarks, or new story elements.",
    ])


def _asset_suffix(gs_uri: str, content_type: str, default: str) -> str:
    suffix = Path(gs_uri.split("?", 1)[0]).suffix
    if suffix:
        return suffix
    return mimetypes.guess_extension(content_type or "") or default


def _base_record(source_scene_id: str, selected: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "trailer_id": TRAILER_ID,
        "source_scene_id": source_scene_id,
        "source_scene_ids": [item["scene"]["scene_id"] for item in selected],
        "music_source_scene_id": None,
        "gs_uri": None,
        "status": "generating",
        "generation_mode": None,
        "clip_count": 0,
        "fallback_scene_ids": [],
        "captioned_scene_ids": [],
        "error": None,
    }


def _persist(
    graph: ProductionGraphClient,
    record: dict[str, Any],
    before_state: dict[str, Any] | None,
    cascade_id: str | None,
) -> None:
    cleaned_before = {
        k: (v.isoformat() if hasattr(v, "isoformat") else v)
        for k, v in (before_state or {}).items()
    }
    cleaned_after = {
        k: (v.isoformat() if hasattr(v, "isoformat") else v)
        for k, v in {**record, **({"cascade_id": cascade_id} if cascade_id else {})}.items()
    }
    graph.upsert_trailer(record)
    graph.log_event(
        actor_agent="trailer_agent",
        entity_type="trailer",
        entity_id=TRAILER_ENTITY_ID,
        before_state=cleaned_before,
        after_state=cleaned_after,
        triggered_agents=[],
    )


def _available_storyboard_scenes(graph: ProductionGraphClient) -> list[dict[str, Any]]:
    available: list[dict[str, Any]] = []
    for scene in graph.list_scenes():
        scene_id = scene.get("scene_id")
        if not scene_id:
            continue
        storyboard = graph.get_storyboard(f"sb_{scene_id}")
        if storyboard and storyboard.get("gs_uri"):
            available.append({"scene": scene, "storyboard": storyboard})
    available.sort(key=lambda item: (item["scene"].get("timeline_position") is None, item["scene"].get("timeline_position") or 0))
    return available


def generate_production_trailer(
    source_scene_id: str,
    cascade_id: str | None = None,
    *,
    graph_client: ProductionGraphClient | None = None,
    storage_client: AssetStorageClient | None = None,
    secret_getter: Callable[[str], str | None] = get_secret,
    veo_generator: Callable[..., Path] = generate_image_to_video,
    trailer_renderer: Callable[..., Path] = render_trailer,
) -> dict[str, Any]:
    """Build and persist the latest project-wide trailer from existing assets.

    The selected source scene supplies optional Lyria music. Visual selection
    remains project-wide: first / middle / last available storyboards in
    timeline order, up to three clips. Individual Veo failures fall back to
    their image instead of failing the whole trailer.
    """
    graph = graph_client or ProductionGraphClient()
    storage = storage_client or AssetStorageClient()
    prior = graph.get_trailer(TRAILER_ID)
    selected = select_timeline_shots(_available_storyboard_scenes(graph), limit=3)
    record = _base_record(source_scene_id, selected)

    if not selected:
        record.update({"status": "failed", "error": "No storyboard assets are ready for trailer generation"})
        _persist(graph, record, prior, cascade_id)
        raise ValueError(record["error"])

    _persist(graph, record, prior, cascade_id)

    try:
        with tempfile.TemporaryDirectory(prefix="cinemapilot-trailer-") as temp_dir_raw:
            temp_dir = Path(temp_dir_raw)
            prepared: list[dict[str, Any]] = []
            for item in selected:
                scene = item["scene"]
                storyboard = item["storyboard"]
                image_bytes, content_type = storage.download_gs_uri(storyboard["gs_uri"])
                image_path = temp_dir / f"{scene['scene_id']}{_asset_suffix(storyboard['gs_uri'], content_type, '.png')}"
                image_path.write_bytes(image_bytes)
                voice_preview = graph.get_voice_preview(f"vp_{scene['scene_id']}")
                prepared.append({
                    **item,
                    "image_path": image_path,
                    "caption_words": _caption_words(voice_preview),
                    "director_note": graph.get_director_note(f"dn_{scene['scene_id']}"),
                })

            music_path: Path | None = None
            music_cue = graph.get_music_cue(f"mc_{source_scene_id}")
            if music_cue and music_cue.get("gs_uri") and music_cue.get("status") == "completed":
                try:
                    music_bytes, music_type = storage.download_gs_uri(music_cue["gs_uri"])
                    music_path = temp_dir / f"music{_asset_suffix(music_cue['gs_uri'], music_type, '.mp3')}"
                    music_path.write_bytes(music_bytes)
                    record["music_source_scene_id"] = source_scene_id
                except Exception as exc:
                    record["error"] = f"Music cue was unavailable / trailer rendered without music: {exc}"

            api_key = secret_getter("GEMINI_API_KEY")
            veo_paths: dict[str, Path] = {}
            fallback_scene_ids: list[str] = []
            errors: list[str] = []

            if api_key:
                with ThreadPoolExecutor(max_workers=3) as executor:
                    futures = {
                        executor.submit(
                            veo_generator,
                            item["image_path"],
                            build_veo_prompt(item["scene"], item["director_note"]),
                            temp_dir / f"veo_{item['scene']['scene_id']}.mp4",
                            api_key=api_key,
                        ): item
                        for item in prepared
                    }
                    for future in as_completed(futures):
                        item = futures[future]
                        scene_id = item["scene"]["scene_id"]
                        try:
                            veo_paths[scene_id] = future.result()
                        except Exception as exc:
                            fallback_scene_ids.append(scene_id)
                            errors.append(f"{scene_id}: {exc}")
            else:
                fallback_scene_ids = [item["scene"]["scene_id"] for item in prepared]
                errors.append("GEMINI_API_KEY is not configured / used storyboard fallback")

            shots = []
            for item in prepared:
                scene_id = item["scene"]["scene_id"]
                video_path = veo_paths.get(scene_id)
                media_path = video_path or item["image_path"]
                if video_path is None and scene_id not in fallback_scene_ids:
                    fallback_scene_ids.append(scene_id)
                shots.append(TrailerShot(
                    scene_id=scene_id,
                    media_path=media_path,
                    media_kind="video" if video_path else "image",
                    caption_words=item["caption_words"],
                ))

            output_path = trailer_renderer(shots, temp_dir / "production_trailer.mp4", music_path=music_path)
            captioned_scene_ids = [shot.scene_id for shot in shots if shot.caption_rendered]
            requested_captions = [shot.scene_id for shot in shots if shot.caption_words]
            if requested_captions and len(captioned_scene_ids) != len(requested_captions):
                errors.append("Caption overlay was unavailable for one or more clips / trailer rendered without those captions")
            gs_uri = storage.upload_asset(
                "trailer", "production", Path(output_path).read_bytes(), "video/mp4", "mp4"
            )

            if len(fallback_scene_ids) == len(prepared):
                generation_mode = "local-placeholder"
            elif fallback_scene_ids:
                generation_mode = "mixed"
            else:
                generation_mode = "veo"

            record.update({
                "gs_uri": gs_uri,
                "status": "ready",
                "generation_mode": generation_mode,
                "clip_count": len(shots),
                "fallback_scene_ids": fallback_scene_ids,
                "captioned_scene_ids": captioned_scene_ids,
                "error": "; ".join(filter(None, [record.get("error"), *errors])) or None,
            })
    except Exception as exc:
        record.update({
            "status": "failed",
            "generation_mode": None,
            "clip_count": 0,
            "error": str(exc),
        })
        _persist(graph, record, prior, cascade_id)
        raise

    _persist(graph, record, prior, cascade_id)
    return record
