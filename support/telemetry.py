"""Export one Phoenix trace per turn (O-1, O-2, O-3, O-4)."""

import urllib.request

from openinference.instrumentation.google_genai import GoogleGenAIInstrumentor
from openinference.semconv.resource import ResourceAttributes
from openinference.semconv.trace import OpenInferenceSpanKindValues, SpanAttributes
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import SpanProcessor, TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

OTLP_ENDPOINT = "http://127.0.0.1:6006/v1/traces"
PHOENIX_HEALTH = "http://127.0.0.1:6006/healthz"
PROJECT = "default"
_ready = False

# ADK names the child spans. Phoenix reads the OpenInference kind attribute.
_KINDS = (
    ("agent.turn", OpenInferenceSpanKindValues.CHAIN.value),
    ("invoke_agent", OpenInferenceSpanKindValues.AGENT.value),
    ("call_llm", OpenInferenceSpanKindValues.LLM.value),
    ("execute_tool", OpenInferenceSpanKindValues.TOOL.value),
    ("security.sanitize", OpenInferenceSpanKindValues.GUARDRAIL.value),
    ("security.a2a_judge", OpenInferenceSpanKindValues.GUARDRAIL.value),
    ("guardrail.check", OpenInferenceSpanKindValues.GUARDRAIL.value),
    ("memory.recall", OpenInferenceSpanKindValues.RETRIEVER.value),
    ("security.a2a_mask", OpenInferenceSpanKindValues.GUARDRAIL.value),
    ("memory.save", OpenInferenceSpanKindValues.TOOL.value),
)


class _KindProcessor(SpanProcessor):
    def on_start(self, span, parent_context=None):
        name = span.name
        for prefix, kind in _KINDS:
            if name == prefix or name.startswith(prefix + " "):
                span.set_attribute(SpanAttributes.OPENINFERENCE_SPAN_KIND, kind)
                return

    def on_end(self, span):
        return None

    def shutdown(self):
        return None

    def force_flush(self, timeout_millis=30000):
        return True


def setup_telemetry():
    """Install the global tracer before any agent is built (O-3)."""
    global _ready
    if _ready:
        return
    provider = TracerProvider(resource=Resource.create({ResourceAttributes.PROJECT_NAME: PROJECT}))
    provider.add_span_processor(_KindProcessor())
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=OTLP_ENDPOINT)))
    trace.set_tracer_provider(provider)
    GoogleGenAIInstrumentor().instrument(tracer_provider=provider)
    _ready = True


def flush_traces():
    """Push ended spans to Phoenix before the process exits."""
    provider = trace.get_tracer_provider()
    flush = getattr(provider, "force_flush", None)
    if flush is not None and not flush():
        raise RuntimeError("Phoenix did not accept the trace export")


def phoenix_is_up():
    try:
        with urllib.request.urlopen(PHOENIX_HEALTH, timeout=2) as response:
            return response.status == 200
    except Exception:
        return False


def trace_url(trace_id):
    # Phoenix routes /projects/<id> by its internal id, not the name; this redirect takes the OTel trace id.
    return f"http://localhost:6006/redirects/traces/{trace_id}"
