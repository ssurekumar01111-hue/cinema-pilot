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

        # Automatically escalate high-severity risks to Grafana Cloud Incident as part of the cascade
        if severity == "high":
            try:
                print(f"  + [risk_agent] Automatically escalating high-severity risk '{flag_id}' to Grafana Cloud...")
                try:
                    loop = asyncio.get_event_loop()
                except RuntimeError:
                    loop = None

                if loop and loop.is_running():
                    import nest_asyncio
                    nest_asyncio.apply()
                    loop.run_until_complete(escalate_risk_to_grafana(flag_id))
                else:
                    asyncio.run(escalate_risk_to_grafana(flag_id))
            except Exception as exc:
                print(f"  + [risk_agent] Warning: Automatic Grafana escalation failed for '{flag_id}': {exc}")

    return updated_records


from shared.grafana_client import get_grafana_toolset as _shared_get_grafana_toolset

def get_grafana_toolset() -> McpToolset:
    return _shared_get_grafana_toolset(
        tool_filter=["list_incidents", "create_incident", "add_activity_to_incident", "get_incident"]
    )


async def escalate_risk_to_grafana(risk_flag_id: str) -> dict:
    """
    Escalate a high-severity, mitigated risk flag to Grafana Cloud Incidents via MCP.

    Args:
        risk_flag_id: Unique ID of the risk flag record.

    Returns:
        Dict containing Grafana incident details or status.
    """
    graph = ProductionGraphClient()

    # a. Fetch risk flag record
    risk_flag = graph.get_risk_flag(risk_flag_id)
    if not risk_flag:
        raise ValueError(f"Risk flag '{risk_flag_id}' not found in Production Graph.")

    severity = str(risk_flag.get("severity", "")).lower()
    mitigation = str(risk_flag.get("mitigation", "")).strip()

    # b. Only proceed if severity == "high" AND mitigation is not empty
    if severity != "high" or not mitigation:
        print(f"[risk_agent] Skipping escalation for '{risk_flag_id}': severity='{severity}', has_mitigation={bool(mitigation)}")
        return {"status": "skipped", "reason": "Not high severity or unmitigated"}

    # Initialize Grafana MCP toolset
    toolset = get_grafana_toolset()
    tools = await toolset.get_tools()
    tools_by_name = {t.name: t for t in tools}

    list_incidents_tool = tools_by_name.get("list_incidents")
    create_incident_tool = tools_by_name.get("create_incident")
    add_activity_tool = tools_by_name.get("add_activity_to_incident")
    get_incident_tool = tools_by_name.get("get_incident")

    # c. Check if incident already exists referencing risk_flag_id
    if list_incidents_tool:
        try:
            incidents_response = await list_incidents_tool._run_async_impl(
                args={"limit": 20},
                tool_context=None,
                credential=None,
            )
            # Parse text response content from MCP tool
            content_list = incidents_response.get("content", [])
            for c in content_list:
                if c.get("type") == "text":
                    try:
                        inc_data = json.loads(c.get("text", "{}"))
                        incidents = inc_data.get("incidents", [])
                        for inc in incidents:
                            inc_title = str(inc.get("title", ""))
                            inc_desc = str(inc.get("description", ""))
                            if risk_flag_id in inc_title or risk_flag_id in inc_desc:
                                inc_url = inc.get("url") or inc.get("html_url") or json.dumps(inc)
                                print(f"[risk_agent] Existing Grafana incident found for {risk_flag_id}: {inc_url}")
                                updated_record = dict(risk_flag)
                                updated_record["grafana_incident_url"] = inc_url
                                graph.upsert_risk_flag(updated_record)
                                return {"status": "already_exists", "grafana_incident_url": inc_url, "incident": inc}
                    except json.JSONDecodeError:
                        pass
        except Exception as exc:
            print(f"[risk_agent] Warning: list_incidents check failed ({exc}). Proceeding to create check.")

    # Check if we already stored a URL locally
    existing_url = risk_flag.get("grafana_incident_url")
    if existing_url:
        print(f"[risk_agent] Existing incident URL recorded in graph: {existing_url}")
        return {"status": "already_exists", "grafana_incident_url": existing_url}

    # d. Create new Grafana incident with full context
    linked_entity_id = risk_flag.get("linked_entity_id", "unknown")
    description_text = risk_flag.get("description", "")
    
    title = f"CinemaPilot Risk: {linked_entity_id} — {severity.upper()}"
    full_description = (
        f"Risk Flag ID: {risk_flag_id}\n"
        f"Linked Entity: {linked_entity_id}\n"
        f"Severity: {severity.upper()}\n\n"
        f"Risk Description:\n{description_text}\n\n"
        f"Mitigation Strategy:\n{mitigation}"
    )

    if not create_incident_tool:
        raise RuntimeError("Grafana MCP tool 'create_incident' is not available.")

    incident_response = await create_incident_tool._run_async_impl(
        args={
            "title": title,
            "severity": "Critical",
            "roomPrefix": "cinemapilot",
            "isDrill": False,
        },
        tool_context=None,
        credential=None,
    )

    incident_url = ""
    incident_id = None
    content_list = incident_response.get("content", [])
    for c in content_list:
        if c.get("type") == "text":
            text = c.get("text", "")
            if "url" in text or "http" in text or "incidentID" in text:
                incident_url = text
            try:
                parsed_inc = json.loads(text)
                incident_id = parsed_inc.get("incidentID") or parsed_inc.get("id")
            except Exception:
                pass

    # Post the full contextual description note directly onto the incident timeline
    if incident_id and add_activity_tool:
        try:
            print(f"[risk_agent] Adding full context note to Grafana incident {incident_id} timeline...")
            await add_activity_tool._run_async_impl(
                args={
                    "incidentId": str(incident_id),
                    "body": full_description,
                },
                tool_context=None,
                credential=None,
            )
        except Exception as exc:
            print(f"[risk_agent] Warning: add_activity_to_incident failed: {exc}")

    # e. Store incident URL/JSON back onto risk_flags record
    updated_record = dict(risk_flag)
    updated_record["grafana_incident_url"] = incident_url
    graph.upsert_risk_flag(updated_record)

    # f. Log audit event
    graph.log_event(
        actor_agent="risk_agent",
        entity_type="risk_flag",
        entity_id=risk_flag_id,
        before_state={"grafana_incident_url": None},
        after_state={"grafana_incident_url": incident_url, "full_description": full_description},
        triggered_agents=[],
    )

    print(f"[risk_agent] Escalation result for {risk_flag_id}: {incident_response}")
    return incident_response


# ---------------------------------------------------------------------------
# Standalone Test Harness
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import asyncio

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

    print("=" * 70)
    print("  Testing Grafana Risk Escalation")
    print("=" * 70)
    test_flag_id = "rf_loc_loc_sunset_beach"
    try:
        esc_res = asyncio.run(escalate_risk_to_grafana(test_flag_id))
        print(f"\nEscalation Result for {test_flag_id}:")
        print(json.dumps(esc_res, indent=2, default=str))
    except Exception as e:
        print(f"\nEscalation Execution Error: {e}")


