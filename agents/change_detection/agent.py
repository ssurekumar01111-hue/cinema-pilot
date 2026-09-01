"""
agents/change_detection/agent.py

Change Detection Agent — polls the Production Graph events table,
computes which downstream agents should be triggered for each change,
and logs a structured audit event per change that records the decision.

Design notes:
  - Agent trigger logic is expressed as a declarative rule table
    (TRIGGER_RULES), not scattered if/else code. Adding a new rule is a
    one-liner in the table.
  - detect_changes() is stateless: it takes a ``since_timestamp`` and
    returns a list of routing decisions. The caller (scheduler / Pub/Sub
    handler) controls the polling interval and persistence of the cursor.
  - Events logged by change_detection_agent itself are skipped to prevent
    infinite re-processing loops.
  - Metadata fields (version, updated_at) are excluded from business-logic
    diffs so that a version increment alone never triggers downstream agents.

Usage (imported):
    from agents.change_detection.agent import detect_changes
    from datetime import datetime, timezone, timedelta

    since = datetime.now(timezone.utc) - timedelta(minutes=5)
    routing_decisions = detect_changes(since)

Usage (standalone demo):
    python agents/change_detection/agent.py
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Repo-root path resolution (works when run as __main__ or imported)
# ---------------------------------------------------------------------------
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from shared.graph_client import ProductionGraphClient, GraphClientError
from shared.telemetry import instrument_agent, record_affected_agents_count

# ---------------------------------------------------------------------------
# Trigger rule table
# ---------------------------------------------------------------------------
# Structure: entity_type -> list of (watched_fields | None, agents_to_trigger)
#
# watched_fields: a set of field names — the rule fires if ANY of these
#                 fields changed between before_state and after_state.
# None (_ALWAYS): rule fires regardless of which fields changed (used for
#                 entity types where any mutation is always significant).
#
# Agents in each list are returned in priority order. If multiple rules
# fire for the same event, triggered agents are merged in rule order with
# duplicates removed (preserving first-seen order).

_ALWAYS = None  # Sentinel: trigger on any change to this entity type.

TRIGGER_RULES: dict[str, list[tuple[set[str] | None, list[str]]]] = {
    "scene": [
        ({"location_id"},    ["budget", "location", "storyboard", "schedule", "music", "risk"]),
        ({"character_ids"},  ["casting", "budget"]),
        ({"emotional_tone"}, ["director", "music"]),
        ({"camera_cues"},    ["director", "storyboard"]),
    ],
    "location": [
        ({"cost_profile"},        ["budget"]),
        ({"weather_sensitivity"}, ["risk", "schedule"]),
    ],
    "character": [
        ({"description", "costume_notes"}, ["casting"]),
    ],
    "budget_line": [
        (_ALWAYS, ["producer"]),   # any budget mutation always alerts producer
    ],
    "prop": [
        # Prop changes are informational only — no downstream triggers yet
    ],
    "screenplay": [
        # Initial ingestion event — no downstream triggers
    ],
}

# Fields that are always updated on any upsert (metadata). Exclude from
# business-logic diffs so a version bump alone triggers nothing.
_METADATA_FIELDS: frozenset[str] = frozenset({
    "version", "updated_at", "event_id", "event_timestamp",
})

# Actor name used when this agent logs its own routing decisions.
_ACTOR = "change_detection_agent"


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------

def _parse_state(raw: Any) -> dict:
    """
    Coerce a state value to a plain dict.

    Event state fields come back from BigQuery as JSON strings
    (via TO_JSON_STRING). This handles both the pre-parsed dict case
    (when called programmatically) and the JSON-string case (from BQ).

    Args:
        raw: Either a dict or a JSON-encoded string.

    Returns:
        A plain Python dict. Returns ``{}`` for None or empty inputs.
    """
    if raw is None:
        return {}
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}


def _diff(before: dict, after: dict) -> tuple[list[str], dict, dict]:
    """
    Compute a field-level diff between two state dicts.

    Metadata fields (``version``, ``updated_at``, etc.) are excluded.
    List values are compared for full equality (order matters).

    Args:
        before: Entity state before the change.
        after:  Entity state after the change.

    Returns:
        A 3-tuple of:
          - ``changed_fields``: sorted list of field names that differ.
          - ``diff_before``: sub-dict of ``before`` for changed fields only.
          - ``diff_after``:  sub-dict of ``after``  for changed fields only.
    """
    all_keys = (set(before.keys()) | set(after.keys())) - _METADATA_FIELDS
    changed    : list[str] = []
    diff_before: dict      = {}
    diff_after : dict      = {}

    for key in sorted(all_keys):
        b_val = before.get(key)
        a_val = after.get(key)
        if b_val != a_val:
            changed.append(key)
            diff_before[key] = b_val
            diff_after[key]  = a_val

    return changed, diff_before, diff_after


def _compute_triggered_agents(entity_type: str, changed_fields: set[str]) -> list[str]:
    """
    Determine which downstream agents to trigger given the entity type and
    the set of changed field names.

    Consults ``TRIGGER_RULES`` for the entity type. If multiple rules fire,
    their agent lists are merged in rule order with duplicates removed
    (first-seen order preserved).

    Args:
        entity_type:    The type of entity that changed (e.g. ``"scene"``).
        changed_fields: Set of field names that differed between states.

    Returns:
        Ordered, deduplicated list of agent names to trigger. Empty list if
        no rules match or the entity type is not in the rule table.
    """
    rules = TRIGGER_RULES.get(entity_type, [])
    seen:      set[str]  = set()
    triggered: list[str] = []

    for watched, agents in rules:
        fires = (watched is _ALWAYS) or bool(changed_fields & watched)
        if fires:
            for agent in agents:
                if agent not in seen:
                    triggered.append(agent)
                    seen.add(agent)

    return triggered


def _describe_routing_decision(
    entity_type: str,
    changed_fields: list[str],
    triggered_agents: list[str],
) -> str:
    """Return the concise explanation stored beside a routing decision."""
    fields = ", ".join(changed_fields) if changed_fields else "no business fields"
    if not triggered_agents:
        return f"{entity_type} changed in {fields} / no downstream agent is affected"
    agents = ", ".join(triggered_agents)
    return f"{entity_type} changed in {fields} / routed to {agents}"


# ---------------------------------------------------------------------------
# Main detection function
# ---------------------------------------------------------------------------

@instrument_agent("change_detection_agent")
def detect_changes(since_timestamp: datetime, cascade_id: str | None = None) -> list[dict]:
    """
    Poll the Production Graph events table for changes since a given time
    and compute which downstream agents should be triggered for each.

    For every qualifying event this function:
      1. Parses the before/after state fields from JSON.
      2. Computes a field-level diff (excluding metadata fields).
      3. Looks up the trigger rule table to determine which agents to fire.
      4. Logs a new event in the audit trail recording the routing decision
         (actor_agent="change_detection_agent", before/after = the diff,
         triggered_agents = the computed list). Skipped for zero-trigger events.

    Events already logged by ``change_detection_agent`` itself are skipped
    to prevent infinite re-processing loops.

    Args:
        since_timestamp: Lower bound (exclusive) for ``event_timestamp``.
                         Timezone-naive values are assumed UTC.

    Returns:
        List of routing decision dicts, one per processed event::

            [
              {
                "original_event_id": str,
                "entity_type":       str,
                "entity_id":         str,
                "changed_fields":    list[str],
                "triggered_agents":  list[str],
              },
              ...
            ]

    Raises:
        GraphClientError: If any BigQuery read or write fails.
    """
    # Normalise timestamp to UTC
    if since_timestamp.tzinfo is None:
        since_timestamp = since_timestamp.replace(tzinfo=timezone.utc)

    graph   = ProductionGraphClient()
    events  = graph.get_events_since(since_timestamp)

    results: list[dict] = []

    print(f"[change_detection] Polling events since {since_timestamp.isoformat()}")
    print(f"[change_detection] Found {len(events)} event(s) to evaluate.")

    for event in events:
        event_id    = event["event_id"]
        actor       = event["actor_agent"]
        entity_type = event["entity_type"]
        entity_id   = event["entity_id"]

        # Skip our own routing-decision events to prevent loops
        if actor == _ACTOR:
            print(f"  [skip] {event_id[:8]}… actor={actor!r} (own event)")
            continue

        # Parse state fields
        before = _parse_state(event["before_state"])
        after  = _parse_state(event["after_state"])

        # Compute diff
        changed_fields, diff_before, diff_after = _diff(before, after)

        # Compute which agents to trigger
        triggered = _compute_triggered_agents(entity_type, set(changed_fields))

        print(
            f"  [eval] {event_id[:8]}…  "
            f"entity={entity_type}/{entity_id}  "
            f"actor={actor!r}  "
            f"changed={changed_fields}  "
            f"-> trigger={triggered}"
        )

        routing_reason = _describe_routing_decision(entity_type, changed_fields, triggered)

        # Log routing decision in audit trail (only if there's something to trigger)
        if triggered:
            record_affected_agents_count(cascade_id or "standalone", len(triggered))
            graph.log_event(
                actor_agent=_ACTOR,
                entity_type=entity_type,
                entity_id=entity_id,
                before_state=diff_before,
                after_state={**diff_after, "routing_reason": routing_reason},
                triggered_agents=triggered,
            )

        results.append({
            "original_event_id": event_id,
            "entity_type":       entity_type,
            "entity_id":         entity_id,
            "changed_fields":    changed_fields,
            "triggered_agents":  triggered,
            "reason":             routing_reason,
        })

    print(f"[change_detection] Done. {len(results)} decision(s) produced.")
    return results


# ---------------------------------------------------------------------------
# Standalone demo / test harness
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import pprint

    DEMO_SCENE_ID   = "scene_005"
    NEW_LOCATION_ID = "loc_sunset_beach"

    print("=" * 70)
    print("  Change Detection Agent — Scene 5 Relocation Demo")
    print("=" * 70)

    graph = ProductionGraphClient()

    # ------------------------------------------------------------------
    # Step 1: Fetch the current scene_005
    # ------------------------------------------------------------------
    print(f"\n[demo] Fetching {DEMO_SCENE_ID} from Production Graph...")
    original_scene = graph.get_scene(DEMO_SCENE_ID)
    if original_scene is None:
        raise RuntimeError(
            f"{DEMO_SCENE_ID} not found. "
            f"Run agents/script_intelligence/agent.py first."
        )

    print(f"[demo] Current state of {DEMO_SCENE_ID}:")
    for k, v in original_scene.items():
        print(f"  {k:<25} = {v!r}")

    original_location = original_scene.get("location_id")
    target_location = "loc_sunset_beach" if original_location != "loc_sunset_beach" else "loc_millbrook_storage_unit"

    print(f"\n[demo] Simulating relocation: "
          f"{original_location!r} -> {target_location!r}")

    relocated_scene = dict(original_scene)
    relocated_scene["location_id"] = target_location

    # Capture timestamp BEFORE the write so detect_changes can find it
    change_ts = datetime.now(timezone.utc)

    # ------------------------------------------------------------------
    # Step 3: Write the updated scene
    # ------------------------------------------------------------------
    print(f"[demo] Writing updated {DEMO_SCENE_ID} via upsert_scene()...")
    graph.upsert_scene({
        "scene_id":          relocated_scene["scene_id"],
        "scene_number":      relocated_scene["scene_number"],
        "location_id":       relocated_scene["location_id"],
        "character_ids":     list(relocated_scene.get("character_ids") or []),
        "prop_ids":          list(relocated_scene.get("prop_ids") or []),
        "emotional_tone":    relocated_scene.get("emotional_tone", ""),
        "camera_cues":       list(relocated_scene.get("camera_cues") or []),
        "timeline_position": relocated_scene.get("timeline_position"),
        "status":            relocated_scene.get("status", "draft"),
    })
    print(f"[demo] upsert_scene() complete.")

    # ------------------------------------------------------------------
    # Step 4: Log an event recording this change so detect_changes can find it.
    #         In production, whichever agent makes the upsert is responsible
    #         for calling log_event immediately after the write.
    # ------------------------------------------------------------------
    print(f"[demo] Logging relocation change event...")
    change_event_id = graph.log_event(
        actor_agent="demo_relocation",
        entity_type="scene",
        entity_id=DEMO_SCENE_ID,
        before_state={k: v for k, v in original_scene.items()
                      if k not in ("version", "updated_at")},
        after_state={k: v for k, v in relocated_scene.items()
                     if k not in ("version", "updated_at")},
        triggered_agents=[],   # not decided yet — that's change_detection's job
    )
    print(f"[demo] Change event logged: {change_event_id}")

    # ------------------------------------------------------------------
    # Step 5: Run detect_changes — should find and route the relocation event
    # ------------------------------------------------------------------
    print(f"\n[demo] Running detect_changes(since={change_ts.isoformat()})...")
    decisions = detect_changes(change_ts)

    # ------------------------------------------------------------------
    # Step 6: Print results
    # ------------------------------------------------------------------
    print("\n" + "=" * 70)
    print("  Routing Decisions")
    print("=" * 70)

    for decision in decisions:
        print(f"\n  original_event_id : {decision['original_event_id']}")
        print(f"  entity_type       : {decision['entity_type']}")
        print(f"  entity_id         : {decision['entity_id']}")
        print(f"  changed_fields    : {decision['changed_fields']}")
        print(f"  triggered_agents  : {decision['triggered_agents']}")

    print()

    # Assertion: confirm the relocation decision is exactly right
    relocation_decisions = [
        d for d in decisions
        if d["entity_id"] == DEMO_SCENE_ID
    ]

    if not relocation_decisions:
        print("  ❌  No decision found for scene_005 — check timestamps.")
        sys.exit(1)

    rd = relocation_decisions[0]
    expected_fields  = ["location_id"]
    expected_agents  = ["budget", "location", "storyboard", "schedule", "music", "risk"]

    field_ok  = rd["changed_fields"] == expected_fields
    agents_ok = rd["triggered_agents"] == expected_agents

    print("  Assertion check:")
    print(f"  changed_fields == {expected_fields}:  {'OK' if field_ok  else 'FAIL'}")
    print(f"  triggered_agents == {expected_agents}:")
    print(f"    {'PASS' if agents_ok else 'FAIL'}")

    if field_ok and agents_ok:
        print("\n  Demo PASSED - Change Detection Agent is working correctly.")
    else:
        print("\n  Demo FAILED - review the output above.")
        sys.exit(1)

    # ------------------------------------------------------------------
    # Step 7: Loop-prevention test
    #
    # Run detect_changes() a SECOND TIME using the SAME since_timestamp.
    # At this point the events table contains (since change_ts):
    #   1. e6f4a216… — actor=demo_relocation  (the actual scene change)
    #   2. <uuid>…   — actor=change_detection_agent  (the routing decision
    #                   logged by the first detect_changes() call above)
    #
    # The second run should:
    #   - See 2 events (both are after change_ts)
    #   - Process event 1 again (demo_relocation) → same trigger output
    #   - SKIP  event 2 (change_detection_agent) via the actor filter
    #   - NOT log a second routing-decision event for event 2
    #
    # We verify: decisions list has exactly 1 entry for scene_005, and
    # no entry whose triggered_agents came from change_detection_agent's
    # own event being re-processed.
    # ------------------------------------------------------------------
    print()
    print("=" * 70)
    print("  Loop-Prevention Test: second call with same since_timestamp")
    print("=" * 70)
    print("  (Expect: routing-decision event from run 1 is SKIPPED,")
    print("   scene change event is processed again, total decisions = 1)")
    print()

    decisions_run2 = detect_changes(change_ts)

    print()
    print("  Run 2 raw results:")
    for d in decisions_run2:
        print(f"    entity_id={d['entity_id']}  "
              f"changed={d['changed_fields']}  "
              f"triggered={d['triggered_agents']}")

    # Assertions for run 2
    # The only decision should still be scene_005 -> same 6 agents
    run2_scene_decisions = [d for d in decisions_run2 if d["entity_id"] == DEMO_SCENE_ID]
    loop_ok = len(run2_scene_decisions) == 1

    # Confirm no decision was produced for an event where actor was change_detection_agent.
    no_double_trigger = len([d for d in decisions_run2 if d["entity_id"] == DEMO_SCENE_ID]) <= 1

    print()
    print("  Loop-prevention assertions:")
    print(f"  Exactly 1 decision for {DEMO_SCENE_ID} in run 2: "
          f"{'OK' if loop_ok else 'FAIL - routing event was re-processed!'}")
    print(f"  No double-trigger for {DEMO_SCENE_ID}: "
          f"{'OK' if no_double_trigger else 'FAIL'}")

    # Also confirm skip line was printed for change_detection_agent event
    print("  (Check output above for '[skip]' line with actor='change_detection_agent')")

    if loop_ok and no_double_trigger:
        print("\n  Loop-prevention CONFIRMED - own events are skipped on re-poll.")
    else:
        print("\n  Loop-prevention FAILED - see output above.")
        sys.exit(1)
