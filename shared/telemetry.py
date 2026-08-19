"""
shared/telemetry.py

OpenTelemetry instrumentation module for CinemaPilot.
Exports custom metrics (agent duration, failures, cascade status, asset generation)
to Grafana Cloud's OTLP endpoint with fallback to local logging when unconfigured.
"""

from __future__ import annotations

import base64
import functools
import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Callable

from opentelemetry import metrics
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import (
    ConsoleMetricExporter,
    PeriodicExportingMetricReader,
)
from opentelemetry.sdk.resources import Resource

logger = logging.getLogger("cinemapilot.telemetry")

_METER_PROVIDER: MeterProvider | None = None
_METER: metrics.Meter | None = None

# Metric instruments
_AGENT_DURATION = None
_AGENT_FAILURES = None
_CASCADE_STATUS = None
_ASSET_DURATION = None

# In-process cascade failure tracking
_CASCADE_FAILURES: dict[str, int] = {}


def _load_credentials() -> tuple[str | None, str, str]:
    """
    Load Grafana Cloud OTLP credentials from environment variables or secure local config.
    Never exposes or logs token values.
    """
    token = os.environ.get("GRAFANA_CLOUD_OTLP_TOKEN")
    instance_id = os.environ.get("GRAFANA_CLOUD_INSTANCE_ID")
    endpoint = os.environ.get("GRAFANA_CLOUD_OTLP_ENDPOINT")

    # Fallback to local config file if not set in environment
    if not token or not instance_id:
        cfg_path = Path.home() / ".cinemapilot" / "telemetry_config.json"
        if cfg_path.exists():
            try:
                with open(cfg_path, "r", encoding="utf-8") as f:
                    cfg = json.load(f)
                    if not token:
                        token = cfg.get("otlp_token")
                    if not instance_id:
                        instance_id = cfg.get("instance_id")
                    if not endpoint:
                        endpoint = cfg.get("otlp_endpoint")
            except Exception as e:
                logger.debug("Failed to read ~/.cinemapilot/telemetry_config.json: %s", e)

    instance_id = instance_id or "3419920"
    endpoint = endpoint or "https://prometheus-prod-43-prod-ap-south-1.grafana.net/otlp/v1/metrics"
    return token, instance_id, endpoint


def init_telemetry() -> metrics.Meter:
    """
    Initialize OpenTelemetry MeterProvider and register custom metric instruments.
    """
    global _METER_PROVIDER, _METER, _AGENT_DURATION, _AGENT_FAILURES, _CASCADE_STATUS, _ASSET_DURATION

    if _METER is not None:
        return _METER

    token, instance_id, endpoint = _load_credentials()
    resource = Resource.create({"service.name": "cinemapilot", "service.version": "1.0.0"})

    if token:
        auth_bytes = f"{instance_id}:{token}".encode("utf-8")
        auth_header = base64.b64encode(auth_bytes).decode("utf-8")
        exporter = OTLPMetricExporter(
            endpoint=endpoint,
            headers={"Authorization": f"Basic {auth_header}"},
            timeout=10,
        )
        print(f"[telemetry] Initialized OpenTelemetry OTLP exporter -> {endpoint} (instance: {instance_id})")
    else:
        print("[telemetry] GRAFANA_CLOUD_OTLP_TOKEN not set; falling back to local ConsoleMetricExporter")
        exporter = ConsoleMetricExporter()

    reader = PeriodicExportingMetricReader(exporter, export_interval_millis=2000)
    _METER_PROVIDER = MeterProvider(resource=resource, metric_readers=[reader])
    metrics.set_meter_provider(_METER_PROVIDER)
    _METER = metrics.get_meter("cinemapilot.meter", "1.0.0")

    # 1. Agent execution duration (histogram)
    _AGENT_DURATION = _METER.create_histogram(
        name="cinemapilot_agent_duration_seconds",
        description="Duration of CinemaPilot agent execution in seconds",
        unit="s",
    )

    # 2. Agent failures (counter)
    _AGENT_FAILURES = _METER.create_counter(
        name="cinemapilot_agent_failures_total",
        description="Total count of CinemaPilot agent execution failures",
        unit="1",
    )

    # 3. Cascade execution status (gauge, 1=healthy, 0=degraded)
    _CASCADE_STATUS = _METER.create_gauge(
        name="cinemapilot_cascade_status",
        description="Health status of a cascade run (1=healthy, 0=degraded)",
        unit="1",
    )

    # 4. Asset generation duration (histogram)
    _ASSET_DURATION = _METER.create_histogram(
        name="cinemapilot_asset_generation_duration_seconds",
        description="Duration of creative asset generation (images, audio, TTS) in seconds",
        unit="s",
    )

    return _METER


def flush_telemetry(timeout_millis: int = 5000) -> None:
    """
    Force flush any queued metric exports immediately.
    """
    global _METER_PROVIDER
    if _METER_PROVIDER:
        try:
            _METER_PROVIDER.force_flush(timeout_millis=timeout_millis)
        except Exception as exc:
            logger.warning("Telemetry flush failed: %s", exc)


def record_agent_failure(cascade_id: str, agent_name: str = "unknown", error_type: str = "Exception") -> None:
    """
    Explicitly record an agent failure against a cascade correlation ID.
    Increments the in-process cascade failure tracker and Prometheus counter.
    """
    init_telemetry()
    cascade_str = str(cascade_id or "standalone")
    _CASCADE_FAILURES[cascade_str] = _CASCADE_FAILURES.get(cascade_str, 0) + 1
    if _AGENT_FAILURES:
        _AGENT_FAILURES.add(
            1,
            {
                "agent": agent_name,
                "cascade_id": cascade_str,
                "error_type": error_type,
            },
        )


def get_cascade_failure_count(cascade_id: str) -> int:
    """Return the total number of agent failures recorded for a cascade ID."""
    return _CASCADE_FAILURES.get(str(cascade_id), 0)


def has_cascade_failures(cascade_id: str) -> bool:
    """Return True if any agent failures occurred during this cascade run."""
    return get_cascade_failure_count(cascade_id) > 0


def record_cascade_status(cascade_id: str, is_healthy: bool = True) -> None:
    """
    Record cascade status (1=healthy, 0=degraded).
    """
    init_telemetry()
    val = 1 if is_healthy else 0
    if _CASCADE_STATUS:
        _CASCADE_STATUS.set(val, {"cascade_id": str(cascade_id)})


def instrument_agent(agent_name: str, asset_type: str | None = None) -> Callable:
    """
    Decorator to instrument CinemaPilot agents with execution timing, failure metrics,
    and asset generation histograms.

    Args:
        agent_name: Name of the agent (e.g. "budget_agent", "storyboard_agent").
        asset_type: Optional asset type if this agent generates assets ("image", "audio", "voice").
    """
    def decorator(func: Callable) -> Callable:
        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            init_telemetry()

            # Extract scene_id and cascade_id if present
            scene_id = kwargs.get("scene_id")
            cascade_id = kwargs.get("cascade_id")

            if not scene_id and len(args) > 0 and isinstance(args[0], str):
                scene_id = args[0]
            if not cascade_id and len(args) > 1 and isinstance(args[1], str):
                cascade_id = args[1]

            scene_str = str(scene_id or "global")
            cascade_str = str(cascade_id or "standalone")

            t0 = time.perf_counter()
            try:
                result = func(*args, **kwargs)
                elapsed = time.perf_counter() - t0

                # Record duration
                if _AGENT_DURATION:
                    _AGENT_DURATION.record(
                        elapsed,
                        {
                            "agent": agent_name,
                            "scene_id": scene_str,
                            "cascade_id": cascade_str,
                        },
                    )

                # Record asset generation duration if applicable
                if asset_type and _ASSET_DURATION:
                    _ASSET_DURATION.record(
                        elapsed,
                        {
                            "agent": agent_name,
                            "scene_id": scene_str,
                            "asset_type": asset_type,
                        },
                    )

                return result
            except Exception as exc:
                elapsed = time.perf_counter() - t0
                _CASCADE_FAILURES[cascade_str] = _CASCADE_FAILURES.get(cascade_str, 0) + 1
                if _AGENT_FAILURES:
                    _AGENT_FAILURES.add(
                        1,
                        {
                            "agent": agent_name,
                            "scene_id": scene_str,
                            "cascade_id": cascade_str,
                            "error_type": type(exc).__name__,
                        },
                    )
                raise

        return wrapper
    return decorator
