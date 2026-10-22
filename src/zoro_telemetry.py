"""Azure Application Insights and OpenTelemetry telemetry for Zoro Assistant."""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

_ENV_PATH = Path(__file__).resolve().parent.parent / ".env"
if _ENV_PATH.exists():
    load_dotenv(_ENV_PATH)

# Polyfill semantic conventions if running with pinned opentelemetry-api 1.39
try:
    from opentelemetry.semconv.attributes import otel_attributes
    if not hasattr(otel_attributes, "OTEL_EVENT_NAME"):
        setattr(otel_attributes, "OTEL_EVENT_NAME", "event.name")
except Exception:
    pass

_logger = logging.getLogger("zoro.telemetry")
_initialized = False
_tracer = None


def init_telemetry() -> None:
    """Initialize Azure Monitor OpenTelemetry if configured."""
    global _initialized, _tracer
    if _initialized:
        return
    conn_str = os.getenv("APPLICATIONINSIGHTS_CONNECTION_STRING")
    if not conn_str:
        _logger.debug("APPLICATIONINSIGHTS_CONNECTION_STRING not set; telemetry disabled.")
        _initialized = True
        return

    try:
        from azure.monitor.opentelemetry import configure_azure_monitor
        configure_azure_monitor(
            connection_string=conn_str,
            instrumentation_options={"logging": {"enabled": False}},
            disable_offline_storage=False,
        )
        from opentelemetry import trace
        _tracer = trace.get_tracer("zoro")
        _logger.info("Azure Application Insights telemetry initialized successfully.")
    except Exception as e:
        _logger.warning("Failed to initialize Azure Application Insights: %s", e)
    finally:
        _initialized = True


def get_tracer():
    global _tracer
    if not _initialized:
        init_telemetry()
    if _tracer is None:
        try:
            from opentelemetry import trace
            _tracer = trace.get_tracer("zoro")
        except Exception:
            _tracer = None
    return _tracer


def track_event(name: str, properties: dict[str, Any] | None = None) -> None:
    """Track a custom business or assistant event as a span/event in Azure Monitor."""
    try:
        tracer = get_tracer()
        if tracer:
            with tracer.start_as_current_span(name) as span:
                if properties:
                    for k, v in properties.items():
                        span.set_attribute(str(k), str(v))
    except Exception:
        pass


def track_exception(exc: Exception, properties: dict[str, Any] | None = None) -> None:
    """Record an exception in Azure Monitor."""
    try:
        tracer = get_tracer()
        if tracer:
            with tracer.start_as_current_span("exception") as span:
                span.record_exception(exc)
                if properties:
                    for k, v in properties.items():
                        span.set_attribute(str(k), str(v))
    except Exception:
        pass
