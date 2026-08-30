"""
cinemapilot/dashboard/backend.py

FastAPI backend application for the CinemaPilot Production Dashboard.
Exposes endpoints for scenes, detailed scene graphs with signed media URLs,
and cascade audit events. Serves the static frontend.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import datetime
import os
import re
import sys
import threading
from typing import Any, Optional

from fastapi import FastAPI, HTTPException, Query, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

# Ensure cinemapilot root is on sys.path
CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.abspath(os.path.join(CURRENT_DIR, ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from shared.asset_storage import AssetStorageClient, AssetStorageError
from shared.graph_client import ProductionGraphClient, GraphClientError
from agents.trailer.agent import generate_production_trailer

app = FastAPI(
    title="CinemaPilot Production Dashboard API",
    description="Real-time multi-agent production graph monitoring and cascade viewer",
    version="1.0.0",
)

# Enable CORS for local development
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

ALLOWED_MEDIA_TYPES = {"storyboard", "music", "trailer"}

# Lazy singletons for clients
_graph_client: Optional[ProductionGraphClient] = None
_storage_client: Optional[AssetStorageClient] = None
_trailer_generation_lock = threading.Lock()


class TrailerGenerationRequest(BaseModel):
    source_scene_id: str


def get_graph_client() -> ProductionGraphClient:
    global _graph_client
    if _graph_client is None:
        _graph_client = ProductionGraphClient()
    return _graph_client


def get_storage_client() -> AssetStorageClient:
    global _storage_client
    if _storage_client is None:
        _storage_client = AssetStorageClient()
    return _storage_client


def sanitize_value(val: Any) -> Any:
    """Recursively convert datetime/date/decimal objects to JSON-serializable types."""
    if isinstance(val, (datetime.datetime, datetime.date)):
        return val.isoformat()
    if isinstance(val, dict):
        return {k: sanitize_value(v) for k, v in val.items()}
    if isinstance(val, list):
        return [sanitize_value(item) for item in val]
    return val


def trailer_with_proxy(record: dict | None) -> dict | None:
    """Attach the dashboard MP4 proxy without placing signed URLs in BigQuery."""
    if not record:
        return None
    trailer = dict(record)
    gs_uri = trailer.get("gs_uri")
    if gs_uri and gs_uri.startswith("gs://"):
        trailer["media_url"] = f"/api/media/trailer/production/{gs_uri.split('/')[-1]}"
    return trailer


@app.get("/api/media/{asset_type}/{scene_id}/{filename}")
def get_media_asset(asset_type: str, scene_id: str, filename: str):
    """
    Direct media proxy for dashboard assets (storyboard images, music audio) stored in GCS.
    Downloads object bytes directly via storage.objectViewer IAM permissions without requiring signed URLs.

    Args:
        asset_type: Must be 'storyboard' or 'music'.
        scene_id:   ID of scene (e.g. 'scene_005').
        filename:   File name (e.g. 'abc.png' or 'xyz.mp3').
    """
    if asset_type not in ALLOWED_MEDIA_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid asset_type '{asset_type}'. Allowed types: {sorted(ALLOWED_MEDIA_TYPES)}"
        )

    # Path traversal validation
    if not re.match(r"^[a-zA-Z0-9_\-]+$", scene_id):
        raise HTTPException(status_code=400, detail="Invalid scene_id format.")
    if not re.match(r"^[a-zA-Z0-9_\-\.]+$", filename) or ".." in filename:
        raise HTTPException(status_code=400, detail="Invalid filename format.")

    sc = get_storage_client()
    try:
        data, content_type = sc.download_asset_bytes(asset_type, scene_id, filename)
        return Response(
            content=data,
            media_type=content_type,
            headers={
                "Cache-Control": "public, max-age=3600",
                "Content-Disposition": f'inline; filename="{filename}"',
            }
        )
    except AssetStorageError as exc:
        raise HTTPException(status_code=404, detail=f"Asset not found: {exc}") from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to fetch asset: {exc}") from exc


@app.get("/api/trailer")
def get_trailer() -> dict[str, Any] | None:
    """Return the latest production trailer metadata and its media proxy URL."""
    try:
        return sanitize_value(trailer_with_proxy(get_graph_client().get_trailer()))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to fetch trailer: {exc}") from exc


@app.post("/api/trailer/generate")
def generate_trailer(request: TrailerGenerationRequest) -> dict[str, Any]:
    """Synchronously build the project trailer after an explicit dashboard click."""
    scene_id = request.source_scene_id
    if not re.match(r"^[a-zA-Z0-9_\-]+$", scene_id):
        raise HTTPException(status_code=400, detail="Invalid source_scene_id format.")

    graph = get_graph_client()
    try:
        if not graph.get_scene(scene_id):
            raise HTTPException(status_code=404, detail=f"Scene '{scene_id}' not found.")
        if not _trailer_generation_lock.acquire(blocking=False):
            raise HTTPException(status_code=409, detail="A production trailer is already being generated.")
        try:
            record = generate_production_trailer(
                scene_id,
                graph_client=graph,
                storage_client=get_storage_client(),
            )
            return sanitize_value(trailer_with_proxy(record))
        finally:
            _trailer_generation_lock.release()
    except HTTPException:
        raise
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Trailer generation failed: {exc}") from exc


@app.get("/api/scenes")
def get_scenes() -> list[dict[str, Any]]:
    """
    Return all scenes ordered by timeline position, with joined location names.
    Queries scenes and locations concurrently from BigQuery for faster response.
    """
    gc = get_graph_client()
    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            fut_scenes = executor.submit(gc.list_scenes)
            fut_locations = executor.submit(gc.list_locations)
            scenes = fut_scenes.result()
            locations_list = fut_locations.result()

        locations = {loc["location_id"]: loc for loc in locations_list}

        results = []
        for s in scenes:
            scene_dict = sanitize_value(dict(s))
            loc_id = scene_dict.get("location_id")
            loc = locations.get(loc_id) if loc_id else None
            scene_dict["location_name"] = loc.get("name") if loc else (loc_id or "Unassigned")
            scene_dict["location_type"] = loc.get("location_type") if loc else None
            results.append(scene_dict)

        return results
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to fetch scenes: {exc}") from exc


@app.get("/api/scene/{scene_id}")
def get_scene_detail(scene_id: str) -> dict[str, Any]:
    """
    Return full graph detail for one scene:
    - scene record
    - location
    - characters
    - props
    - budget_lines
    - schedule_blocks
    - risk_flags (with grafana_incident_url)
    - storyboard (with media proxy URL via /api/media/...)
    - music_cue (with media proxy URL via /api/media/...)
    - director_note
    - producer_overview
    - explanation
    """
    gc = get_graph_client()

    try:
        scene = gc.get_scene(scene_id)
        if not scene:
            raise HTTPException(status_code=404, detail=f"Scene '{scene_id}' not found.")

        location_id = scene.get("location_id")
        character_ids = scene.get("character_ids") or []
        prop_ids = scene.get("prop_ids") or []

        # Execute entity queries in parallel for high responsiveness
        with ThreadPoolExecutor(max_workers=12) as executor:
            fut_loc = executor.submit(gc.get_location, location_id) if location_id else None
            fut_chars = [executor.submit(gc.get_character, cid) for cid in character_ids]
            fut_props = [executor.submit(gc.get_prop, pid) for pid in prop_ids]
            fut_budget = executor.submit(gc.get_budget_lines_for_entity, scene_id)
            fut_schedule = executor.submit(gc.get_schedule_blocks_for_scene, scene_id)
            fut_scene_risks = executor.submit(gc.get_risk_flags_for_entity, scene_id)
            fut_loc_risks = executor.submit(gc.get_risk_flags_for_entity, location_id) if location_id else None
            fut_storyboard = executor.submit(gc.get_storyboard, f"sb_{scene_id}")
            fut_music = executor.submit(gc.get_music_cue, f"mc_{scene_id}")
            fut_director = executor.submit(gc.get_director_note, f"dn_{scene_id}")
            fut_producer = executor.submit(gc.get_producer_overview, f"po_{scene_id}")
            fut_explanation = executor.submit(gc.get_explanation, f"exp_{scene_id}")
            fut_edges = executor.submit(gc.get_scene_edges, scene_id)
            fut_trailer = executor.submit(gc.get_trailer)

            location = fut_loc.result() if fut_loc else None
            characters = [record for future in fut_chars if (record := future.result())]
            props = [record for future in fut_props if (record := future.result())]
            budget_lines = fut_budget.result() or []
            schedule_blocks = fut_schedule.result() or []
            scene_risks = fut_scene_risks.result() or []
            loc_risks = (fut_loc_risks.result() if fut_loc_risks else []) or []
            storyboard = fut_storyboard.result()
            music_cue = fut_music.result()
            director_note = fut_director.result()
            producer_overview = fut_producer.result()
            explanation = fut_explanation.result()
            scene_edges = fut_edges.result() or []
            trailer = trailer_with_proxy(fut_trailer.result())

        # Risk flags deduplication
        seen_risk_ids = set()
        risk_flags = []
        for rf in scene_risks + loc_risks:
            rf_id = rf.get("risk_flag_id")
            if rf_id and rf_id not in seen_risk_ids:
                seen_risk_ids.add(rf_id)
                risk_flags.append(rf)

        # Storyboard + media proxy URL
        if storyboard:
            storyboard = dict(storyboard)
            gs_uri = storyboard.get("gs_uri")
            if gs_uri and gs_uri.startswith("gs://"):
                filename = gs_uri.split("/")[-1]
                proxy_path = f"/api/media/storyboard/{scene_id}/{filename}"
                storyboard["media_url"] = proxy_path
                storyboard["proxy_url"] = proxy_path
                storyboard["signed_url"] = proxy_path

        # Music cue + media proxy URL
        if music_cue:
            music_cue = dict(music_cue)
            gs_uri = music_cue.get("gs_uri")
            if gs_uri and gs_uri.startswith("gs://"):
                filename = gs_uri.split("/")[-1]
                proxy_path = f"/api/media/music/{scene_id}/{filename}"
                music_cue["media_url"] = proxy_path
                music_cue["proxy_url"] = proxy_path
                music_cue["signed_url"] = proxy_path

        detail = {
            "scene": scene,
            "location": location,
            "characters": characters,
            "props": props,
            "budget_lines": budget_lines,
            "schedule_blocks": schedule_blocks,
            "risk_flags": risk_flags,
            "storyboard": storyboard,
            "music_cue": music_cue,
            "director_note": director_note,
            "producer_overview": producer_overview,
            "explanation": explanation,
            "scene_edges": scene_edges,
            "trailer": trailer,
        }

        return sanitize_value(detail)

    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to fetch scene detail: {exc}") from exc


@app.get("/api/events")
def get_events(since: Optional[str] = Query(None, description="ISO timestamp or float timestamp")) -> list[dict[str, Any]]:
    """
    Return recent events from the events audit log for cascade timeline visualization.
    """
    gc = get_graph_client()
    try:
        if since:
            try:
                # Try parsing as ISO format or unix timestamp
                if since.replace(".", "", 1).isdigit():
                    since_dt = datetime.datetime.fromtimestamp(float(since), tz=datetime.timezone.utc)
                else:
                    since_dt = datetime.datetime.fromisoformat(since)
                events = gc.get_events_since(since_dt)
            except Exception:
                # Fallback to list_events if parsing fails
                events = gc.list_events()
        else:
            events = gc.list_events()

        return sanitize_value(events)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to fetch events: {exc}") from exc


# Mount static assets directory
STATIC_DIR = os.path.join(CURRENT_DIR, "static")
os.makedirs(STATIC_DIR, exist_ok=True)

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/")
def read_root():
    """Serve single-page frontend application."""
    index_file = os.path.join(STATIC_DIR, "index.html")
    if os.path.exists(index_file):
        return FileResponse(index_file)
    return {"message": "CinemaPilot Dashboard API is running. index.html not yet generated in static directory."}
