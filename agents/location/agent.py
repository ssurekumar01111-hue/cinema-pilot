"""
agents/location/agent.py

Location Agent — Evaluates location feasibility, logistics constraints,
and risk level for scenes, writing risk flags when high/medium risk constraints exist.

Integrates with:
  - ProductionGraphClient (for reading scene/location & writing risk_flags/events)
  - Gemini (for grounded logistics assessment based strictly on provided data)

Usage (imported):
    from agents.location.agent import assess_location
    result = assess_location("scene_005")

Usage (standalone demo):
    python agents/location/agent.py
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

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

GEMINI_MODEL = "gemini-2.5-flash"
GCP_PROJECT  = "cinemapilot-2026"
GCP_LOCATION = "us-central1"

# ---------------------------------------------------------------------------
# Gemini Prompt Template
# ---------------------------------------------------------------------------

LOCATION_ASSESSMENT_PROMPT = """\
You are a film location manager assessing logistics feasibility and risk for a scene location.

Evaluate the location logistics detailed below based STRICTLY on the provided data.

Data Provided:
- Scene ID: {scene_id}
- Scene Number: {scene_number}
- Location ID: {location_id}
- Location Name: {location_name}
- Location Type: {location_type}
- Weather Sensitive: {weather_sensitivity}
- Base Cost Profile: ${cost_profile:.2f}
- Logistics Notes: {logistics_notes}
- Number of Characters in Scene: {num_characters}
- Props Required ({num_props}): {props_list}

Instructions:
1. Provide a concise logistics summary of filming at this location.
2. Determine the risk_level ("low", "medium", or "high"). High or medium risk should be assigned if there are notable logistical constraints (e.g., weather sensitivity, tide windows, power constraints, or permit requirements).
3. Provide a brief 1-2 sentence risk_reason.
   CRITICAL: Only cite logistics factors that actually appear in Logistics Notes or the other real fields provided above (e.g., weather sensitivity, permit requirements, tide access, power constraints, or prop/character load). Do NOT invent external details (such as specific municipal ordinance numbers, unlisted equipment names, or specific crew counts).
4. Set requires_risk_flag to true if risk_level is "medium" or "high", otherwise false.
5. Return ONLY valid JSON matching this schema:
{{
  "logistics_summary": <string>,
  "risk_level": <"low" | "medium" | "high">,
  "risk_reason": <string>,
  "requires_risk_flag": <boolean>
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

def assess_location(scene_id: str) -> dict[str, Any]:
    """
    Assess location logistics and risks for a given scene.

    Args:
        scene_id: Unique ID of the scene (e.g. "scene_005").

    Returns:
        The assessment dict including logistics_summary, risk_level, risk_reason, requires_risk_flag.

    Raises:
        ValueError: If scene or location is missing or JSON response is invalid.
        GraphClientError: On BigQuery read/write errors.
    """
    graph = ProductionGraphClient()

    # 1. Fetch Scene
    scene = graph.get_scene(scene_id)
    if not scene:
        raise ValueError(f"Scene '{scene_id}' not found in Production Graph.")

    location_id = scene.get("location_id")
    if not location_id:
        raise ValueError(f"Scene '{scene_id}' does not have a location_id assigned.")

    location = graph.get_location(location_id)
    if not location:
        raise ValueError(f"Location '{location_id}' not found in Production Graph.")

    character_ids = scene.get("character_ids") or []
    prop_ids = scene.get("prop_ids") or []

    # 2. Call Gemini for assessment
    client = _build_gemini_client()
    prompt = LOCATION_ASSESSMENT_PROMPT.format(
        scene_id=scene.get("scene_id"),
        scene_number=scene.get("scene_number"),
        location_id=location.get("location_id"),
        location_name=location.get("name", "Unknown"),
        location_type=location.get("location_type", "unknown"),
        weather_sensitivity=location.get("weather_sensitivity", False),
        cost_profile=float(location.get("cost_profile") or 0.0),
        logistics_notes=location.get("logistics_notes") or "None",
        num_characters=len(character_ids),
        num_props=len(prop_ids),
        props_list=", ".join(prop_ids) if prop_ids else "None",
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
        raise RuntimeError(f"Gemini location assessment call failed: {exc}") from exc

    cleaned_json = _strip_code_fences(response.text)
    try:
        assessment = json.loads(cleaned_json)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Failed to parse Gemini JSON response: {exc}\nRaw: {response.text}") from exc

    logistics_summary = str(assessment.get("logistics_summary", "")).strip()
    risk_level = str(assessment.get("risk_level", "low")).lower().strip()
    risk_reason = str(assessment.get("risk_reason", "")).strip()
    requires_risk_flag = bool(assessment.get("requires_risk_flag", False))

    if risk_level not in ("low", "medium", "high"):
        risk_level = "medium" if requires_risk_flag else "low"

    # 3. Write Risk Flag if required
    risk_flag_id = None
    if requires_risk_flag:
        risk_flag_id = f"rf_loc_{location_id}"
        risk_record = {
            "risk_flag_id": risk_flag_id,
            "linked_entity_id": location_id,
            "severity": risk_level,
            "description": risk_reason,
            "mitigation": "",  # Mitigation left for Risk Agent
        }
        graph.upsert_risk_flag(risk_record)

    # 4. Log audit event
    triggered_agents = ["risk"] if requires_risk_flag else []
    after_state = {
        "logistics_summary": logistics_summary,
        "risk_level": risk_level,
        "risk_reason": risk_reason,
        "requires_risk_flag": requires_risk_flag,
        "risk_flag_id": risk_flag_id,
    }

    graph.log_event(
        actor_agent="location_agent",
        entity_type="location",
        entity_id=location_id,
        before_state={},
        after_state=after_state,
        triggered_agents=triggered_agents,
    )

    assessment_result = {
        "location_id": location_id,
        "location_name": location.get("name"),
        "logistics_summary": logistics_summary,
        "risk_level": risk_level,
        "risk_reason": risk_reason,
        "requires_risk_flag": requires_risk_flag,
        "risk_flag_id": risk_flag_id,
        "triggered_agents": triggered_agents,
    }

    return assessment_result


# ---------------------------------------------------------------------------
# Standalone Test Harness
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    test_scene_id = "scene_005"

    print("=" * 70)
    print(f"  Location Agent — Assessing Location for {test_scene_id}")
    print("=" * 70)

    result = assess_location(test_scene_id)

    print("\n[location_agent] Assessment Complete.")
    print(f"Location ID        : {result.get('location_id')}")
    print(f"Location Name      : {result.get('location_name')}")
    print(f"Risk Level         : {result.get('risk_level').upper()}")
    print(f"Requires Risk Flag : {result.get('requires_risk_flag')}")
    print(f"Risk Flag ID       : {result.get('risk_flag_id')}")
    print(f"Triggered Agents   : {result.get('triggered_agents')}")
    print("\nLogistics Summary:")
    print("-" * 70)
    print(result.get('logistics_summary'))
    print("-" * 70)
    print("\nRisk Reason:")
    print("-" * 70)
    print(result.get('risk_reason'))
    print("-" * 70)
