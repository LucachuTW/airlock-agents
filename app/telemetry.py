"""Observability in one place: OTel traces, Prometheus metrics, Langfuse LLM traces.

Swapping any vendor means touching only this module.
"""

import logging
import os

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from prometheus_client import Counter, Histogram, make_asgi_app

from app.config import settings

log = logging.getLogger("agentic")

RUNS_TOTAL = Counter("runs_total", "Finished agent runs", ["agent", "status"])
RUN_LATENCY = Histogram(
    "run_latency_seconds", "Agent run latency", ["agent"],
    buckets=(1, 2, 5, 10, 20, 40, 80, 160, 320),
)
TOKENS_TOTAL = Counter("tokens_total", "LLM tokens", ["direction"])
TOOL_CALLS = Counter("tool_calls_total", "MCP tool calls", ["tool", "status"])
APPROVALS_TOTAL = Counter("approvals_total", "Approval decisions", ["decision"])
APPROVAL_WAIT = Histogram(
    "approval_wait_seconds", "Time from approval request to human decision",
    buckets=(60, 300, 900, 3600, 14400, 86400),
)

metrics_app = make_asgi_app()


def setup_otel(app, engine) -> None:
    """Tracer provider is always installed (so runs get linkable trace ids);
    spans are exported only when an OTLP endpoint is configured."""
    provider = TracerProvider(resource=Resource.create({"service.name": "agentic-api"}))
    if settings.otel_exporter_otlp_endpoint:
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter

        provider.add_span_processor(
            BatchSpanProcessor(OTLPSpanExporter(endpoint=settings.otel_exporter_otlp_endpoint))
        )
    trace.set_tracer_provider(provider)

    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
    from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
    from opentelemetry.instrumentation.redis import RedisInstrumentor
    from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor

    FastAPIInstrumentor.instrument_app(app, excluded_urls="health,metrics")
    SQLAlchemyInstrumentor().instrument(engine=engine.sync_engine)
    HTTPXClientInstrumentor().instrument()
    RedisInstrumentor().instrument()


def langfuse_handler():
    """LangChain callback for Langfuse, or None when not configured."""
    if not (settings.langfuse_host and settings.langfuse_public_key):
        return None
    os.environ.setdefault("LANGFUSE_HOST", settings.langfuse_host)
    os.environ.setdefault("LANGFUSE_PUBLIC_KEY", settings.langfuse_public_key)
    os.environ.setdefault("LANGFUSE_SECRET_KEY", settings.langfuse_secret_key)
    try:
        from langfuse.langchain import CallbackHandler

        return CallbackHandler()
    except Exception:  # tracing must never break a run
        log.exception("langfuse handler init failed; continuing without LLM tracing")
        return None
