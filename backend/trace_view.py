"""Make agent traces readable before they are sent to Phoenix.

A raw LangGraph ReAct run produces ~16 spans per chat turn, most of them
framework plumbing (RunnableSequence, Prompt, should_continue, ...) under
generic names. This span processor sits in front of the exporter and:

  * hides the plumbing spans, re-attaching their children to the nearest
    visible ancestor so the tree stays connected;
  * renames what remains so the flow of control reads at a glance:
        💬 VoxPath chat                                (root: question -> answer)
        ├─ 🧠 VoxPath Agent · decide → get_stock_price  (model call that chose tools)
        ├─ 🔧 get_stock_price                           (tool call: input -> output)
        └─ 🧠 VoxPath Agent · answer                    (model call that replied)
    or, when no tool was called at all:
        └─ 🧠 VoxPath Agent · answer directly (no tools used)
  * shows the root's input/output as the user's question and the final answer
    instead of the whole serialized message list.

Only presentation changes; the agent and the span data it records are untouched.
Any error while rewriting a span exports the original span unchanged.
"""

from __future__ import annotations

import json
import re
import threading

from opentelemetry.sdk.trace import ReadableSpan, SpanProcessor
from opentelemetry.trace import SpanContext, StatusCode

AGENT_NAME = "VoxPath Agent"
ROOT_NAME = "💬 VoxPath chat"

# LangGraph/LangChain plumbing spans of the ReAct agent. Hidden by name; model
# (LLM) and tool (TOOL) spans are never hidden, whatever they are called.
HIDDEN_SPAN_NAMES = frozenset(
    {"agent", "tools", "RunnableSequence", "Prompt", "should_continue", "pre_model_hook", "call_model"}
)
_GRAPH_ROOT_NAMES = frozenset({"LangGraph"})

_KIND = "openinference.span.kind"
_TOOL_CALL_NAME = re.compile(r"^llm\.output_messages\.\d+\.message\.tool_calls\.\d+\.tool_call\.function\.name$")


class ReadableTraceProcessor(SpanProcessor):
    """Wraps the real span processor (e.g. a BatchSpanProcessor to Phoenix)."""

    def __init__(self, inner: SpanProcessor, rewrite_root_io: bool = True):
        self._inner = inner
        self._rewrite_root_io = rewrite_root_io
        self._lock = threading.Lock()
        self._parent_of: dict[int, int | None] = {}
        self._hidden: set[int] = set()
        self._spans_in_trace: dict[int, list[int]] = {}
        self._traces_with_tool_calls: set[int] = set()

    # -- SpanProcessor interface -------------------------------------------------

    def on_start(self, span, parent_context=None) -> None:
        ctx = span.get_span_context()
        with self._lock:
            self._parent_of[ctx.span_id] = span.parent.span_id if span.parent else None
            self._spans_in_trace.setdefault(ctx.trace_id, []).append(ctx.span_id)
            if span.name in HIDDEN_SPAN_NAMES:
                self._hidden.add(ctx.span_id)
        self._inner.on_start(span, parent_context=parent_context)

    def on_end(self, span: ReadableSpan) -> None:
        ctx = span.context
        kind = (span.attributes or {}).get(_KIND)
        with self._lock:
            hidden = ctx.span_id in self._hidden and kind not in ("LLM", "TOOL")
            parent = self._visible_parent(ctx.span_id)
            if kind == "TOOL":
                self._traces_with_tool_calls.add(ctx.trace_id)
            used_tools = ctx.trace_id in self._traces_with_tool_calls
            if self._parent_of.get(ctx.span_id) is None:
                # The root ends last: forget this trace's bookkeeping.
                for span_id in self._spans_in_trace.pop(ctx.trace_id, []):
                    self._parent_of.pop(span_id, None)
                    self._hidden.discard(span_id)
                self._traces_with_tool_calls.discard(ctx.trace_id)
        if hidden:
            return
        try:
            span = self._rewrite(span, parent, used_tools)
        except Exception:
            pass  # presentation must never cost us a span
        self._inner.on_end(span)

    def shutdown(self) -> None:
        self._inner.shutdown()

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return self._inner.force_flush(timeout_millis)

    # -- helpers -----------------------------------------------------------------

    def _visible_parent(self, span_id: int) -> int | None:
        parent = self._parent_of.get(span_id)
        while parent is not None and parent in self._hidden:
            parent = self._parent_of.get(parent)
        return parent

    def _rewrite(self, span: ReadableSpan, parent: int | None, used_tools: bool = True) -> ReadableSpan:
        attributes = dict(span.attributes or {})
        kind = attributes.get(_KIND)
        name = span.name

        if kind == "LLM":
            tools = sorted({v for k, v in attributes.items() if _TOOL_CALL_NAME.match(k)})
            if span.status.status_code == StatusCode.ERROR:
                name = f"🧠 {AGENT_NAME} · error"
            elif tools:
                name = f"🧠 {AGENT_NAME} · decide → {', '.join(tools)}"
            elif used_tools:
                name = f"🧠 {AGENT_NAME} · answer"
            else:
                # Say it outright, so a missing tool step isn't mistaken for missing data.
                name = f"🧠 {AGENT_NAME} · answer directly (no tools used)"
        elif kind == "TOOL":
            name = f"🔧 {span.name}"
        elif parent is None and span.name in _GRAPH_ROOT_NAMES:
            # Name the user in the trace list (user.id carries the username).
            user = attributes.get("user.id")
            name = f"{ROOT_NAME} · {user}" if user else ROOT_NAME
            attributes[_KIND] = "AGENT"
            if self._rewrite_root_io:
                _show_question_and_answer(attributes)

        original_parent = span.parent
        new_parent = None
        if parent is not None:
            new_parent = SpanContext(
                trace_id=span.context.trace_id,
                span_id=parent,
                is_remote=False,
                trace_flags=span.context.trace_flags,
            )
        if name == span.name and attributes == dict(span.attributes or {}) and (
            (original_parent.span_id if original_parent else None) == parent
        ):
            return span
        return ReadableSpan(
            name=name,
            context=span.context,
            parent=new_parent,
            resource=span.resource,
            attributes=attributes,
            events=span.events,
            links=span.links,
            kind=span.kind,
            status=span.status,
            start_time=span.start_time,
            end_time=span.end_time,
            instrumentation_scope=span.instrumentation_scope,
        )


def _message_text(serialized: str, message_type: str) -> str | None:
    """Content of the last message of `message_type` in a serialized LangGraph state."""
    messages = json.loads(serialized).get("messages", [])
    for message in reversed(messages):
        if message.get("type") == message_type:
            content = message.get("data", {}).get("content")
            if isinstance(content, str) and content:
                return content
    return None


def _show_question_and_answer(attributes: dict) -> None:
    for key, message_type in (("input", "human"), ("output", "ai")):
        value = attributes.get(f"{key}.value")
        if not isinstance(value, str):
            continue
        try:
            text = _message_text(value, message_type)
        except (ValueError, AttributeError):
            continue
        if text:
            attributes[f"{key}.value"] = text
            attributes[f"{key}.mime_type"] = "text/plain"
