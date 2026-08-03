"""
agents/risk/agent.py

Risk Agent — Proposes concrete, realistic mitigation strategies for unmitigated
risk flags linked to locations or scenes, updating the Production Graph risk_flags
table and emitting audit events.

Integrates with:
  - ProductionGraphClient (for querying unmitigated risk flags & updating mitigations)
  - Gemini (for generating realistic, production-grounded mitigations without inventing vendors/costs)

Usage (imported):
    from agents.risk.agent import mitigate_risks
    updated_flags = mitigate_risks("loc_sunset_beach")

Usage (standalone demo):
    python agents/risk/agent.py
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

RISK_MITIGATION_PROMPT = """\
You are a film production risk manager.

Propose a concrete, actionable mitigation strategy for the identified production risk flag detailed below.

Location Details:
- Location Name: {location_name}
- Location Type: {location_type}
- Weather Sensitive: {weather_sensitivity}
- Logistics Notes: {logistics_notes}

Risk Flag Details:
- Severity: {severity}
- Risk Description: {description}

Instructions:
1. Propose a realistic, actionable mitigation strategy (1-2 clear sentences) addressing the specific risks described (e.g. scheduling around tide windows, securing power generators, securing permits in advance, or scheduling rain contingency days).
2. CRITICAL: Do NOT invent specific vendor company names, exact monetary dollar amounts, or fictional municipal regulations. Keep the mitigation focused on standard film production practices.
3. Return ONLY valid JSON matching this schema:
{{
  "mitigation": <string, 1-2 sentences>
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

def mitigate_risks(location_id: str) -> list[dict[str, Any]]:
    """
    Find unmitigated risk flags linked to location_id and generate mitigations for them.

    Args:
        location_id: Unique ID of the location (e.g. "loc_sunset_beach").

    Returns:
        List of updated risk flag dicts containing the newly generated mitigations.

    Raises:
        ValueError: If location is missing or JSON response is invalid.
        GraphClientError: On BigQuery read/write errors.
    """
    graph = ProductionGraphClient()

    # 1. Fetch Location
    location = graph.get_location(location_id)
    if not location:
        raise ValueError(f"Location '{location_id}' not found in Production Graph.")

    # 2. Fetch unmitigated risk flags for this location using method on graph_client
    unmitigated_flags = graph.get_unmitigated_risk_flags_for_entity(location_id)
    if not unmitigated_flags:
        print(f"[risk_agent] No unmitigated risk flags found for location '{location_id}'.")
        return []

    print(f"[risk_agent] Found {len(unmitigated_flags)} unmitigated risk flag(s) for '{location_id}'.")

    client = _build_gemini_client()
    updated_records: list[dict[str, Any]] = []

    # 3. Process each unmitigated risk flag
    for flag in unmitigated_flags:
        flag_id = flag["risk_flag_id"]
        severity = flag.get("severity", "medium")
        description = flag.get("description", "")

        prompt = RISK_MITIGATION_PROMPT.format(
            location_name=location.get("name", "Unknown Location"),
            location_type=location.get("location_type", "unknown"),
            weather_sensitivity=location.get("weather_sensitivity", False),
            logistics_notes=location.get("logistics_notes") or "None",
            severity=severity,
            description=description,
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
            raise RuntimeError(f"Gemini risk mitigation call failed for {flag_id}: {exc}") from exc

        cleaned_json = _strip_code_fences(response.text)
        try:
            res = json.loads(cleaned_json)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Failed to parse Gemini JSON for {flag_id}: {exc}\nRaw: {response.text}") from exc

        mitigation_text = str(res.get("mitigation", "")).strip()

        # Update risk_flag record
        updated_flag_record = {
            "risk_flag_id": flag_id,
            "linked_entity_id": location_id,
            "severity": severity,
            "description": description,
            "mitigation": mitigation_text,
        }

        graph.upsert_risk_flag(updated_flag_record)

        # Log audit event (risk mitigation alerts producer)
        graph.log_event(
            actor_agent="risk_agent",
            entity_type="risk_flag",
            entity_id=flag_id,
            before_state={"mitigation": ""},
            after_state={"mitigation": mitigation_text},
            triggered_agents=["producer"],
        )

        full_record = graph.get_risk_flag(flag_id) or updated_flag_record
        updated_records.append(full_record)
        print(f"  + [risk_agent] Updated {flag_id} (severity={severity})")

    return updated_records


# ---------------------------------------------------------------------------
# Standalone Test Harness
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    test_location_id = "loc_sunset_beach"

    print("=" * 70)
    print(f"  Risk Agent — Mitigating Risks for {test_location_id}")
    print("=" * 70)

    results = mitigate_risks(test_location_id)

    print(f"\n[risk_agent] Processing Complete ({len(results)} risk flags updated).\n")
    for r in results:
        print(f"Risk Flag ID  : {r.get('risk_flag_id')}")
        print(f"Entity Linked : {r.get('linked_entity_id')}")
        print(f"Severity      : {str(r.get('severity')).upper()}")
        print(f"Description   : {r.get('description')}")
        print("\nProposed Mitigation:")
        print("-" * 70)
        print(r.get('mitigation'))
        print("-" * 70)
        print()
