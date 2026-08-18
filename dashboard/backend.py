"""
cinemapilot/dashboard/backend.py

FastAPI backend application for the CinemaPilot Production Dashboard.
Exposes endpoints for scenes, detailed scene graphs with signed media URLs,
and cascade audit events. Serves the static frontend.
"""

from __future__ import annotations

import datetime
import os
import sys
from typing import Any, Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

# Ensure cinemapilot root is on sys.path
CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.abspath(os.path.join(CURRENT_DIR, ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from shared.asset_storage import AssetStorageClient, AssetStorageError
from shared.graph_client import ProductionGraphClient, GraphClientError

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

# Lazy singletons for clients
_graph_client: Optional[ProductionGraphClient] = None
_storage_client: Optional[AssetStorageClient] = None


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


@app.get("/api/scenes")
def get_scenes() -> list[dict[str, Any]]:
    """
    Return all scenes ordered by timeline position, with joined location names.
    """
    gc = get_graph_client()
    try:
        scenes = gc.list_scenes()
        locations = {loc["location_id"]: loc for loc in gc.list_locations()}

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
    - storyboard (with signed URL via asset_storage)
    - music_cue (with signed URL via asset_storage)
    - director_note
    - producer_overview
    - explanation
    """
    gc = get_graph_client()
    sc = get_storage_client()

    try:
        scene = gc.get_scene(scene_id)
        if not scene:
            raise HTTPException(status_code=404, detail=f"Scene '{scene_id}' not found.")

        location_id = scene.get("location_id")
        location = gc.get_location(location_id) if location_id else None

        # Characters
        characters = []
        for cid in scene.get("character_ids") or []:
            c = gc.get_character(cid)
            if c:
                characters.append(c)

        # Props
        props = []
        for pid in scene.get("prop_ids") or []:
            p = gc.get_prop(pid)
            if p:
                props.append(p)

        # Budget lines
        budget_lines = gc.get_budget_lines_for_entity(scene_id)

        # Schedule blocks
        schedule_blocks = gc.get_schedule_blocks_for_scene(scene_id)

        # Risk flags (check both scene_id and location_id, deduplicate)
        scene_risks = gc.get_risk_flags_for_entity(scene_id)
        loc_risks = gc.get_risk_flags_for_entity(location_id) if location_id else []
        seen_risk_ids = set()
        risk_flags = []
        for rf in scene_risks + loc_risks:
            rf_id = rf.get("risk_flag_id")
            if rf_id and rf_id not in seen_risk_ids:
                seen_risk_ids.add(rf_id)
                risk_flags.append(rf)

        # Storyboard + signed URL
        storyboard = gc.get_storyboard(f"sb_{scene_id}")
        if storyboard:
            storyboard = dict(storyboard)
            gs_uri = storyboard.get("gs_uri")
            if gs_uri and gs_uri.startswith("gs://"):
                try:
                    storyboard["signed_url"] = sc.get_signed_url(gs_uri, expiration_minutes=120)
                except Exception as exc:
                    storyboard["signed_url_error"] = str(exc)

        # Music cue + signed URL
        music_cue = gc.get_music_cue(f"mc_{scene_id}")
        if music_cue:
            music_cue = dict(music_cue)
            gs_uri = music_cue.get("gs_uri")
            if gs_uri and gs_uri.startswith("gs://"):
                try:
                    music_cue["signed_url"] = sc.get_signed_url(gs_uri, expiration_minutes=120)
                except Exception as exc:
                    music_cue["signed_url_error"] = str(exc)

        # Director note
        director_note = gc.get_director_note(f"dn_{scene_id}")

        # Producer overview
        producer_overview = gc.get_producer_overview(f"po_{scene_id}")

        # Explanation
        explanation = gc.get_explanation(f"exp_{scene_id}")

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
