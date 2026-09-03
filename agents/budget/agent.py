"""
agents/budget/agent.py

Budget Agent — Calculates and updates line item costs for scenes based on
location cost profiles, character counts, prop logistics, and weather sensitivity.

Integrates with:
  - ProductionGraphClient (for reading scene/location & writing budget_lines/events)
  - Gemini (for grounded reasoning & cost estimation based strictly on provided data)

Usage (imported):
    from agents.budget.agent import recalculate_budget
    result = recalculate_budget("scene_005")

Usage (standalone demo):
    python agents/budget/agent.py
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
from shared.telemetry import instrument_agent, record_budget_delta

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

GEMINI_MODEL = "gemini-2.5-flash"
GCP_PROJECT  = "cinemapilot-2026"
GCP_LOCATION = "us-central1"

# ---------------------------------------------------------------------------
# Gemini Prompt Template
# ---------------------------------------------------------------------------

BUDGET_PROMPT = """\
You are a film production budget estimator.

Calculate the estimated location budget for the scene detailed below based STRICTLY on the provided data.

Data Provided:
- Scene ID: {scene_id}
- Scene Number: {scene_number}
- Location Name: {location_name}
- Location Type: {location_type}
- Weather Sensitive: {weather_sensitivity}
- Base Location Cost Profile: ${cost_profile:.2f}
- Logistics Notes: {logistics_notes}
- Number of Characters in Scene: {num_characters}
- Props Required ({num_props}): {props_list}

Instructions:
1. Provide a brief 2-3 sentence reasoning explaining the estimated cost.
   CRITICAL: Explicitly reference specific logistics factors from the Logistics Notes (such as permits, power constraints, or access windows) and location properties (location type, weather sensitivity, character/prop requirements) to justify the cost. Do NOT invent external details not provided above.
2. Estimate the total location budget amount for filming this scene: calculate this as the Base Location Cost Profile plus a standard $1,000.00 logistics contingency if external site constraints (such as permits, power requirements, access windows, or weather sensitivity) are present; otherwise use the base cost profile.
3. Return ONLY valid JSON matching this schema:
{{
  "reasoning": <string, 2-3 sentences>,
  "estimated_cost": <float>
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

@instrument_agent("budget_agent")
def recalculate_budget(scene_id: str, cascade_id: str | None = None) -> dict[str, Any]:
    """
    Recalculate the location budget line item for a given scene.

    Args:
        scene_id: Unique ID of the scene (e.g. "scene_005").
        cascade_id: Optional correlation ID for the multi-agent cascade run.

    Returns:
        The budget_line dict written to the Production Graph.

    Raises:
        ValueError: If scene or location is not found, or response is invalid JSON.
        GraphClientError: On BigQuery read/write errors.
    """
    graph = ProductionGraphClient()

    # 1. Fetch Scene
    scene = graph.get_scene(scene_id)
    if not scene:
        raise ValueError(f"Scene '{scene_id}' not found in Production Graph.")

    location_id = scene.get("location_id")
    location = None
    if location_id:
        location = graph.get_location(location_id)

    # Fallback default location info if location record doesn't exist yet
    location_name = location.get("name") if location else (location_id or "Unknown Location")
    location_type = location.get("location_type") if location else ("exterior" if "beach" in location_name.lower() or "outdoor" in location_name.lower() else "interior")
    weather_sensitivity = location.get("weather_sensitivity") if location else (location_type == "exterior")
    cost_profile = float(location.get("cost_profile") or 0.0) if location else (3500.0 if location_type == "exterior" else 850.0)
    logistics_notes = location.get("logistics_notes") if location and location.get("logistics_notes") else "None"

    character_ids = scene.get("character_ids") or []
    prop_ids = scene.get("prop_ids") or []

    # 2. Call Gemini for cost estimation & reasoning
    client = _build_gemini_client()
    prompt = BUDGET_PROMPT.format(
        scene_id=scene.get("scene_id"),
        scene_number=scene.get("scene_number"),
        location_name=location_name,
        location_type=location_type,
        weather_sensitivity=weather_sensitivity,
        cost_profile=cost_profile,
        logistics_notes=logistics_notes,
        num_characters=len(character_ids),
        num_props=len(prop_ids),
        props_list=", ".join(prop_ids) if prop_ids else "None",
    )

    try:
        response = client.models.generate_content(
            model=GEMINI_MODEL,
            contents=prompt,
            config=types.GenerateContentConfig(
                temperature=0.0,
                thinking_config=types.ThinkingConfig(thinking_budget=0),
                response_mime_type="application/json",
            ),
        )
    except Exception as exc:
        raise RuntimeError(f"Gemini estimation call failed: {exc}") from exc

    cleaned_json = _strip_code_fences(response.text)
    try:
        res = json.loads(cleaned_json)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Failed to parse Gemini JSON response: {exc}\nRaw: {response.text}") from exc

    estimated_cost = float(res.get("estimated_cost", 0.0))
    reasoning = str(res.get("reasoning", "")).strip()

    # 3. Check previous budget line for diffing/audit
    budget_line_id = f"bl_location_{scene_id}"
    previous_line = graph.get_budget_line(budget_line_id)

    before_state = {
        "amount": previous_line.get("amount") if previous_line else None,
        "reason": previous_line.get("reason") if previous_line else None,
    }

    budget_record = {
        "budget_line_id": budget_line_id,
        "category": "location",
        "amount": estimated_cost,
        "linked_entity_id": scene_id,
        "last_changed_by_agent": "budget_agent",
        "reason": reasoning,
    }

    # 4. Upsert Budget Line
    graph.upsert_budget_line(budget_record)

    previous_amount = float(previous_line.get("amount") or 0.0) if previous_line else 0.0
    cost_delta = estimated_cost - previous_amount
    record_budget_delta(cascade_id or "standalone", scene_id, cost_delta, category="location")

    after_state = {
        "amount": estimated_cost,
        "reason": reasoning,
    }

    # 5. Log audit event (budget_line changes trigger producer)
    graph.log_event(
        actor_agent="budget_agent",
        entity_type="budget_line",
        entity_id=budget_line_id,
        before_state=before_state,
        after_state=after_state,
        triggered_agents=["producer"],
    )

    return graph.get_budget_line(budget_line_id) or budget_record


# ---------------------------------------------------------------------------
# Standalone Test Harness
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    test_scene_id = "scene_005"

    print("=" * 70)
    print(f"  Budget Agent — Recalculating Budget for {test_scene_id}")
    print("=" * 70)

    result = recalculate_budget(test_scene_id)

    print("\n[budget_agent] Recalculation Complete.")
    print(f"Budget Line ID : {result.get('budget_line_id')}")
    print(f"Category       : {result.get('category')}")
    print(f"Scene ID       : {result.get('linked_entity_id')}")
    print(f"Estimated Cost : ${result.get('amount'):,.2f}")
    reason_text = result.get('reason') or result.get('reasoning')
    print(f"Version        : {result.get('version')}")
    print("\nGemini Reasoning:")
    print("-" * 70)
    print(reason_text)
    print("-" * 70)
