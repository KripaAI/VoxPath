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
        from phoenix.otel import register
    except ImportError:
        log.warning(
            "PHOENIX_TRACING=true but tracing packages are not installed; "
            "run: pip install -r requirements-tracing.txt. Continuing without tracing."
        )
        return False
    try:
        endpoint = os.getenv("PHOENIX_COLLECTOR_ENDPOINT", "http://localhost:6006/v1/traces")
        tracer_provider = register(
            endpoint=endpoint,
            project_name=os.getenv("PHOENIX_PROJECT_NAME", "voxpath"),
            batch=True,  # export in the background so chats never wait on Phoenix
            verbose=False,
            # Not global: FastAPI and fastmcp emit spans for every request to a global
            # provider, which would bury agent traces under /health polls (every 6 s).
            set_global_tracer_provider=False,
        )
        hide = not user_content_logging()
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


def trace_context(session_id: str | None, user_id: str | None):
    """Tag spans created inside this block with the conversation and user ids."""
    if not _enabled:
        return contextlib.nullcontext()
    from openinference.instrumentation import using_attributes

    return using_attributes(session_id=session_id or "", user_id=user_id or "")
