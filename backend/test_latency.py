"""Latency fixes: idle-only connection checks, one save per turn, lazy stock lookup."""

import asyncio
import time

from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver

import agent
import db_pool
import services
import stock_resolver


# --------------------------------------------------------------------------- pool checks


class _FakeConn:
    def __init__(self, alive=True):
        self.alive = alive
        self.pings = 0

    async def execute(self, sql, *args, **kwargs):
        self.pings += 1
        if not self.alive:
            raise ConnectionError("SSL connection has been closed unexpectedly")

    # psycopg_pool.check_connection inspects these
    @property
    def autocommit(self):
        return True


def _run(coro):
    return asyncio.run(coro)


def test_recently_used_connection_is_not_pinged(monkeypatch):
    conn = _FakeConn()
    _run(db_pool.mark_returned(conn))
    pinged = []
    monkeypatch.setattr(db_pool.AsyncConnectionPool, "check_connection", staticmethod(lambda c: pinged.append(c)))
    _run(db_pool.check_if_idle(conn))
    assert pinged == []  # no round trip for a connection used moments ago


def test_idle_or_unknown_connection_is_pinged(monkeypatch):
    pinged = []

    async def ping(c):
        pinged.append(c)

    monkeypatch.setattr(db_pool.AsyncConnectionPool, "check_connection", staticmethod(ping))
    fresh = _FakeConn()
    _run(db_pool.check_if_idle(fresh))  # never returned before -> ping
    idle = _FakeConn()
    db_pool._last_returned[idle] = time.monotonic() - db_pool.STALE_AFTER_SECONDS - 1
    _run(db_pool.check_if_idle(idle))
    assert pinged == [fresh, idle]


def test_dead_idle_connection_fails_the_check(monkeypatch):
    async def ping(c):
        await c.execute("SELECT 1")

    monkeypatch.setattr(db_pool.AsyncConnectionPool, "check_connection", staticmethod(ping))
    dead = _FakeConn(alive=False)
    db_pool._last_returned[dead] = time.monotonic() - db_pool.STALE_AFTER_SECONDS - 1
    try:
        _run(db_pool.check_if_idle(dead))
        raise AssertionError("a dead connection must fail the check so the pool replaces it")
    except ConnectionError:
        pass


# --------------------------------------------------------------------------- one save per turn


class ChatOpenAI(FakeMessagesListChatModel):
    def bind_tools(self, tools, **kwargs):
        return self

    def get_num_tokens_from_messages(self, messages, tools=None):
        return 10


def test_conversation_history_still_saved_with_one_save_per_turn(monkeypatch):
    monkeypatch.setattr(services, "get_stock_price_smart", lambda t: {"result": f"{t}: 100 INR"})
    monkeypatch.setattr(services, "build_llm", lambda: ChatOpenAI(responses=[
        AIMessage(content="", tool_calls=[{"name": "get_stock_price", "args": {"ticker": "TCS"}, "id": "c1"}]),
        AIMessage(content="TCS is at 100."),
        AIMessage(content="You asked about TCS."),
    ]))
    saver = InMemorySaver()
    puts = []
    real_aput = saver.aput

    async def counting_aput(*args, **kwargs):
        puts.append(1)
        return await real_aput(*args, **kwargs)

    monkeypatch.setattr(saver, "aput", counting_aput)

    async def two_turns():
        await agent.init_agent(saver, None)
        await agent.agent_chat("price of TCS?", thread_id="t", user_id="u")
        first_turn_saves = len(puts)
        await agent.agent_chat("what did I ask?", thread_id="t", user_id="u")
        state = await agent._agent.aget_state({"configurable": {"thread_id": "t"}})
        return first_turn_saves, state

    first_turn_saves, state = asyncio.run(two_turns())
    assert first_turn_saves == 1  # was one per graph step (5+) before
    contents = [m.content for m in state.values["messages"]]
    assert contents[0] == "price of TCS?"  # turn 1 survived into turn 2
    assert "TCS is at 100." in contents and contents[-1] == "You asked about TCS."


# --------------------------------------------------------------------------- lazy stock lookup


def test_yahoo_search_skipped_when_nse_match_has_a_price(monkeypatch):
    searches = []
    monkeypatch.setattr(stock_resolver, "nse_matches", lambda q: ["TATATECH"])
    monkeypatch.setattr(stock_resolver, "company_name", lambda s: "TATA TECHNOLOGIES LIMITED")
    monkeypatch.setattr(services.yf, "Search", lambda q: searches.append(q))
    monkeypatch.setattr(services, "_price_for_symbol", lambda s: 657.35 if s == "TATATECH.NS" else None)
    assert services.get_stock_price("Tata technology") == "TATA TECHNOLOGIES LIMITED (TATATECH.NS): 657.35 INR"
    assert searches == []


def test_yahoo_search_used_when_nse_has_no_price(monkeypatch):
    class _Search:
        def __init__(self, q):
            self.quotes = [{"symbol": "ETERNAL.NS"}]

    monkeypatch.setattr(stock_resolver, "nse_matches", lambda q: [])
    monkeypatch.setattr(stock_resolver, "company_name", lambda s: None)
    monkeypatch.setattr(services.yf, "Search", _Search)
    monkeypatch.setattr(services, "_price_for_symbol", lambda s: 250.0 if s == "ETERNAL.NS" else None)
    assert services.get_stock_price("Zomato") == "ETERNAL.NS: 250.00 INR"
