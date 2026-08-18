"""
agents/producer/agent.py

Producer Agent for CinemaPilot.
Synthesizes executive producer overviews for production scenes using Gemini.
Grounds summary, total budget impact, schedule status, outstanding risks, and recommendations
strictly in real Production Graph data (budget lines, schedule blocks, risk flags, location info).
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


@instrument_agent("producer_agent")
def producer_overview(scene_id: str, cascade_id: str | None = None) -> dict[str, Any]:
    """
    Generate an executive producer overview for a scene ID using Gemini.

    Args:
      scene_id: Unique scene identifier (e.g. "scene_005").
      cascade_id: Optional correlation ID for the multi-agent cascade run.

    Steps:
      a. Fetches scene, location, budget_lines, schedule_blocks, and risk_flags.
      b. Prompts Gemini (gemini-2.5-flash) for JSON synthesis grounded strictly in fetched data.
      c. Stores result in BigQuery producer_overviews table.
      d. Logs audit event to Production Graph events table.

    Returns:
      Dict with producer_overview_id, scene_id, overview_summary, total_budget_impact,
      schedule_status, outstanding_risks, and recommendation.
    """
    graph_client = ProductionGraphClient()

    # a. Fetch scene and location
    scene = graph_client.get_scene(scene_id)
    if not scene:
        raise ValueError(f"Scene with ID '{scene_id}' not found in Production Graph.")

    location_id = scene.get("location_id")
    location = graph_client.get_location(location_id) if location_id else None

    # Fetch linked budget lines
    budget_lines = graph_client.get_budget_lines_for_entity(scene_id)
    total_budget_impact = sum(bl.get("amount", 0.0) for bl in budget_lines)

    budget_details = [
        f"- Budget Line '{bl.get('budget_line_id')}': ${bl.get('amount', 0.0):,.2f} ({bl.get('category')}) - {bl.get('reason')}"
        for bl in budget_lines
    ]
    budget_str = "\n".join(budget_details) if budget_details else "No specific budget lines recorded."

    # Fetch linked schedule blocks
    schedule_blocks = graph_client.get_schedule_blocks_for_scene(scene_id)
    schedule_details = [
        f"- Block '{sb.get('schedule_block_id')}': Day {sb.get('day_index')}, {sb.get('duration_minutes')} minutes. Constraints: {sb.get('constraints')}"
        for sb in schedule_blocks
    ]
    schedule_str = "\n".join(schedule_details) if schedule_details else "No schedule blocks assigned."

    # Fetch all risk flags for location
    risk_flags = graph_client.get_risk_flags_for_entity(location_id) if location_id else []
    risk_details = []
    for rf in risk_flags:
        status_str = "MITIGATED" if rf.get("mitigation") and rf.get("mitigation").strip() else "UNMITIGATED"
        risk_details.append(
            f"- Risk '{rf.get('risk_flag_id')}' (Severity {rf.get('severity')}): {rf.get('description')} [{status_str}]. Mitigation: '{rf.get('mitigation')}'"
        )
    risk_str = "\n".join(risk_details) if risk_details else "No risk flags recorded for location."

    # b. Construct prompt for Gemini
    prompt = f"""
You are an Executive Producer creating a high-level producer overview for a movie production scene.

FETCHED PRODUCTION GRAPH DATA:
- Scene ID: {scene_id} (Number: {scene.get('scene_number')}, Emotional Tone: {scene.get('emotional_tone')})
- Location: {location.get('name') if location else 'Unknown'} ({location.get('location_type') if location else ''})
- Budget Impact: Total calculated = ${total_budget_impact:,.2f}
  Detailed Budget Lines:
{budget_str}

- Schedule Info:
{schedule_str}

- Location Risk Flags:
{risk_str}

INSTRUCTIONS & CONSTRAINTS:
1. Base your summary, total_budget_impact, schedule_status, outstanding_risks, and recommendation STRICTLY on the actual fetched data above.
2. DO NOT invent dollar figures not present in the fetched budget lines.
3. DO NOT invent risks beyond what is recorded in the risk_flags list.
4. Note that if a risk flag has a recorded mitigation (status [MITIGATED]), it is considered MITIGATED and should NOT be listed as an outstanding unmitigated risk in "outstanding_risks". Only unmitigated risks belong in "outstanding_risks".
5. Ensure total_budget_impact in the output JSON matches the exact total budget calculated from the budget lines ({total_budget_impact}).
6. Keep recommendations grounded in the data (e.g. do not recommend fixing a risk if it has already been mitigated).
7. Return ONLY a valid JSON object matching this schema:
{{
  "overview_summary": "string",
  "total_budget_impact": {total_budget_impact},
  "schedule_status": "string",
  "outstanding_risks": ["string"],
  "recommendation": "string"
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

    overview_summary = data.get("overview_summary", "")
    returned_budget = float(data.get("total_budget_impact", total_budget_impact))
    schedule_status = data.get("schedule_status", "")
    outstanding_risks = data.get("outstanding_risks", [])
    recommendation = data.get("recommendation", "")

    # c. Store in BigQuery producer_overviews table
    producer_overview_id = f"po_{scene_id}"
    before_state = graph_client.get_producer_overview(producer_overview_id) or {}

    overview_record = {
        "producer_overview_id": producer_overview_id,
        "scene_id": scene_id,
        "overview_summary": overview_summary,
        "total_budget_impact": returned_budget,
        "schedule_status": schedule_status,
        "outstanding_risks": outstanding_risks,
        "recommendation": recommendation,
    }

    graph_client.upsert_producer_overview(overview_record)
    after_state = graph_client.get_producer_overview(producer_overview_id) or overview_record

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
        actor_agent="producer_agent",
        entity_type="producer_overview",
        entity_id=scene_id,
        before_state=cleaned_before,
        after_state=cleaned_after,
        triggered_agents=[],
    )

    return {
        "producer_overview_id": producer_overview_id,
        "scene_id": scene_id,
        "overview_summary": overview_summary,
        "total_budget_impact": returned_budget,
        "schedule_status": schedule_status,
        "outstanding_risks": outstanding_risks,
        "recommendation": recommendation,
    }


if __name__ == "__main__":
    result = producer_overview("scene_005")
    print("=" * 80)
    print("PRODUCER OVERVIEW GENERATED SUCCESSFULLY")
    print("=" * 80)
    print(f"Scene ID:               {result['scene_id']}")
    print(f"Producer Overview ID:   {result['producer_overview_id']}")
    print(f"Total Budget Impact:    ${result['total_budget_impact']:,.2f}")
    print(f"Schedule Status:        {result['schedule_status']}")
    print(f"Outstanding Risks:      {result['outstanding_risks']}")
    print(f"\nOverview Summary:\n  {result['overview_summary']}")
    print(f"\nRecommendation:\n  {result['recommendation']}")
    print("=" * 80)
