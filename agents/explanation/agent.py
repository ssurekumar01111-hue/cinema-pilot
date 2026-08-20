"""
agents/explanation/agent.py

Explanation Agent for CinemaPilot.
Synthesizes transparent, producer-facing narratives explaining production graph changes for a scene.
Strictly synthesizes ONLY from existing agent reasoning strings (Budget, Location, Risk, Schedule, Director, Producer).
Never introduces new unverified figures, risks, or unlogged assumptions.
Stores narrative in BigQuery explanations table and logs audit events.
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


@instrument_agent("explanation_agent")
def explain_change(scene_id: str, cascade_id: str | None = None) -> dict[str, Any]:
    """
    Synthesize a producer-facing narrative explaining changes for a scene ID.

    Args:
      scene_id: Unique scene identifier (e.g. "scene_005").
      cascade_id: Optional correlation ID for the multi-agent cascade run.

    Steps:
      a. Fetches logged reasoning produced by other agents for this scene & location:
         - Location: base cost profile & logistics notes
         - Budget: budget line amount and logged reasoning
         - Schedule: schedule block day index, duration, and constraints
         - Risk: risk flag severity, description, and mitigation
         - Director: pacing notes and camera plan
         - Producer: overview summary, total budget impact, schedule status, recommendation
      b. Uses Gemini (gemini-2.5-flash) to synthesize ONLY from these source texts into a plain-language narrative.
      c. Returns narrative and list of sources used.
      d. Stores in BigQuery explanations table and logs audit event.

    Returns:
      Dict with explanation_id, scene_id, narrative, and sources_used.
    """
    graph_client = ProductionGraphClient()

    # a. Gather logged reasoning strings from source agents
    scene = graph_client.get_scene(scene_id)
    if not scene:
        raise ValueError(f"Scene with ID '{scene_id}' not found in Production Graph.")

    location_id = scene.get("location_id")
    location = graph_client.get_location(location_id) if location_id else None

    budget_lines = graph_client.get_budget_lines_for_entity(scene_id)
    schedule_blocks = graph_client.get_schedule_blocks_for_scene(scene_id)
    risk_flags = graph_client.get_risk_flags_for_entity(location_id) if location_id else []
    director_note = graph_client.get_director_note(f"dn_{scene_id}")
    producer_overview = graph_client.get_producer_overview(f"po_{scene_id}")

    # Build structured source reasoning catalog
    sources_catalog = {}

    if location:
        sources_catalog["location_agent"] = (
            f"Location '{location.get('name')}' (base cost profile: ${location.get('cost_profile', 0.0):,.2f}): "
            f"Logistics notes: '{location.get('logistics_notes')}'. Weather sensitive: {location.get('weather_sensitivity')}."
        )

    if budget_lines:
        b_texts = [
            f"Budget Line (Amount: ${bl.get('amount', 0.0):,.2f}, Category: {bl.get('category')}): Logged Reason: '{bl.get('reason')}'"
            for bl in budget_lines
        ]
        sources_catalog["budget_agent"] = "\n".join(b_texts)

    if schedule_blocks:
        s_texts = [
            f"Schedule Block: Day {sb.get('day_index')}, Duration: {sb.get('duration_minutes')} minutes. Constraints: {sb.get('constraints')}"
            for sb in schedule_blocks
        ]
        sources_catalog["schedule_agent"] = "\n".join(s_texts)

    if risk_flags:
        r_texts = [
            f"Risk Flag (Severity: {rf.get('severity')}): Description: '{rf.get('description')}'. Mitigation Plan: '{rf.get('mitigation')}'"
            for rf in risk_flags
        ]
        sources_catalog["risk_agent"] = "\n".join(r_texts)

    if director_note:
        sources_catalog["director_agent"] = (
            f"Pacing Notes: '{director_note.get('pacing_notes')}'. Camera Plan: '{director_note.get('camera_plan')}'."
        )

    if producer_overview:
        sources_catalog["producer_agent"] = (
            f"Producer Overview Summary: '{producer_overview.get('overview_summary')}'. "
            f"Total Budget Impact: ${producer_overview.get('total_budget_impact', 0.0):,.2f}. "
            f"Schedule Status: '{producer_overview.get('schedule_status')}'. "
            f"Recommendation: '{producer_overview.get('recommendation')}'."
        )

    sources_formatted = "\n\n".join(f"[{agent_name}]:\n{reasoning}" for agent_name, reasoning in sources_catalog.items())

    # b. Prompt Gemini for strict synthesis
    prompt = f"""
You are an Explanation Agent responsible for synthesizing a transparent, executive-level narrative for a movie producer.

SOURCE AGENT REASONING LOGS:
{sources_formatted}

STRICT CONSTRAINTS & INSTRUCTIONS:
1. Synthesize a single, clear, producer-facing narrative explaining what changed and why for Scene '{scene_id}'.
2. Ground EVERY single statement, figure, and claim STRICTLY in the source reasoning texts provided above.
3. DO NOT introduce any new facts, numbers, figures, risks, or assumptions not explicitly stated in the source text.
   - For example: if citing baseline cost ($850.00), relocated total ($4,500.00), or net budget delta (+$3,650.00), use the exact figures from the source notes.
   - Trace costs, permits, weather/tide constraints, power generation, pacing, and risk mitigations back to the source agent notes.
4. List in "sources_used" the exact list of agent names whose notes were incorporated in your narrative (options: "location_agent", "budget_agent", "schedule_agent", "risk_agent", "director_agent", "producer_agent").
5. Return ONLY a valid JSON object matching this schema:
{{
  "narrative": "string",
  "sources_used": ["string"]
}}
"""

    client = genai.Client(vertexai=True, project=ProductionGraphClient.PROJECT, location="us-central1")
    response = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=prompt,
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            temperature=0.1,
        ),
    )

    try:
        data = json.loads(response.text)
    except Exception as exc:
        raise RuntimeError(f"Failed to parse Gemini response as JSON: {response.text}") from exc

    narrative = data.get("narrative", "")
    sources_used = data.get("sources_used", list(sources_catalog.keys()))

    # c. Store in BigQuery explanations table
    explanation_id = f"exp_{scene_id}"
    before_state = graph_client.get_explanation(explanation_id) or {}

    exp_record = {
        "explanation_id": explanation_id,
        "scene_id": scene_id,
        "narrative": narrative,
        "sources_used": sources_used,
    }

    graph_client.upsert_explanation(exp_record)
    after_state = graph_client.get_explanation(explanation_id) or exp_record

    # Helper to serialize state for audit logging
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
        actor_agent="explanation_agent",
        entity_type="explanation",
        entity_id=scene_id,
        before_state=cleaned_before,
        after_state=cleaned_after,
        triggered_agents=[],
    )

    return {
        "explanation_id": explanation_id,
        "scene_id": scene_id,
        "narrative": narrative,
        "sources_used": sources_used,
    }


if __name__ == "__main__":
    result = explain_change("scene_005")
    print("=" * 80)
    print("EXPLANATION NARRATIVE GENERATED SUCCESSFULLY")
    print("=" * 80)
    print(f"Scene ID:        {result['scene_id']}")
    print(f"Explanation ID:  {result['explanation_id']}")
    print(f"Sources Used:    {result['sources_used']}")
    print(f"\nNarrative:\n{result['narrative']}")
    print("=" * 80)
