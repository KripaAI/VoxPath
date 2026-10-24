"""Optional agent execution tracing with Arize Phoenix.

Off unless PHOENIX_TRACING=true. When on, every chat turn is recorded as a trace
in the Phoenix UI (http://localhost:6006): the LangGraph run, each OpenAI call
(prompt, response, tokens, latency) and each tool call (input and output),
grouped by conversation (thread id) and user.

Tracing must never take the app down: if the tracing packages are missing or
setup fails, the app logs a warning and runs untraced. If the Phoenix server is
not running, spans are dropped in the background and chats are unaffected.

Settings (backend/.env):
  PHOENIX_TRACING=true                       turn tracing on
  PHOENIX_COLLECTOR_ENDPOINT=http://localhost:6006/v1/traces
  PHOENIX_PROJECT_NAME=voxpath
  LOG_USER_CONTENT=false                     also hides message text in traces
"""

from __future__ import annotations

import contextlib
import contextvars
import os

from logging_config import get_logger, user_content_logging

log = get_logger("voxpath.tracing")

_enabled = False


def tracing_requested() -> bool:
    return os.getenv("PHOENIX_TRACING", "false").strip().lower() in ("true", "1", "yes")


def setup_tracing() -> bool:
    """Instrument LangChain/LangGraph to export traces to Phoenix. Returns whether tracing is on."""
    global _enabled
    if _enabled or not tracing_requested():
        return _enabled
    try:
        from openinference.instrumentation import TraceConfig
        from openinference.instrumentation.langchain import LangChainInstrumentor
        from phoenix.otel import PROJECT_NAME, BatchSpanProcessor, Resource, TracerProvider

        from trace_view import ReadableTraceProcessor
    except ImportError:
        log.warning(
            "PHOENIX_TRACING=true but tracing packages are not installed; "
            "run: pip install -r requirements-tracing.txt. Continuing without tracing."
        )
        return False
    try:
        endpoint = os.getenv("PHOENIX_COLLECTOR_ENDPOINT", "http://localhost:6006/v1/traces")
        hide = not user_content_logging()
        # Built by hand rather than with phoenix.otel.register() so the readable-trace
        # processor can sit in front of the exporter. It is deliberately NOT the global
        # provider: FastAPI and fastmcp emit spans for every request to a global
        # provider, which would bury agent traces under /health polls (every 6 s).
        tracer_provider = TracerProvider(
            resource=Resource({PROJECT_NAME: os.getenv("PHOENIX_PROJECT_NAME", "voxpath")}),
            verbose=False,
        )
        tracer_provider.add_span_processor(
            ReadableTraceProcessor(
                BatchSpanProcessor(endpoint=endpoint),  # background export: chats never wait on Phoenix
                rewrite_root_io=not hide,
            )
        )
        config = TraceConfig(
            hide_inputs=hide,
            hide_outputs=hide,
            hide_input_messages=hide,
            hide_output_messages=hide,
        )
        LangChainInstrumentor().instrument(tracer_provider=tracer_provider, config=config)
    except Exception as error:
        log.warning("tracing setup failed, continuing without tracing: %s", error)
        return False
    _enabled = True
    log.info("Phoenix tracing on -> %s (message content %s)", endpoint, "hidden" if hide else "visible")
    return True


# Where the trace id of the chat turn now running goes, so the turn's database
# row can link to its Phoenix trace. A context variable, so concurrent turns
# each get their own.
_trace_id_sink: contextvars.ContextVar[dict | None] = contextvars.ContextVar("trace_id_sink", default=None)


@contextlib.contextmanager
def capture_trace_id():
    """Yield a dict that gets "trace_id" (32 hex chars, as Phoenix shows it) once
    the first root span starts inside the block. Stays empty when tracing is off."""
    holder: dict = {}
    token = _trace_id_sink.set(holder)
    try:
        yield holder
    finally:
        _trace_id_sink.reset(token)


def note_root_span(trace_id: int) -> None:
    """Called by the span processor when a trace's root span starts."""
    holder = _trace_id_sink.get()
    if holder is not None and "trace_id" not in holder:
        holder["trace_id"] = format(trace_id, "032x")


def trace_context(session_id: str | None, user_id: str | None, username: str | None = None):
    """Tag spans created inside this block with the conversation and the user.

    Phoenix's user field shows the readable username (falling back to the internal
    id); the internal id is kept in the metadata, as that is what memories use.
    """
    if not _enabled:
        return contextlib.nullcontext()
    from openinference.instrumentation import using_attributes

    return using_attributes(
        session_id=session_id or "",
        user_id=username or user_id or "",
        metadata={"user_id": user_id or "", "username": username or ""},
    )
