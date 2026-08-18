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


from shared.grafana_client import get_grafana_toolset

# ---------------------------------------------------------------------------
# Grafana MCP Observability Helper
# ---------------------------------------------------------------------------

async def check_observability_context(location_name: str) -> dict[str, Any]:
    """
    Query Grafana Cloud MCP for observability data (datasources, metrics, logs)
    relevant to location_name, logging audit events.

    Args:
        location_name: Name of the location (e.g. "Sunset Beach").

    Returns:
        Dict containing observability findings and query status.
    """
    graph = ProductionGraphClient()
    toolset = get_grafana_toolset(
        tool_filter=[
            "list_datasources",
            "list_prometheus_metric_names",
            "query_prometheus",
            "query_loki_logs",
        ]
    )

    tools = await toolset.get_tools()
    tools_by_name = {t.name: t for t in tools}
    list_ds_tool = tools_by_name.get("list_datasources")

    datasources: list[dict[str, Any]] = []
    if list_ds_tool:
        try:
            ds_res = await list_ds_tool._run_async_impl(args={}, tool_context=None, credential=None)
            content_list = ds_res.get("content", [])
            for c in content_list:
                if c.get("type") == "text":
                    try:
                        parsed = json.loads(c.get("text", "{}"))
                        datasources = parsed.get("datasources", parsed if isinstance(parsed, list) else [])
                    except json.JSONDecodeError:
                        pass
        except Exception as exc:
            print(f"[location_agent] Warning: list_datasources check failed ({exc})")

    if not datasources:
        result_dict = {
            "datasources_checked": True,
            "relevant_data_found": False,
            "datasources_count": 0,
            "note": "No datasources configured in this Grafana stack yet",
        }
        graph.log_event(
            actor_agent="location_agent",
            entity_type="observability_check",
            entity_id=location_name,
            before_state={},
            after_state=result_dict,
            triggered_agents=[],
        )
        return result_dict

    # Filter for Prometheus or Loki datasources
    prometheus_ds = [ds for ds in datasources if "prometheus" in str(ds.get("type", "")).lower()]
    loki_ds = [ds for ds in datasources if "loki" in str(ds.get("type", "")).lower()]

    queried_metrics: list[Any] = []
    queried_logs: list[Any] = []

    # Check Prometheus metrics if available
    prom_tool = tools_by_name.get("list_prometheus_metric_names")
    query_prom_tool = tools_by_name.get("query_prometheus")
    if prometheus_ds:
        prom_uid = prometheus_ds[0].get("uid")
        if prom_uid and prom_tool:
            try:
                metrics_res = await prom_tool._run_async_impl(
                    args={"datasourceUid": prom_uid}, tool_context=None, credential=None
                )
                queried_metrics.append(metrics_res)
            except Exception as exc:
                print(f"[location_agent] Warning: list_prometheus_metric_names failed: {exc}")

        if prom_uid and query_prom_tool:
            try:
                prom_query_res = await query_prom_tool._run_async_impl(
                    args={
                        "datasourceUid": prom_uid,
                        "expr": "up",
                        "endTime": "now",
                        "queryType": "instant",
                    },
                    tool_context=None,
                    credential=None,
                )
                queried_metrics.append(prom_query_res)
            except Exception as exc:
                print(f"[location_agent] Warning: query_prometheus failed: {exc}")

    # Check Loki logs if available
    loki_tool = tools_by_name.get("query_loki_logs")
    if loki_ds and loki_tool:
        loki_uid = loki_ds[0].get("uid")
        if loki_uid:
            try:
                logs_res = await loki_tool._run_async_impl(
                    args={"datasourceUid": loki_uid, "logql": f'{{app="cinemapilot"}} |= `{location_name}`'},
                    tool_context=None,
                    credential=None,
                )
                queried_logs.append(logs_res)
            except Exception as exc:
                print(f"[location_agent] Warning: query_loki_logs failed: {exc}")

    # Determine if any query actually succeeded AND returned non-empty, non-error content
    has_valid_metrics = False
    for res in queried_metrics:
        if isinstance(res, dict) and not res.get("isError", False):
            for c in res.get("content", []):
                if c.get("type") == "text" and c.get("text", "[]").strip() not in ("[]", ""):
                    has_valid_metrics = True

    has_valid_logs = False
    for res in queried_logs:
        if isinstance(res, dict) and not res.get("isError", False):
            for c in res.get("content", []):
                if c.get("type") == "text":
                    try:
                        parsed = json.loads(c.get("text", "{}"))
                        if parsed.get("data") and len(parsed.get("data")) > 0:
                            has_valid_logs = True
                    except json.JSONDecodeError:
                        pass

    relevant_data_found = has_valid_metrics or has_valid_logs

    result_dict = {
        "datasources_checked": True,
        "relevant_data_found": relevant_data_found,
        "datasources_count": len(datasources),
        "prometheus_count": len(prometheus_ds),
        "loki_count": len(loki_ds),
        "metrics_response": queried_metrics,
        "logs_response": queried_logs,
    }

    graph.log_event(
        actor_agent="location_agent",
        entity_type="observability_check",
        entity_id=location_name,
        before_state={},
        after_state=result_dict,
        triggered_agents=[],
    )

    return result_dict


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
    location_name = location.get("name", "Unknown Location")

    # 2. Call Gemini for assessment
    client = _build_gemini_client()
    prompt = LOCATION_ASSESSMENT_PROMPT.format(
        scene_id=scene.get("scene_id"),
        scene_number=scene.get("scene_number"),
        location_id=location.get("location_id"),
        location_name=location_name,
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

    # 4. Check Grafana Observability Context via MCP
    import asyncio
    try:
        observability_context = asyncio.run(check_observability_context(location_name))
    except Exception as exc:
        print(f"[location_agent] Warning: check_observability_context failed: {exc}")
        observability_context = {"error": str(exc)}

    # 5. Log audit event
    triggered_agents = ["risk"] if requires_risk_flag else []
    after_state = {
        "logistics_summary": logistics_summary,
        "risk_level": risk_level,
        "risk_reason": risk_reason,
        "requires_risk_flag": requires_risk_flag,
        "risk_flag_id": risk_flag_id,
        "observability_context": observability_context,
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
        "location_name": location_name,
        "logistics_summary": logistics_summary,
        "risk_level": risk_level,
        "risk_reason": risk_reason,
        "requires_risk_flag": requires_risk_flag,
        "risk_flag_id": risk_flag_id,
        "observability_context": observability_context,
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
    print(f"Location ID           : {result.get('location_id')}")
    print(f"Location Name         : {result.get('location_name')}")
    print(f"Risk Level            : {result.get('risk_level').upper()}")
    print(f"Requires Risk Flag    : {result.get('requires_risk_flag')}")
    print(f"Risk Flag ID          : {result.get('risk_flag_id')}")
    print(f"Triggered Agents      : {result.get('triggered_agents')}")
    print("\nLogistics Summary:")
    print("-" * 70)
    print(result.get('logistics_summary'))
    print("-" * 70)
    print("\nRisk Reason:")
    print("-" * 70)
    print(result.get('risk_reason'))
    print("-" * 70)
    print("\nObservability Context (Grafana Cloud MCP):")
    print("-" * 70)
    print(json.dumps(result.get('observability_context'), indent=2))
    print("-" * 70)

