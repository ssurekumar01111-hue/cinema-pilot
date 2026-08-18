"""
agents/casting/agent.py

Casting Agent for CinemaPilot.
Generates structured character sheets for production characters using Gemini,
grounded strictly in real character details (name, description, costume_notes)
and actual scene appearances (location context, emotional tone, scene count).
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
from shared.telemetry import instrument_agent


@instrument_agent("casting_agent")
def generate_character_sheet(character_id: str, cascade_id: str | None = None) -> dict[str, Any]:
    """
    Generate a structured character sheet for a given character ID using Gemini.

    Args:
      character_id: Unique character identifier (e.g. "char_dr_nadia_voss").
      cascade_id: Optional correlation ID for the multi-agent cascade run.

    Steps:
      a. Fetches character record via get_character(character_id).
      b. Fetches all scenes the character appears in via get_scenes_for_character(character_id).
      c. Uses Gemini to produce a structured character sheet grounded strictly in real details.
      d. Stores result in BigQuery character_sheets table via upsert_character_sheet().
      e. Logs audit event to Production Graph events table.

    Returns:
      Dict with character_sheet_id, character_id, summary, personality_notes,
      costume_considerations, and scene_count.
    """
    graph_client = ProductionGraphClient()

    # a. Fetch character
    character = graph_client.get_character(character_id)
    if not character:
        raise ValueError(f"Character with ID '{character_id}' not found in Production Graph.")

    name = character.get("name", "Unknown Character")
    description = character.get("description", "")
    costume_notes = character.get("costume_notes") or []

    # b. Fetch scenes where character appears
    scenes = graph_client.get_scenes_for_character(character_id)
    scene_count = len(scenes)

    scene_summaries = []
    for s in scenes:
        sid = s.get("scene_id", "")
        tone = s.get("emotional_tone", "unspecified")
        loc_id = s.get("location_id", "")
        loc = graph_client.get_location(loc_id) if loc_id else None
        loc_name = loc.get("name", loc_id) if loc else loc_id
        loc_type = loc.get("location_type", "") if loc else ""
        scene_summaries.append(
            f"Scene {sid} (Location: {loc_name} [{loc_type}], Emotional Tone: {tone})"
        )

    scenes_info_str = "\n".join(scene_summaries) if scene_summaries else "No assigned scenes."

    # c. Construct prompt for Gemini
    prompt = f"""
You are an expert casting director and costume designer creating a character sheet for a film production.

CHARACTER DATA:
- Character ID: {character_id}
- Name: {name}
- Description: {description}
- Existing Costume Notes: {costume_notes}

SCENE APPEARANCES ({scene_count} total):
{scenes_info_str}

INSTRUCTIONS & CONSTRAINTS:
1. Base all character sheet details STRICTLY on the actual character description and real scene appearance data provided above.
2. DO NOT invent specific costume brand names, clothing sizes, unstated backstories, or unmentioned personal histories.
3. Base costume_considerations strictly on existing costume notes and the setting/tone of the scenes (e.g. weather/environment/tone of the locations).
4. Return ONLY a valid JSON object matching this schema:
{{
  "summary": "string",
  "personality_notes": "string",
  "costume_considerations": "string",
  "scene_count": {scene_count}
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

    summary = data.get("summary", "")
    personality_notes = data.get("personality_notes", "")
    costume_considerations = data.get("costume_considerations", "")

    # d. Store in BigQuery character_sheets table
    character_sheet_id = f"cs_{character_id}"
    before_state = graph_client.get_character_sheet(character_sheet_id) or {}

    sheet_record = {
        "character_sheet_id": character_sheet_id,
        "character_id": character_id,
        "summary": summary,
        "personality_notes": personality_notes,
        "costume_considerations": costume_considerations,
        "scene_count": scene_count,
    }

    graph_client.upsert_character_sheet(sheet_record)
    after_state = graph_client.get_character_sheet(character_sheet_id) or sheet_record

    # Helper to serialize datetime objects for audit logging
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

    # e. Log audit event
    graph_client.log_event(
        actor_agent="casting_agent",
        entity_type="character_sheet",
        entity_id=character_id,
        before_state=cleaned_before,
        after_state=cleaned_after,
        triggered_agents=[],
    )

    return {
        "character_sheet_id": character_sheet_id,
        "character_id": character_id,
        "summary": summary,
        "personality_notes": personality_notes,
        "costume_considerations": costume_considerations,
        "scene_count": scene_count,
    }


if __name__ == "__main__":
    result = generate_character_sheet("char_felix_crane")
    print("=" * 80)
    print("CHARACTER SHEET GENERATED SUCCESSFULLY")
    print("=" * 80)
    print(f"Character ID:       {result['character_id']}")
    print(f"Character Sheet ID: {result['character_sheet_id']}")
    print(f"Scene Count:        {result['scene_count']}")
    print(f"\nSummary:\n  {result['summary']}")
    print(f"\nPersonality Notes:\n  {result['personality_notes']}")
    print(f"\nCostume Considerations:\n  {result['costume_considerations']}")
    print("=" * 80)
