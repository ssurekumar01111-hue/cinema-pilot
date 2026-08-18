"""
agents/schedule/agent.py

Schedule Agent — Determines scheduling implications, duration, shoot day index,
and constraints for scenes, writing schedule_blocks records to the Production Graph
and emitting audit events triggering the producer.

Integrates with:
  - ProductionGraphClient (for querying scene/location/schedule_blocks & writing updates)
  - Gemini (for grounded scheduling constraint reasoning based strictly on provided data)

Usage (imported):
    from agents.schedule.agent import reschedule_shoot
    updated_block = reschedule_shoot("scene_005")

Usage (standalone demo):
    python agents/schedule/agent.py
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Repo-root path resolution
# ---------------------------------------------------------------------------
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from google import genai
from google.genai import types

from shared.graph_client import ProductionGraphClient, GraphClientError
from shared.telemetry import instrument_agent

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

GEMINI_MODEL = "gemini-2.5-flash"
GCP_PROJECT  = "cinemapilot-2026"
GCP_LOCATION = "us-central1"

# ---------------------------------------------------------------------------
# Gemini Prompt Template
# ---------------------------------------------------------------------------

SCHEDULE_PROMPT = """\
You are a film production schedule planner.

Determine the shoot duration, recommended shoot day index, and specific scheduling constraints for the scene detailed below based STRICTLY on the provided data.

Data Provided:
- Scene ID: {scene_id}
- Scene Number: {scene_number}
- Timeline Position: {timeline_position}
- Location Name: {location_name}
- Location Type: {location_type}
- Weather Sensitive: {weather_sensitivity}
- Logistics Notes: {logistics_notes}
- Number of Characters in Scene: {num_characters}
- Props Required ({num_props}): {props_list}
- Existing Schedule Blocks: {existing_blocks}

Instructions:
1. Determine the estimated duration in minutes (`duration_minutes`, typically between 60 and 480 minutes depending on complexity).
2. Assign a recommended shoot day index (`day_index`, integer >= 1). By default, `day_index` should match the scene's Timeline Position ({timeline_position}). Only assign a different `day_index` if specific location logistics or constraints (e.g. grouping scenes at the same location or weather/tide window batching) explicitly justify it. If `day_index` differs from Timeline Position ({timeline_position}), you MUST explicitly state the justification in the `reasoning` field.
3. List specific scheduling constraints (`constraints` array of strings).
   CRITICAL: Constraints must cite ONLY real factors present in the Logistics Notes or location properties (such as tide-dependent access windows, filming permit timing, lack of on-site power, or weather sensitivity). Do NOT invent call times, fictional union rules, or unlisted equipment.
4. Provide a brief 1-2 sentence reasoning (`reasoning`) explaining the schedule choices and explicitly explaining any divergence of `day_index` from Timeline Position.
5. Return ONLY valid JSON matching this schema:
{{
  "duration_minutes": <int>,
  "day_index": <int>,
  "constraints": [<string>, ...],
  "reasoning": <string, 1-2 sentences>
}}
"""



# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _build_gemini_client() -> genai.Client:
    """Initialise Gemini Client via ADC / Vertex AI."""
    try:
        return genai.Client(
            vertexai=True,
            project=GCP_PROJECT,
            location=GCP_LOCATION,
        )
    except Exception as exc:
        raise RuntimeError(
            f"Failed to initialise Gemini client (Vertex AI / ADC): {exc}"
        ) from exc


def _strip_code_fences(text: str) -> str:
    """Remove Markdown code fences."""
    text = text.strip()
    fence = re.compile(r"^```(?:json)?\s*\n?(.*?)\n?```\s*$", re.DOTALL)
    match = fence.match(text)
    return match.group(1).strip() if match else text


# ---------------------------------------------------------------------------
# Main Agent Function
# ---------------------------------------------------------------------------

@instrument_agent("schedule_agent")
def reschedule_shoot(scene_id: str, cascade_id: str | None = None) -> dict[str, Any]:
    """
    Determine scheduling implications for a scene and update schedule_blocks.

    Args:
        scene_id: Unique ID of the scene (e.g. "scene_005").
        cascade_id: Optional correlation ID for the multi-agent cascade run.

    Returns:
        The schedule_block dict written to the Production Graph.

    Raises:
        ValueError: If scene or location is missing or JSON response is invalid.
        GraphClientError: On BigQuery read/write errors.
    """
    graph = ProductionGraphClient()

    # 1. Fetch Scene & Location
    scene = graph.get_scene(scene_id)
    if not scene:
        raise ValueError(f"Scene '{scene_id}' not found in Production Graph.")

    location_id = scene.get("location_id")
    if not location_id:
        raise ValueError(f"Scene '{scene_id}' has no location_id assigned.")

    location = graph.get_location(location_id)
    if not location:
        raise ValueError(f"Location '{location_id}' not found in Production Graph.")

    character_ids = scene.get("character_ids") or []
    prop_ids = scene.get("prop_ids") or []

    # 2. Fetch existing schedule blocks for this scene via graph_client method
    existing_blocks = graph.get_schedule_blocks_for_scene(scene_id)
    existing_block_id = f"sb_{scene_id}"
    previous_block = graph.get_schedule_block(existing_block_id)

    def _sanitize_for_json(obj: Any) -> Any:
        if isinstance(obj, dict):
            return {k: _sanitize_for_json(v) for k, v in obj.items()}
        elif isinstance(obj, list):
            return [_sanitize_for_json(v) for v in obj]
        elif hasattr(obj, "isoformat"):
            return obj.isoformat()
        return obj

    cleaned_blocks = _sanitize_for_json(existing_blocks) if existing_blocks else "None"

    # 3. Call Gemini for schedule calculation
    client = _build_gemini_client()
    prompt = SCHEDULE_PROMPT.format(
        scene_id=scene.get("scene_id"),
        scene_number=scene.get("scene_number"),
        timeline_position=scene.get("timeline_position", scene.get("scene_number")),
        location_name=location.get("name", "Unknown Location"),
        location_type=location.get("location_type", "unknown"),
        weather_sensitivity=location.get("weather_sensitivity", False),
        logistics_notes=location.get("logistics_notes") or "None",
        num_characters=len(character_ids),
        num_props=len(prop_ids),
        props_list=", ".join(prop_ids) if prop_ids else "None",
        existing_blocks=json.dumps(cleaned_blocks) if isinstance(cleaned_blocks, (dict, list)) else cleaned_blocks,
    )

    try:
        response = client.models.generate_content(
            model=GEMINI_MODEL,
            contents=prompt,
            config=types.GenerateContentConfig(
                temperature=0.1,
                thinking_config=types.ThinkingConfig(thinking_budget=0),
                response_mime_type="application/json",
            ),
        )
    except Exception as exc:
        raise RuntimeError(f"Gemini schedule planning call failed for {scene_id}: {exc}") from exc

    cleaned_json = _strip_code_fences(response.text)
    try:
        res = json.loads(cleaned_json)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Failed to parse Gemini JSON for {scene_id}: {exc}\nRaw: {response.text}") from exc

    duration_minutes = int(res.get("duration_minutes", 240))
    raw_day = res.get("day_index")
    timeline_pos = int(scene.get("timeline_position", scene.get("scene_number", 1)))

    try:
        parsed_day = int(raw_day)
        if parsed_day < 1:
            print(f"[schedule_agent] WARNING: Invalid day_index ({parsed_day}) < 1 returned. Defaulting to timeline_position ({timeline_pos}).")
            day_index = timeline_pos
        else:
            day_index = parsed_day
    except (TypeError, ValueError):
        print(f"[schedule_agent] WARNING: Non-integer day_index ({raw_day}) returned. Defaulting to timeline_position ({timeline_pos}).")
        day_index = timeline_pos

    constraints = [str(c).strip() for c in res.get("constraints", []) if c]
    reasoning = str(res.get("reasoning", "")).strip()

    if day_index != timeline_pos:
        print(f"[schedule_agent] LOG: day_index ({day_index}) diverges from timeline_position ({timeline_pos}). Stated reasoning: '{reasoning}'")

    # 4. Upsert Schedule Block
    schedule_block_record = {
        "schedule_block_id": existing_block_id,
        "scene_id": scene_id,
        "day_index": day_index,
        "duration_minutes": duration_minutes,
        "constraints": constraints,
    }

    graph.upsert_schedule_block(schedule_block_record)

    before_state = {
        "day_index": previous_block.get("day_index") if previous_block else None,
        "duration_minutes": previous_block.get("duration_minutes") if previous_block else None,
        "constraints": previous_block.get("constraints") if previous_block else [],
    }

    after_state = {
        "day_index": day_index,
        "duration_minutes": duration_minutes,
        "constraints": constraints,
        "reasoning": reasoning,
    }

    # 5. Log audit event (schedule changes affect producer)
    graph.log_event(
        actor_agent="schedule_agent",
        entity_type="schedule_block",
        entity_id=existing_block_id,
        before_state=before_state,
        after_state=after_state,
        triggered_agents=["producer"],
    )

    full_record = graph.get_schedule_block(existing_block_id) or schedule_block_record
    result_dict = dict(full_record)
    result_dict["reasoning"] = reasoning
    return result_dict


# ---------------------------------------------------------------------------
# Standalone Test Harness
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    test_scene_id = "scene_005"

    print("=" * 70)
    print(f"  Schedule Agent — Rescheduling Shoot for {test_scene_id}")
    print("=" * 70)

    result = reschedule_shoot(test_scene_id)

    print("\n[schedule_agent] Rescheduling Complete.")
    print(f"Schedule Block ID : {result.get('schedule_block_id')}")
    print(f"Scene ID          : {result.get('scene_id')}")
    print(f"Day Index         : Day {result.get('day_index')}")
    print(f"Duration (mins)   : {result.get('duration_minutes')} mins")
    print(f"Constraints ({len(result.get('constraints', []))}):")
    for c in result.get("constraints", []):
        print(f"  - {c}")

    print("\nGemini Reasoning:")
    print("-" * 70)
    print(result.get('reasoning'))
    print("-" * 70)
