"""LangGraph "root node" text agent for VoxPath.

A single ReAct-style agent that routes any text request to the
right tool by intent. Its tools are loaded from the MCP server — the same single
registry the voice agent uses — so there is one tool layer behind both surfaces.
"""

from __future__ import annotations

import functools
import inspect
import json
import os
import time

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.messages.utils import trim_messages
from langchain_core.tools import StructuredTool
from langgraph.prebuilt import create_react_agent

import memory
import services
import tracing
from logging_config import get_logger, log_tool_call, log_tool_readable, redact
from mcp_server import mcp

log = get_logger("voxpath.agent")

SYSTEM_PROMPT = (
    "You are VoxPath, a concise, friendly Indian assistant. Use the available "
    "tools for anything about live news, Indian stock/market prices, or Indian "
    "Railways (PNR status, train running status, schedules, train search, seat "
    "availability, fares). Your training data is out of date, so NEVER answer "
    "those from memory — call the matching tool and answer from its result. "
    "Live data goes stale within minutes: for stock prices, train running status, "
    "seat availability and fares, call the tool again every time they are asked, "
    "even if the same question was answered earlier in this conversation. Fixed "
    "facts, such as a station's code, may be reused from earlier in the conversation. "
    "For stock prices, always pass the company or stock name to get_stock_price "
    "exactly as phrased by the user — never guess or convert names into ticker "
    "symbols from memory. For "
    "railway fares/availability you need IRCTC station codes like NDLS or HJP; if "
    "the user gives a city name, call resolve_station_code first. If a required "
    "detail (PNR, train number, stations, date) is missing, ask for it. Keep "
    "answers short and clear. You have long-term memory across conversations: "
    "call search_memories before answering anything that depends on who the user "
    "is or what they prefer, and when they refer to something they told you "
    "earlier. When a fact they told you has CHANGED, search_memories for it "
    "and call update_memory with its id instead of saving a second memory -- "
    "otherwise both the old and new versions come back later and contradict each "
    "other. Use delete_memory when they ask you to forget something, and "
    "list_memories when they ask what you remember about them. save_memory when "
    "the user states a durable fact about themselves (a name, a preference, a "
    "home station, a standing interest) -- never for the answer to the current "
    "question, and never for live data."
)



AGENT_TOOLS = [
    "get_latest_news",
    "get_stock_price",
    "get_pnr_status",
    "resolve_station_code",
    "get_live_train_status",
    "get_train_schedule",
    "search_trains",
    "check_seat_availability",
    "get_fare",
    "save_memory",
    "search_memories",
    "update_memory",
    "delete_memory",
    "list_memories",
]

_agent = None
_saver = None
_store = None





MAX_HISTORY_TOKENS = int(os.getenv("MAX_HISTORY_TOKENS", "3000"))





MAX_TOOL_CHARS = int(os.getenv("MAX_TOOL_CHARS", "6000"))



DEFAULT_THREAD_ID = "voxpath-default"


async def init_agent(saver=None, store=None) -> None:
    """Build the single app-lifetime agent. Called once from the lifespan.

    Built at startup rather than on the first request so that the checkpointer
    and store (which do not exist until the pool is open) are always attached,
    and so a bad API key or a missing MCP tool fails the boot instead of some
    user's chat message. saver/store are None when DATABASE_URL is unset, which
    yields a working agent with no conversation memory.
    """
    global _agent, _saver, _store
    _saver = saver
    _store = store
    _agent = await _build_agent()


def _cap_tool_messages(messages: list) -> list:
    """Shorten oversized tool results for the model's view only."""
    capped = []
    for message in messages:
        content = message.content
        if isinstance(message, ToolMessage) and isinstance(content, str) and len(content) > MAX_TOOL_CHARS:
            message = message.model_copy(
                update={"content": content[:MAX_TOOL_CHARS] + "\n...[truncated]"}
            )
        capped.append(message)
    return capped


def _drop_orphan_tool_messages(messages: list) -> list:
    """Remove tool results whose originating tool call is no longer present.

    OpenAI rejects the whole request when a tool message does not follow an
    assistant message that called it, so an orphan is not a cosmetic problem:
    it is a 400 for the user.
    """
    offered = set()
    kept = []
    for message in messages:
        if isinstance(message, AIMessage):
            offered.update(call["id"] for call in (message.tool_calls or []) if call.get("id"))
        if isinstance(message, ToolMessage) and message.tool_call_id not in offered:
            log.warning("dropping orphaned tool message %s", message.tool_call_id)
            continue
        kept.append(message)
    return kept


def _last_exchange(messages: list) -> list:
    """The newest human message onward, which is always a valid request shape."""
    for index in range(len(messages) - 1, -1, -1):
        if isinstance(messages[index], HumanMessage):
            return messages[index:]
    return []


def _build_pre_model_hook(llm):
    """Trim the thread to a token budget before each model call.

    Returns `llm_input_messages`, which feeds the LLM without rewriting the
    stored messages, so nothing is lost from the checkpointer. start_on="human"
    matters: dropping an AI tool-call while keeping its ToolMessage leaves an
    orphan that OpenAI rejects outright.
    """

    def trim(state):
        messages = _cap_tool_messages(state["messages"])
        trimmed = trim_messages(
            messages,
            max_tokens=MAX_HISTORY_TOKENS,
            token_counter=llm,
            strategy="last",
            start_on="human",
            end_on=("human", "tool"),
            include_system=True,
            allow_partial=False,
        )



        if not trimmed:
            trimmed = _last_exchange(messages)
        trimmed = _drop_orphan_tool_messages(trimmed)
        if not trimmed:
            trimmed = _last_exchange(messages) or messages[-1:]
        return {"llm_input_messages": trimmed}

    return trim


def _record_tool_call(name: str, tool_input, output, started: float, status: str) -> None:
    """Write one tool call to logs/tool_calls.log (JSON) and tool_calls_readable.log."""
    try:
        duration_ms = (time.perf_counter() - started) * 1000
        tool_input, output = redact(tool_input), redact(output)
        log_tool_call(name, tool_input, output, duration_ms, status)
        body = (
            f"Input: {json.dumps(tool_input, ensure_ascii=False, default=str)}\n"
            f"Output: {json.dumps(output, ensure_ascii=False, default=str)[:2000]}"
        )
        log_tool_readable(name, body, duration_ms, status)
    except Exception as error:  # logging must never break the tool call itself
        log.debug("tool call logging failed for %s: %s", name, error)


def _with_call_logging(name: str, fn):
    """Wrap a tool function so every call is logged.

    functools.wraps keeps the signature and annotations, so the argument schema
    the model sees is identical to the unwrapped function's.
    """
    if inspect.iscoroutinefunction(fn):

        @functools.wraps(fn)
        async def logged_async(*args, **kwargs):
            started = time.perf_counter()
            try:
                result = await fn(*args, **kwargs)
            except Exception as error:
                _record_tool_call(name, kwargs or list(args), repr(error), started, "error")
                raise
            _record_tool_call(name, kwargs or list(args), result, started, "ok")
            return result

        return logged_async

    @functools.wraps(fn)
    def logged_sync(*args, **kwargs):
        started = time.perf_counter()
        try:
            result = fn(*args, **kwargs)
        except Exception as error:
            _record_tool_call(name, kwargs or list(args), repr(error), started, "error")
            raise
        _record_tool_call(name, kwargs or list(args), result, started, "ok")
        return result

    return logged_sync


async def _build_agent():

    tools_map = {tool.name: tool for tool in await mcp.list_tools()}
    lc_tools = []
    for name in AGENT_TOOLS:
        tool = tools_map.get(name)
        if tool is None:
            log.warning("agent tool %s not found in MCP registry", name)
            continue



        fn = _with_call_logging(name, tool.fn)
        if inspect.iscoroutinefunction(fn):
            lc_tool = StructuredTool.from_function(
                coroutine=fn, name=name, description=tool.description
            )
        else:
            lc_tool = StructuredTool.from_function(
                fn, name=name, description=tool.description
            )
        lc_tools.append(lc_tool)
    llm = services.build_llm()
    # History trimming counts tokens before every model call. The first count in a
    # process loads the tokenizer (~1 s); do it now rather than in a user's chat.
    try:
        llm.get_num_tokens_from_messages([HumanMessage(content="warm up")])
    except Exception as error:
        log.debug("tokenizer warm-up skipped: %s", error)
    log.info("LangGraph agent built with %d MCP tools", len(lc_tools))
    return create_react_agent(
        llm,
        lc_tools,
        prompt=SYSTEM_PROMPT,
        checkpointer=_saver,
        store=_store,
        pre_model_hook=_build_pre_model_hook(llm),
    )


async def agent_chat(
    message: str, thread_id: str | None = None, user_id: str | None = None
) -> str:
    """Run one text turn through the LangGraph agent; return the reply text.

    thread_id scopes short-term memory (one conversation); user_id scopes
    long-term memory (every conversation that person has had). There is no auth
    yet so user_id is normally unset, but the memory tools read it from this
    config, so wiring real users later needs no change here.
    """
    if _agent is None:
        raise RuntimeError(
            "agent not initialised - init_agent() must run in the app lifespan"
        )
    config = {
        "configurable": {
            "thread_id": thread_id or DEFAULT_THREAD_ID,
            "user_id": user_id or memory.DEFAULT_USER_ID,
        }
    }
    with tracing.trace_context(config["configurable"]["thread_id"], config["configurable"]["user_id"]):
        # durability="exit": save the conversation once when the turn finishes rather
        # than after every graph step. Each save is a round trip to the remote
        # database, and per-step saves cost ~2 s of a chat turn; the stored
        # history is the same. A crash mid-turn loses only that unfinished turn.
        result = await _agent.ainvoke(
            {"messages": [HumanMessage(content=message)]}, config=config, durability="exit"
        )
    final = result["messages"][-1]
    content = final.content
    return content if isinstance(content, str) else str(content)
