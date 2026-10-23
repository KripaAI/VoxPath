"""The readable trace: a real agent run must export a clean, connected tree."""

import asyncio

import pytest

pytest.importorskip("openinference.instrumentation.langchain")  # only with requirements-tracing.txt

from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage
from openinference.instrumentation.langchain import LangChainInstrumentor
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

import agent
import services
import tracing
from trace_view import HIDDEN_SPAN_NAMES, ROOT_NAME, ReadableTraceProcessor


class ChatOpenAI(FakeMessagesListChatModel):
    """Scripted stand-in for the real model (same class name, so spans look identical)."""

    def bind_tools(self, tools, **kwargs):
        return self

    def get_num_tokens_from_messages(self, messages, tools=None):
        return 10


def _run_traced_chat(monkeypatch, rewrite_root_io=True, responses=None):
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(ReadableTraceProcessor(SimpleSpanProcessor(exporter), rewrite_root_io=rewrite_root_io))
    instrumentor = LangChainInstrumentor()
    if instrumentor.is_instrumented_by_opentelemetry:
        instrumentor.uninstrument()  # don't inherit another test's (or the app's) provider
    instrumentor.instrument(tracer_provider=provider)
    monkeypatch.setattr(tracing, "_enabled", True)
    monkeypatch.setattr(services, "get_stock_price_smart", lambda ticker: {"result": f"{ticker}: 657.35 INR"})
    monkeypatch.setattr(services, "build_llm", lambda: ChatOpenAI(responses=responses or [
        AIMessage(content="", tool_calls=[{"name": "get_stock_price", "args": {"ticker": "Tata technology"}, "id": "c1"}]),
        AIMessage(content="Tata Technologies is trading at 657.35."),
    ]))
    try:
        async def chat():
            await agent.init_agent(None, None)
            return await agent.agent_chat("What is the stock price of Tata technology?", thread_id="t-9", user_id="u-1")

        reply = asyncio.run(chat())
    finally:
        instrumentor.uninstrument()
    return reply, exporter.get_finished_spans()


def test_agent_run_exports_a_clean_readable_tree(monkeypatch):
    reply, spans = _run_traced_chat(monkeypatch)
    assert reply == "Tata Technologies is trading at 657.35."

    # Plumbing is gone; only the steps a person cares about remain.
    names = [s.name for s in sorted(spans, key=lambda s: s.start_time)]
    assert not HIDDEN_SPAN_NAMES & set(names)
    assert names == [
        ROOT_NAME,
        "🧠 VoxPath Agent · decide → get_stock_price",
        "🔧 get_stock_price",
        "🧠 VoxPath Agent · answer",
    ]

    # One root; every other step hangs directly off it (no orphans, no broken links).
    root = next(s for s in spans if s.name == ROOT_NAME)
    assert root.parent is None
    for s in spans:
        if s is not root:
            assert s.parent.span_id == root.context.span_id
            assert s.context.trace_id == root.context.trace_id

    # The trace list shows the question and the answer, tagged with conversation and user.
    assert root.attributes["input.value"] == "What is the stock price of Tata technology?"
    assert root.attributes["output.value"] == "Tata Technologies is trading at 657.35."
    assert root.attributes["session.id"] == "t-9" and root.attributes["user.id"] == "u-1"

    # Tool span keeps its real input/output.
    tool = next(s for s in spans if s.name == "🔧 get_stock_price")
    assert tool.attributes["input.value"] == "Tata technology"
    assert "657.35" in tool.attributes["output.value"]


def test_answer_without_tools_says_so(monkeypatch):
    # Like "station code of Hajipur?" asked again: answered from the conversation, no tool.
    reply, spans = _run_traced_chat(monkeypatch, responses=[AIMessage(content="Hajipur is HJP.")])
    assert reply == "Hajipur is HJP."
    names = [s.name for s in sorted(spans, key=lambda s: s.start_time)]
    assert names == [ROOT_NAME, "🧠 VoxPath Agent · answer directly (no tools used)"]


def test_root_io_left_alone_when_content_is_hidden(monkeypatch):
    _, spans = _run_traced_chat(monkeypatch, rewrite_root_io=False)
    root = next(s for s in spans if s.name == ROOT_NAME)
    assert root.attributes["input.mime_type"] == "application/json"


def test_a_rewrite_error_still_exports_the_span(monkeypatch):
    import trace_view

    def broken(self, span, parent, used_tools=True):
        raise RuntimeError("bad attribute")

    monkeypatch.setattr(trace_view.ReadableTraceProcessor, "_rewrite", broken)
    _, spans = _run_traced_chat(monkeypatch)
    assert any(s.name == "get_stock_price" for s in spans)  # exported, just not renamed


def test_tracer_provider_is_not_global(monkeypatch):
    pytest.importorskip("phoenix.otel")
    from opentelemetry import trace

    monkeypatch.setenv("PHOENIX_TRACING", "true")
    monkeypatch.setenv("PHOENIX_COLLECTOR_ENDPOINT", "http://127.0.0.1:9/v1/traces")  # nothing listens here
    monkeypatch.setattr(tracing, "_enabled", False)
    before = trace.get_tracer_provider()
    try:
        assert tracing.setup_tracing() is True
        assert trace.get_tracer_provider() is before  # FastAPI/fastmcp spans stay out of Phoenix
    finally:
        LangChainInstrumentor().uninstrument()
