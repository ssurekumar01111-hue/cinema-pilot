"""
scripts/test_full_cascade.py

Full end-to-end integration test runner for the CinemaPilot multi-agent cascade.
Executes all 7 steps continuously in a single session:
  Step 1: Baseline reset (scene_005 -> loc_millbrook_storage_unit)
  Step 2: Trigger change (scene_005 -> loc_sunset_beach)
  Step 3: Change Detection Agent invocation
  Step 4: Cascade execution from the Change Detection routing decision
  Step 5: Producer Agent overview synthesis
  Step 6: Explanation Agent narrative synthesis
  Step 7: Full Production Graph audit trail extraction
"""

from __future__ import annotations

import datetime
import json
import os
import sys
import time
import uuid

# Ensure cinemapilot directory is in path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../")))

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.abspath(os.path.join(os.path.dirname(__file__), "../.env")))
except ImportError:
    pass

from shared.graph_client import ProductionGraphClient
from shared.cascade_dispatch import dispatch_routing_decision
from shared.telemetry import (
    record_cascade_status,
    flush_telemetry,
    has_cascade_failures,
    get_cascade_failure_count,
)
from agents.change_detection.agent import detect_changes
from agents.budget.agent import recalculate_budget
from agents.location.agent import assess_location
from agents.risk.agent import mitigate_risks
from agents.schedule.agent import reschedule_shoot
from agents.storyboard.agent import generate_storyboard
from agents.music.agent import generate_music_cue
from agents.producer.agent import producer_overview
from agents.explanation.agent import explain_change


def main():
    graph_client = ProductionGraphClient()
    cascade_start_real = time.time()
    cascade_id = f"cascade-{uuid.uuid4().hex[:8]}"

    print("=" * 80)
    print("STARTING CINEMAPILOT FULL END-TO-END INTEGRATION TEST")
    print(f"Active Cascade Correlation ID: {cascade_id}")
    print("=" * 80)

    # -------------------------------------------------------------------------
    # STEP 1 — Baseline reset
    # -------------------------------------------------------------------------
    print("\n" + "=" * 80)
    print("STEP 1: BASELINE RESET")
    print("=" * 80)

    cascade_start_time = datetime.datetime.now(datetime.timezone.utc)
    print(f"Recorded cascade_start_time: {cascade_start_time.isoformat()}")

    scene = graph_client.get_scene("scene_005")
    if not scene:
        raise ValueError("scene_005 not found in graph!")

    before_reset = dict(scene)
    scene["location_id"] = "loc_millbrook_storage_unit"
    graph_client.upsert_scene(scene)

    reset_event_id = graph_client.log_event(
        actor_agent="test_harness",
        entity_type="scene",
        entity_id="scene_005",
        before_state={"location_id": before_reset.get("location_id")},
        after_state={"location_id": "loc_millbrook_storage_unit"},
        triggered_agents=[],
    )
    print(f"Reset scene_005 location_id -> 'loc_millbrook_storage_unit' (Event ID: {reset_event_id})")

    # Reset risk flag for Sunset Beach so Risk Agent tests mitigation & escalation from scratch
    rf_beach = graph_client.get_risk_flag("rf_loc_loc_sunset_beach")
    if rf_beach:
        rf_beach["mitigation"] = ""
        rf_beach["grafana_incident_url"] = None
        graph_client.upsert_risk_flag(rf_beach)
        print("Reset rf_loc_loc_sunset_beach mitigation -> '' and grafana_incident_url -> None")

    # Reset baseline budget line for scene_005 to Millbrook storage unit baseline ($850.00)
    graph_client.upsert_budget_line({
        "budget_line_id": "bl_location_scene_005",
        "category": "location",
        "amount": 850.0,
        "linked_entity_id": "scene_005",
        "last_changed_by_agent": "baseline_reset",
        "reason": "Baseline location fee for Millbrook Storage Unit.",
    })
    print("Reset bl_location_scene_005 amount -> $850.00 (Millbrook baseline)")

    # Reset baseline schedule block for scene_005 to baseline Day 3
    graph_client.upsert_schedule_block({
        "schedule_block_id": "sb_scene_005",
        "scene_id": "scene_005",
        "day_index": 3,
        "duration_minutes": 180,
        "constraints": ["Standard interior lighting", "No weather constraints"],
    })
    print("Reset sb_scene_005 day_index -> Day 3 (Millbrook baseline)")

    # Brief pause to ensure timestamp spacing in BigQuery
    time.sleep(2)

    # -------------------------------------------------------------------------
    # STEP 2 — Fire the trigger
    # -------------------------------------------------------------------------
    print("\n" + "=" * 80)
    print("STEP 2: FIRE THE TRIGGER")
    print("=" * 80)

    trigger_timestamp = datetime.datetime.now(datetime.timezone.utc)
    scene["location_id"] = "loc_sunset_beach"
    graph_client.upsert_scene(scene)

    trigger_event_id = graph_client.log_event(
        actor_agent="user_producer",
        entity_type="scene",
        entity_id="scene_005",
        before_state={"location_id": "loc_millbrook_storage_unit"},
        after_state={"location_id": "loc_sunset_beach"},
        triggered_agents=["change_detection_agent"],
    )
    print(f"TRIGGER FIRED: scene_005 location_id -> 'loc_sunset_beach'")
    print(f"Trigger Event ID: {trigger_event_id}")

    # Brief pause before detection
    time.sleep(2)

    # -------------------------------------------------------------------------
    # STEP 3 — Change Detection
    # -------------------------------------------------------------------------
    print("\n" + "=" * 80)
    print("STEP 3: CHANGE DETECTION")
    print("=" * 80)

    cd_results = detect_changes(since_timestamp=trigger_timestamp, cascade_id=cascade_id)
    print(f"Change Detection Results ({len(cd_results)} change(s) detected):")
    for res in cd_results:
        print(f"  Entity Type:      {res.get('entity_type')}")
        print(f"  Entity ID:        {res.get('entity_id')}")
        print(f"  Changed Fields:   {res.get('changed_fields')}")
        print(f"  Triggered Agents: {res.get('triggered_agents')}")
        print(f"  Reason:           {res.get('reason')}")

    # -------------------------------------------------------------------------
    # STEP 4 — Dispatch exactly the routing decision returned by Change Detection
    # -------------------------------------------------------------------------
    print("\n" + "=" * 80)
    print("STEP 4: RUN ROUTED AGENTS")
    print("=" * 80)

    decision = next(
        (item for item in cd_results if item["entity_type"] == "scene" and item["entity_id"] == "scene_005"),
        None,
    )
    if not decision:
        raise RuntimeError("No routing decision was produced for scene_005")

    handlers = {
        "budget": lambda: recalculate_budget("scene_005", cascade_id=cascade_id),
        "location": lambda: assess_location("scene_005", cascade_id=cascade_id),
        "risk": lambda: mitigate_risks("loc_sunset_beach", cascade_id=cascade_id),
        "schedule": lambda: reschedule_shoot("scene_005", cascade_id=cascade_id),
        "storyboard": lambda: generate_storyboard("scene_005", cascade_id=cascade_id),
        "music": lambda: generate_music_cue("scene_005", cascade_id=cascade_id),
        "director": lambda: print("Director output is already represented by the latest director note"),
        "casting": lambda: print("Casting output is already represented by the latest character sheets"),
        "producer": lambda: producer_overview("scene_005", cascade_id=cascade_id),
    }
    t0 = time.time()
    dispatched = dispatch_routing_decision(decision, handlers)
    for agent_name, result in dispatched:
        print(f"  {agent_name}: completed")
        if isinstance(result, dict):
            print(f"    {json.dumps(result, default=str)[:320]}")
    print(f"Routed cascade completed in {time.time() - t0:.2f}s")

    # -------------------------------------------------------------------------
    # STEP 5 — Producer Synthesis
    # -------------------------------------------------------------------------
    print("\n" + "=" * 80)
    print("STEP 5: PRODUCER SYNTHESIS")
    print("=" * 80)

    t0 = time.time()
    po_res = producer_overview("scene_005", cascade_id=cascade_id)
    print(f"Completed in {time.time() - t0:.2f}s:")
    print(f"  Producer Overview ID: {po_res.get('producer_overview_id')}")
    print(f"  Total Budget Impact:  ${po_res.get('total_budget_impact', 0.0):,.2f}")
    print(f"  Schedule Status:      {po_res.get('schedule_status')}")
    print(f"  Outstanding Risks:    {po_res.get('outstanding_risks')}")
    print(f"  Overview Summary:     {po_res.get('overview_summary')}")
    print(f"  Recommendation:       {po_res.get('recommendation')}")

    # -------------------------------------------------------------------------
    # STEP 6 — Explanation Synthesis
    # -------------------------------------------------------------------------
    print("\n" + "=" * 80)
    print("STEP 6: EXPLANATION SYNTHESIS")
    print("=" * 80)

    t0 = time.time()
    exp_res = explain_change("scene_005", cascade_id=cascade_id)
    print(f"Completed in {time.time() - t0:.2f}s:")
    print(f"  Explanation ID: {exp_res.get('explanation_id')}")
    print(f"  Sources Used:   {exp_res.get('sources_used')}")
    print(f"\nNarrative:\n{exp_res.get('narrative')}")

    # -------------------------------------------------------------------------
    # STEP 7 — Full Audit Trail & Telemetry Flush
    # -------------------------------------------------------------------------
    print("\n" + "=" * 80)
    print("STEP 7: FULL PRODUCTION GRAPH AUDIT TRAIL")
    print("=" * 80)

    events = graph_client.get_events_since(cascade_start_time)
    print(f"Total Audit Events Logged during Cascade: {len(events)}\n")
    for idx, ev in enumerate(events, 1):
        print(f"[{idx}] Timestamp: {ev.get('event_timestamp')}")
        print(f"    Actor Agent:      {ev.get('actor_agent')}")
        print(f"    Entity Type:      {ev.get('entity_type')}")
        print(f"    Entity ID:        {ev.get('entity_id')}")
        print(f"    Triggered Agents: {ev.get('triggered_agents')}")
        print(f"    Before State:     {ev.get('before_state')[:120]}..." if len(str(ev.get('before_state'))) > 120 else f"    Before State:     {ev.get('before_state')}")
        print(f"    After State:      {ev.get('after_state')[:120]}..." if len(str(ev.get('after_state'))) > 120 else f"    After State:      {ev.get('after_state')}")
        print("-" * 60)

    # Record cascade status based on real outcome:
    # If any agent in the cascade raised an exception, is_healthy=False (0=degraded), else True (1=healthy)
    cascade_healthy = not has_cascade_failures(cascade_id)
    failure_count = get_cascade_failure_count(cascade_id)
    record_cascade_status(cascade_id, is_healthy=cascade_healthy)
    flush_telemetry(timeout_millis=10000)
    status_label = "HEALTHY (1)" if cascade_healthy else f"DEGRADED (0) — {failure_count} agent failure(s) recorded"
    print(f"\n[telemetry] Cascade Health Status: {status_label}")
    print(f"[telemetry] Successfully flushed OpenTelemetry cascade metrics for '{cascade_id}' to Grafana Cloud.")

    total_elapsed = time.time() - cascade_start_real
    print("=" * 80)
    print(f"CASCADE INTEGRATION TEST COMPLETE — TOTAL ELAPSED TIME: {total_elapsed:.2f} seconds")
    print("=" * 80)


if __name__ == "__main__":
    main()
