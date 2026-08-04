"""
agents/director/agent.py

Director Agent for CinemaPilot.
Generates shot and pacing guidance for production scenes using Gemini,
grounded strictly in real scene data (emotional_tone, camera_cues, location type/name, character count).
Stores metadata in BigQuery Production Graph and logs audit events.
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any

from google import genai
from google.genai import types

# Ensure shared package is importable when running directly
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../../")))

from shared.graph_client import ProductionGraphClient


def direct_scene(scene_id: str) -> dict[str, Any]:
    """
    Generate director guidance (shot suggestions, pacing notes, camera plan) for a scene ID.

    Steps:
      a. Fetches scene and location records from BigQuery Production Graph.
      b. Prompts Gemini (gemini-2.5-flash) for JSON guidance grounded strictly in scene tone, location, camera cues.
      c. Upserts director_notes row in BigQuery.
      d. Logs audit event to Production Graph events table.

    Returns:
      Dict with director_note_id, scene_id, shot_suggestions, pacing_notes, and camera_plan.
    """
    graph_client = ProductionGraphClient()

    # a. Fetch scene and location
    scene = graph_client.get_scene(scene_id)
    if not scene:
        raise ValueError(f"Scene with ID '{scene_id}' not found in Production Graph.")

    location_id = scene.get("location_id")
    location = graph_client.get_location(location_id) if location_id else None

    # Extract scene attributes and characters for strict grounding
    emotional_tone = scene.get("emotional_tone", "neutral")
    camera_cues = scene.get("camera_cues") or []
    character_ids = scene.get("character_ids") or []
    characters = [graph_client.get_character(cid) for cid in character_ids if graph_client.get_character(cid)]

    char_details = []
    for c in characters:
        name = c.get("name", "Unknown Character")
        desc = c.get("description", "")
        if desc:
            char_details.append(f"{name} ({desc})")
        else:
            char_details.append(name)
    character_info_str = "; ".join(char_details) if char_details else "None specified"

    location_name = location.get("name", "Unknown Location") if location else "Unknown Location"
    location_type = location.get("location_type", "exterior") if location else "exterior"
    logistics = location.get("logistics_notes", "") if location else ""

    # b. Construct prompt for Gemini
    prompt = f"""
You are an expert film director creating shot suggestions, pacing notes, and a camera plan for a movie production scene.

SCENE DATA:
- Scene ID: {scene_id}
- Location Name: {location_name}
- Location Type: {location_type}
- Location Logistics: {logistics}
- Emotional Tone: {emotional_tone}
- Camera Cues from Script: {camera_cues}
- Characters Present ({len(characters)} total): {character_info_str}

INSTRUCTIONS & CONSTRAINTS:
1. Base all suggestions STRICTLY on the actual scene data provided above (tone, setting, character names, camera cues).
2. Refer to the characters explicitly by their actual names (e.g. {", ".join([c.get("name", "") for c in characters])}) in the shot_suggestions. DO NOT use generic placeholders like "Character 1" or "Character 2".
3. DO NOT invent named lenses, camera model numbers, or specific equipment brand names.
4. DO NOT suggest shot types or camera movements that contradict the location type (e.g. no "sweeping aerial drone shot" or "crane shot" for a cramped interior, or unfeasible setups given logistics).
5. Match the pacing notes and shot framing to the '{emotional_tone}' emotional tone.
6. Return ONLY a valid JSON object matching this schema:
{{
  "shot_suggestions": ["string"],
  "pacing_notes": "string",
  "camera_plan": "string"
}}
"""

    client = genai.Client(vertexai=True, project=ProductionGraphClient.PROJECT, location="us-central1")
    response = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=prompt,
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            temperature=0.2,
        ),
    )

    try:
        data = json.loads(response.text)
    except Exception as exc:
        raise RuntimeError(f"Failed to parse Gemini response as JSON: {response.text}") from exc

    shot_suggestions = data.get("shot_suggestions", [])
    pacing_notes = data.get("pacing_notes", "")
    camera_plan = data.get("camera_plan", "")

    # c. Store result in BigQuery director_notes table
    director_note_id = f"dn_{scene_id}"
    before_state = graph_client.get_director_note(director_note_id) or {}

    note_record = {
        "director_note_id": director_note_id,
        "scene_id": scene_id,
        "shot_suggestions": shot_suggestions,
        "pacing_notes": pacing_notes,
        "camera_plan": camera_plan,
    }

    graph_client.upsert_director_note(note_record)
    after_state = graph_client.get_director_note(director_note_id) or note_record

    # Helper to make dict JSON serializable (handling datetime objects from BigQuery)
    def sanitize_state(state: dict | None) -> dict:
        if not state:
            return {}
        cleaned = {}
        for k, v in state.items():
            if hasattr(v, "isoformat"):
                cleaned[k] = v.isoformat()
            else:
                cleaned[k] = v
        return cleaned

    cleaned_before = sanitize_state(before_state)
    cleaned_after = sanitize_state(after_state)

    # d. Log audit event
    graph_client.log_event(
        actor_agent="director_agent",
        entity_type="director_note",
        entity_id=scene_id,
        before_state=cleaned_before,
        after_state=cleaned_after,
        triggered_agents=[],
    )

    return {
        "director_note_id": director_note_id,
        "scene_id": scene_id,
        "shot_suggestions": shot_suggestions,
        "pacing_notes": pacing_notes,
        "camera_plan": camera_plan,
    }


if __name__ == "__main__":
    result = direct_scene("scene_005")
    print("=" * 80)
    print("DIRECTOR SCENE GUIDANCE GENERATED SUCCESSFULLY")
    print("=" * 80)
    print(f"Scene ID:          {result['scene_id']}")
    print(f"Director Note ID:  {result['director_note_id']}")
    print("Shot Suggestions:")
    for idx, shot in enumerate(result["shot_suggestions"], 1):
        print(f"  {idx}. {shot}")
    print(f"\nPacing Notes:\n  {result['pacing_notes']}")
    print(f"\nCamera Plan:\n  {result['camera_plan']}")
    print("=" * 80)
