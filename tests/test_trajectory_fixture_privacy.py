"""合成业务隐私场景确实发出敏感工具参数；消费方须自行治理。"""

import asyncio
import json
from pathlib import Path

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter


@pytest.mark.parametrize("operation,customer_value", [("privacy", "4111111111111111"), ("privacy_ssn", "123-45-6789")])
def test_privacy_business_scenario_exports_raw_tool_arguments(monkeypatch, operation, customer_value):
    monkeypatch.syspath_prepend(str(Path(__file__).parent / "fixtures" / "trajectory_agents"))
    from business import Workflow
    provider, exporter = TracerProvider(), InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    workflow = Workflow(provider.get_tracer("privacy-business"), "native_otel", operation)
    try:
        asyncio.run(workflow.tool("balance_read", "synthetic-001"))
        requested = next(span for span in exporter.get_finished_spans() if span.name.endswith(".request"))
        value = json.loads(requested.attributes["gen_ai.tool.call.arguments"])
        assert value["customer_id"] == customer_value
        assert value["identity_number"] == "11010519491231002X"
        assert value["Authorization"] == "Bearer synthetic-privacy-sentinel"
        assert value["Cookie"] == "session=synthetic-privacy-sentinel"
        assert requested.attributes["workflow.arguments_visibility"] == "AVAILABLE"
    finally:
        provider.shutdown()
