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

from shared.graph_client import ProductionGraphClient
from shared.grafana_client import get_grafana_toolset, get_grafana_stack_url
from shared.telemetry import instrument_agent


def check_production_readiness(scene_id: str, location_id: str, cascade_id: str | None = None) -> dict[str, Any]:
    """
    Evaluate production readiness against real Grafana signals:
    1. Queries Grafana Cloud Incidents via MCP to check for active unresolved incidents on this location.
    2. Queries Prometheus telemetry for agent failures in this cascade run (discovering datasource dynamically).

    Args:
        scene_id: Scene ID being evaluated.
        location_id: Location ID being checked.
        cascade_id: Optional correlation ID for this cascade.

    Returns:
        Dict with keys: ready (bool), readiness_status ("ready"|"blocked"|"unknown"),
        blocking_reasons (list[str]), active_incident_url (str|None),
        active_incident_id (str|None), cascade_had_failures (bool).
    """
    import asyncio

    async def _check_async() -> dict[str, Any]:
        blocking_reasons: list[str] = []
        active_incident_url: str | None = None
        active_incident_id: str | None = None
        cascade_had_failures: bool = False
        query_error: bool = False

        try:
            toolset = get_grafana_toolset(tool_filter=["list_incidents", "query_prometheus", "list_datasources"])
            tools = {t.name: t for t in await toolset.get_tools()}
        except Exception as exc:
            print(f"[producer_agent] Warning: Failed to initialize Grafana toolset: {exc}")
            query_error = True
            blocking_reasons.append(
                f"Could not verify Grafana incident/telemetry status — readiness cannot be confirmed ({exc})."
            )
            tools = {}

        # 1. Check for active unresolved Grafana incidents for this location
        list_incidents_tool = tools.get("list_incidents")
        if list_incidents_tool and location_id:
            try:
                resp = await list_incidents_tool._run_async_impl(
                    args={"limit": 50, "status": "active"},
                    tool_context=None,
                    credential=None,
                )
                if isinstance(resp, dict) and resp.get("isError"):
                    query_error = True
                    err_msg = ""
                    for c in resp.get("content", []):
                        if c.get("type") == "text":
                            err_msg += c.get("text", "")
                    blocking_reasons.append(
                        f"Could not verify Grafana incident status — MCP tool returned error: {err_msg or 'isError=True'}."
                    )
                else:
                    for c in resp.get("content", []):
                        if c.get("type") == "text":
                            try:
                                inc_data = json.loads(c.get("text", "{}"))
                                incidents = inc_data.get("incidents", [])
                                for inc in incidents:
                                    inc_status = str(inc.get("status", "")).lower()
                                    if inc_status != "active":
                                        continue
                                    inc_title = str(inc.get("title", ""))
                                    inc_desc = str(inc.get("description", ""))
                                    if location_id in inc_title or location_id in inc_desc:
                                        inc_id = str(inc.get("incidentId") or inc.get("incidentID") or inc.get("id") or "")
                                        active_incident_id = inc_id
                                        active_incident_url = inc.get("url") or inc.get("html_url") or f"{get_grafana_stack_url()}/a/grafana-irm-app/incidents/{inc_id}"
                                        blocking_reasons.append(
                                            f"Active Grafana incident #{inc_id} for {location_id} has not been resolved: '{inc_title}'"
                                        )
                                        break
                            except json.JSONDecodeError as jde:
                                query_error = True
                                blocking_reasons.append(
                                    f"Could not verify Grafana incident status — invalid JSON response ({jde})."
                                )
            except Exception as exc:
                print(f"[producer_agent] Warning: list_incidents check failed: {exc}")
                query_error = True
                blocking_reasons.append(
                    f"Could not verify Grafana incident status — readiness cannot be confirmed ({exc})."
                )
        elif not list_incidents_tool and location_id:
            query_error = True
            blocking_reasons.append(
                "Could not verify Grafana incident status — list_incidents tool unavailable."
            )

        # 2. Check cascade telemetry failures in Prometheus (dynamic datasource discovery)
        if cascade_id:
            prom_tool = tools.get("query_prometheus")
            list_ds_tool = tools.get("list_datasources")
            if prom_tool:
                # Discover Prometheus datasource dynamically or fallback to env var
                prom_uid = os.environ.get("GRAFANA_PROMETHEUS_UID")
                if not prom_uid and list_ds_tool:
                    try:
                        ds_res = await list_ds_tool._run_async_impl(args={}, tool_context=None, credential=None)
                        if isinstance(ds_res, dict) and not ds_res.get("isError"):
                            for c in ds_res.get("content", []):
                                if c.get("type") == "text":
                                    try:
                                        parsed = json.loads(c.get("text", "{}"))
                                        datasources = parsed.get("datasources", parsed if isinstance(parsed, list) else [])
                                        for ds in datasources:
                                            if "prometheus" in str(ds.get("type", "")).lower() and ds.get("uid"):
                                                prom_uid = ds.get("uid")
                                                break
                                    except json.JSONDecodeError:
                                        pass
                    except Exception as exc:
                        print(f"[producer_agent] Warning: list_datasources discovery failed: {exc}")

                if not prom_uid:
                    prom_uid = os.environ.get("GRAFANA_PROMETHEUS_UID", "grafanacloud-prom")

                try:
                    q_expr = f'cinemapilot_agent_failures_total{{cascade_id="{cascade_id}"}}'
                    p_resp = await prom_tool._run_async_impl(
                        args={"datasourceUid": prom_uid, "expr": q_expr, "endTime": "now", "queryType": "instant"},
                        tool_context=None,
                        credential=None,
                    )
                    if isinstance(p_resp, dict) and p_resp.get("isError"):
                        query_error = True
                        p_err = ""
                        for c in p_resp.get("content", []):
                            if c.get("type") == "text":
                                p_err += c.get("text", "")
                        blocking_reasons.append(
                            f"Could not verify Grafana telemetry status — MCP tool returned error: {p_err or 'isError=True'}."
                        )
                    else:
                        for c in p_resp.get("content", []):
                            if c.get("type") == "text":
                                try:
                                    p_data = json.loads(c.get("text", "{}"))
                                    raw_data = p_data.get("data")
                                    res_list = raw_data if isinstance(raw_data, list) else p_data.get("data", {}).get("result", [])
                                    failing_agents = []
                                    for item in res_list:
                                        val = float(item.get("value", [0, 0])[1])
                                        if val > 0:
                                            failing_agents.append(item.get("metric", {}).get("agent", "unknown"))
                                    if failing_agents:
                                        cascade_had_failures = True
                                        blocking_reasons.append(
                                            f"Agent failure(s) detected during cascade '{cascade_id}': {', '.join(failing_agents)}"
                                        )
                                except Exception as p_parse_exc:
                                    query_error = True
                                    blocking_reasons.append(
                                        f"Could not verify Grafana telemetry status — invalid JSON response ({p_parse_exc})."
                                    )
                except Exception as exc:
                    print(f"[producer_agent] Warning: telemetry query failed: {exc}")
                    query_error = True
                    blocking_reasons.append(
                        f"Could not verify Grafana telemetry status — readiness cannot be confirmed ({exc})."
                    )

        if query_error:
            readiness_status = "unknown"
            ready = False
        elif len(blocking_reasons) > 0:
            readiness_status = "blocked"
            ready = False
        else:
            readiness_status = "ready"
            ready = True

        return {
            "ready": ready,
            "readiness_status": readiness_status,
            "blocking_reasons": blocking_reasons,
            "active_incident_url": active_incident_url,
            "active_incident_id": active_incident_id,
            "cascade_had_failures": cascade_had_failures,
        }

    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = None

    if loop and loop.is_running():
        import nest_asyncio
        nest_asyncio.apply()
        return loop.run_until_complete(_check_async())
    else:
        return asyncio.run(_check_async())


@instrument_agent("producer_agent")
def producer_overview(scene_id: str, cascade_id: str | None = None) -> dict[str, Any]:
    """
    Generate an executive producer overview for a scene ID using Gemini with Grafana readiness gating.

    Args:
      scene_id: Unique scene identifier (e.g. "scene_005").
      cascade_id: Optional correlation ID for the multi-agent cascade run.

    Steps:
      a. Fetches scene, location, budget_lines, schedule_blocks, and risk_flags.
      b. Evaluates live production readiness gate via check_production_readiness().
      c. Prompts Gemini (gemini-2.5-flash) for JSON synthesis with hard readiness constraints.
      d. Stores result in BigQuery producer_overviews table.
      e. Logs audit event to Production Graph events table.

    Returns:
      Dict with producer_overview_id, scene_id, overview_summary, total_budget_impact,
      schedule_status, outstanding_risks, recommendation, readiness_status, and blocking_reasons.
    """
    graph_client = ProductionGraphClient()

    # a. Fetch scene and location
    scene = graph_client.get_scene(scene_id)
    if not scene:
        raise ValueError(f"Scene with ID '{scene_id}' not found in Production Graph.")

    location_id = scene.get("location_id")
    location = graph_client.get_location(location_id) if location_id else None

    # b. Evaluate live Grafana readiness gate
    readiness = check_production_readiness(scene_id, location_id or "", cascade_id)
    print(f"[producer_agent] Readiness gate: status='{readiness['readiness_status']}', ready={readiness['ready']}, blockers={readiness['blocking_reasons']}")

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

    # b. Construct prompt for Gemini with live Grafana Readiness Gate
    readiness_str = f"""- Status: {readiness['readiness_status'].upper()} (Ready: {readiness['ready']})
- Active Incident: {readiness.get('active_incident_url') or 'None'}
- Blocking Reasons: {readiness['blocking_reasons']}"""

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

- Live Production Readiness Gate (Grafana Incidents & Telemetry Observability):
{readiness_str}

INSTRUCTIONS & CONSTRAINTS:
1. Base your summary, total_budget_impact, schedule_status, outstanding_risks, and recommendation STRICTLY on the actual fetched data above.
2. DO NOT invent dollar figures not present in the fetched budget lines.
3. DO NOT invent risks beyond what is recorded in the risk_flags list.
4. Note that if a risk flag has a recorded mitigation (status [MITIGATED]), it is considered MITIGATED and should NOT be listed as an outstanding unmitigated risk in "outstanding_risks". Only unmitigated risks belong in "outstanding_risks".
5. Ensure total_budget_impact in the output JSON matches the exact total budget calculated from the budget lines ({total_budget_impact}).
6. READINESS GATE REQUIREMENTS:
   - If the Readiness Status is UNKNOWN (ready=False):
     * "readiness_status" in your output JSON MUST be "unknown".
     * "blocking_reasons" MUST contain: {json.dumps(readiness['blocking_reasons'])}.
     * "recommendation" MUST EXPLICITLY STATE that production readiness CANNOT BE CONFIRMED due to unverified observability signals or Grafana connectivity errors, advising caution and requiring manual verification before proceeding. NEVER phrase an "unknown" state as an all-clear.
   - If the Readiness Status is BLOCKED (ready=False):
     * "readiness_status" in your output JSON MUST be "blocked".
     * "blocking_reasons" MUST contain: {json.dumps(readiness['blocking_reasons'])}.
     * "recommendation" MUST EXPLICITLY STATE that production is NOT READY to proceed and MUST cite the specific blocker reason(s) (e.g. naming the active Grafana incident or pipeline failures). DO NOT soften this into an "all clear" recommendation despite the blocker.
   - If the Readiness Status is READY (ready=True):
     * "readiness_status" in your output JSON MUST be "ready".
     * "blocking_reasons" MUST be [].
     * "recommendation" should give clear production go-ahead guidance based on the data.
7. Return ONLY a valid JSON object matching this schema:
{{
  "overview_summary": "string",
  "total_budget_impact": {total_budget_impact},
  "schedule_status": "string",
  "outstanding_risks": ["string"],
  "recommendation": "string",
  "readiness_status": "{readiness['readiness_status']}",
  "blocking_reasons": {json.dumps(readiness['blocking_reasons'])}
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
    readiness_status = data.get("readiness_status", readiness["readiness_status"])
    blocking_reasons = data.get("blocking_reasons", readiness["blocking_reasons"])

    # Hard enforcement: ensure returned readiness_status and blocking_reasons match the evaluated gate
    if readiness["readiness_status"] == "unknown":
        readiness_status = "unknown"
        if not blocking_reasons:
            blocking_reasons = readiness["blocking_reasons"]
    elif not readiness["ready"]:
        readiness_status = "blocked"
        if not blocking_reasons:
            blocking_reasons = readiness["blocking_reasons"]

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
        "readiness_status": readiness_status,
        "blocking_reasons": blocking_reasons,
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
        "readiness_status": readiness_status,
        "blocking_reasons": blocking_reasons,
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
