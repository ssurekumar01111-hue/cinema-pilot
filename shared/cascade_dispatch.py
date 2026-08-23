"""Deterministic dispatch for agents selected by Change Detection."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from typing import Any


class UnknownCascadeAgentError(ValueError):
    """Raised when a routing decision names an agent without a handler."""


def dispatch_triggered_agents(
    triggered_agents: Iterable[str],
    handlers: Mapping[str, Callable[[], Any]],
) -> dict[str, Any]:
    """Execute each routed agent once, in the order selected by routing.

    The complete decision is validated before any handler runs. This prevents
    a partially executed cascade if a rule introduces an agent that the caller
    has not wired yet.
    """
    ordered_agents: list[str] = []
    seen: set[str] = set()

    for agent_name in triggered_agents:
        if not isinstance(agent_name, str) or not agent_name:
            raise ValueError("Triggered agent names must be non-empty strings.")
        if agent_name not in seen:
            ordered_agents.append(agent_name)
            seen.add(agent_name)

    unknown_agents = [name for name in ordered_agents if name not in handlers]
    if unknown_agents:
        raise UnknownCascadeAgentError(
            "No cascade handler is configured for: "
            + ", ".join(repr(name) for name in unknown_agents)
        )

    return {agent_name: handlers[agent_name]() for agent_name in ordered_agents}
