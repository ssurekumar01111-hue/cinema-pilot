"""Offline contract tests for routing-driven cascade dispatch."""

from __future__ import annotations

import unittest

from shared.cascade_dispatch import (
    UnknownCascadeAgentError,
    dispatch_triggered_agents,
)


class DispatchTriggeredAgentsTests(unittest.TestCase):
    def test_runs_selected_agents_once_in_routing_order(self) -> None:
        calls: list[str] = []

        def handler(name: str) -> str:
            calls.append(name)
            return f"{name}-result"

        results = dispatch_triggered_agents(
            ["schedule", "budget", "schedule"],
            {
                "budget": lambda: handler("budget"),
                "schedule": lambda: handler("schedule"),
            },
        )

        self.assertEqual(calls, ["schedule", "budget"])
        self.assertEqual(
            results,
            {"schedule": "schedule-result", "budget": "budget-result"},
        )

    def test_rejects_unknown_agents_before_running_any_handler(self) -> None:
        calls: list[str] = []

        with self.assertRaisesRegex(UnknownCascadeAgentError, "risk"):
            dispatch_triggered_agents(
                ["budget", "risk"],
                {"budget": lambda: calls.append("budget")},
            )

        self.assertEqual(calls, [])

    def test_rejects_invalid_agent_names(self) -> None:
        with self.assertRaisesRegex(ValueError, "non-empty strings"):
            dispatch_triggered_agents(["budget", ""], {"budget": lambda: None})


if __name__ == "__main__":
    unittest.main()
