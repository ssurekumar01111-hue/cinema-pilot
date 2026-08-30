"""Small, explicit dispatcher for a routing decision.

The Change Detection Agent owns the order in ``triggered_agents``.  This
module only validates that decision and executes matching handlers in that
same order, so a cascade can be inspected or tested without duplicating it.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any


class CascadeDispatchError(ValueError):
    """Raised when a routing decision cannot be executed safely."""


def dispatch_routing_decision(
    routing_decision: dict[str, Any],
    handlers: dict[str, Callable[[], Any]],
) -> list[tuple[str, Any]]:
    """Run the decision's known handlers exactly once and in routing order."""
    triggered = routing_decision.get("triggered_agents") or []
    if not isinstance(triggered, list) or not all(isinstance(name, str) for name in triggered):
        raise CascadeDispatchError("routing decision must contain a list of agent names")

    ordered = list(dict.fromkeys(triggered))
    unknown = [name for name in ordered if name not in handlers]
    if unknown:
        raise CascadeDispatchError(f"no handler registered for: {', '.join(unknown)}")

    return [(name, handlers[name]()) for name in ordered]
