"""The analytics tables: turns, tool calls, sign-ins, crawls and feedback get
recorded, and recording can never break what the user is doing."""

import asyncio
import json
import time
from contextlib import asynccontextmanager

from fastapi.testclient import TestClient

import agent
import analytics
import auth
import main
import news_crawler
import tracing


def _headers(client, username="alice"):
    token = client.post("/auth/register", json={"username": username, "password": "password123"}).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


def _capture(monkeypatch, name):
    saved = []

    async def fake(*args, **kwargs):
        saved.append((args, kwargs))

    monkeypatch.setattr(analytics, name, fake)
    return saved


# --------------------------------------------------------------------------- chat turns


def test_each_turn_is_recorded_with_tool_calls_tokens_and_trace(fake_db, monkeypatch):
    saved = _capture(monkeypatch, "record_turn")

    async def fake_agent_chat(message, thread_id=None, user_id=None, username=None):
        # What the logged tool wrappers and the usage callback do in a real turn.
        agent._record_tool_call("search_memories", {"query": "news"}, {"memories": []}, time.perf_counter(), "ok")
        agent._record_tool_call("get_latest_news", {"region": "Bihar"}, {"count": 5}, time.perf_counter(), "ok")
        agent._record_tool_call("get_latest_news", {"region": "Patna"}, "boom", time.perf_counter(), "error")
        agent._note_usage({"gpt-4o-mini-2024-07-18": {"input_tokens": 1200, "output_tokens": 300}}, "ab" * 16)
        return "Here is the news."

    monkeypatch.setattr(agent, "agent_chat", fake_agent_chat)
    client = TestClient(main.app)

    reply = client.post("/chat", json={"message": "Any news from Bihar?", "thread_id": "t-1"}, headers=_headers(client))

    [(_, row)] = saved
    assert reply.json()["turn_id"] == row["turn_id"]
    assert (row["thread_id"], row["user"].username, row["user_input"]) == ("t-1", "alice", "Any news from Bihar?")
    assert (row["agent_output"], row["status"], row["error"]) == ("Here is the news.", "ok", None)
    turn = row["turn"]
    assert turn.tools_used == ["search_memories", "get_latest_news"]  # once each, in order
    assert [c["status"] for c in turn.tool_calls] == ["ok", "ok", "error"]
    assert turn.tool_calls[1]["input"] == {"region": "Bihar"}
    assert turn.tool_calls[2]["error"] == "boom" and turn.tool_calls[2]["output_preview"] is None
    assert (turn.prompt_tokens, turn.completion_tokens, turn.trace_id) == (1200, 300, "ab" * 16)


def test_a_failed_turn_is_recorded_as_an_error(fake_db, monkeypatch):
    saved = _capture(monkeypatch, "record_turn")

    async def broken_agent_chat(message, thread_id=None, user_id=None, username=None):
        raise RuntimeError("model exploded")

    monkeypatch.setattr(agent, "agent_chat", broken_agent_chat)
    client = TestClient(main.app)

    reply = client.post("/chat", json={"message": "hi", "thread_id": "t-1"}, headers=_headers(client))

    assert reply.status_code == 500
    [(_, row)] = saved
    assert (row["status"], row["error"], row["agent_output"]) == ("error", "model exploded", None)


def test_turns_never_see_each_others_tool_calls():
    async def one_turn(name):
        with agent.record_turn() as turn:
            await asyncio.sleep(0)
            agent._record_tool_call(name, {}, {}, time.perf_counter(), "ok")
            await asyncio.sleep(0)
            return turn.tools_used

    async def both():
        return await asyncio.gather(one_turn("get_fare"), one_turn("get_stock_price"))

    assert asyncio.run(both()) == [["get_fare"], ["get_stock_price"]]
    agent._record_tool_call("get_fare", {}, {}, time.perf_counter(), "ok")  # outside a turn: not collected


def test_trace_id_comes_from_the_first_root_span_inside_the_block():
    with tracing.capture_trace_id() as trace:
        tracing.note_root_span(0xABC)
        tracing.note_root_span(0xDEF)
    tracing.note_root_span(0x123)  # outside: ignored
    assert trace == {"trace_id": "0" * 29 + "abc"}


def test_cost_uses_the_longest_matching_model_price(monkeypatch):
    monkeypatch.delenv("LLM_PRICE_PER_MILLION_TOKENS", raising=False)
    usage = {"input_tokens": 1_000_000, "output_tokens": 1_000_000}
    assert analytics.cost_usd({"gpt-4o-mini-2024-07-18": usage}) == 0.75  # not gpt-4o's price
    assert analytics.cost_usd({"gpt-4o-2024-08-06": usage}) == 12.5
    assert analytics.cost_usd({"some-new-model": usage}) is None  # unknown price: no guess
    assert analytics.cost_usd({}) is None
    monkeypatch.setenv("LLM_PRICE_PER_MILLION_TOKENS", "1,2")
    assert analytics.cost_usd({"some-new-model": usage}) == 3.0


# --------------------------------------------------------------------------- writing rows


class _RecordingPool:
    def __init__(self, fail=False):
        self.fail = fail
        self.statements = []

    @asynccontextmanager
    async def connection(self):
        yield self

    @asynccontextmanager
    async def transaction(self):
        yield

    async def execute(self, sql, params=()):
        if self.fail:
            raise OSError("database is down")
        self.statements.append((" ".join(sql.split()), params))


def test_record_turn_writes_the_turn_and_its_tool_calls(monkeypatch):
    pool = _RecordingPool()
    monkeypatch.setattr(analytics, "_pool", pool)
    with agent.record_turn() as turn:
        agent._record_tool_call("get_fare", {"train": "12345"}, {"fare": 500}, time.perf_counter(), "ok")
        agent._note_usage({"gpt-4o-mini": {"input_tokens": 1000, "output_tokens": 100}}, "f" * 32)
    user = auth.User(id="u_1", username="alice")

    asyncio.run(analytics.record_turn(
        turn_id="t1", thread_id="th", user=user, user_input="fare?", agent_output="₹500",
        status="ok", duration_ms=900, turn=turn,
    ))

    (turn_sql, turn_params), (tool_sql, tool_params) = pool.statements
    assert turn_sql.startswith("INSERT INTO chat_messages")
    assert turn_params[:10] == ("t1", "th", "u_1", "alice", "fare?", "₹500", ["get_fare"], "ok", None, 900)
    assert turn_params[10:] == ("gpt-4o-mini", 1000, 100, 0.00021, "f" * 32)
    assert tool_sql.startswith("INSERT INTO tool_calls")
    assert tool_params[1:6] == ("t1", "th", "u_1", "get_fare", json.dumps({"train": "12345"}))
    assert tool_params[6] == json.dumps({"fare": 500}) and tool_params[7] == "ok"


def test_recording_failures_are_swallowed(monkeypatch):
    user = auth.User(id="u_1", username="alice")
    row = dict(turn_id="t1", thread_id="th", user=user, user_input="hi", agent_output="Hello!", status="ok")
    for pool in (_RecordingPool(fail=True), None):  # database down; no database configured
        monkeypatch.setattr(analytics, "_pool", pool)
        asyncio.run(analytics.record_turn(**row))
        asyncio.run(analytics.record_auth_event("login_failed", username_tried="x"))
        asyncio.run(analytics.record_crawl("n1", [{"name": "A", "status": "Success"}]))


def test_crawl_rows_per_source_and_old_rows_are_pruned(monkeypatch):
    pool = _RecordingPool()
    monkeypatch.setattr(analytics, "_pool", pool)
    summary = [
        {"name": "The Hindu", "category": "india", "status": "Success", "extracted": 20, "saved": 4, "duration_ms": 900},
        {"name": "Live Hindustan Bihar", "category": "regional", "status": "No items", "extracted": 0, "saved": 0},
    ]
    asyncio.run(analytics.record_crawl("n1", summary))
    inserts, (prune_sql, _) = pool.statements[:-1], pool.statements[-1]
    assert [p[1:5] for _, p in inserts] == [("The Hindu", "india", "Success", 20), ("Live Hindustan Bihar", "regional", "No items", 0)]
    assert prune_sql.startswith("DELETE FROM news_crawl_runs")


def test_an_empty_page_is_reported_as_no_items(monkeypatch):
    monkeypatch.setattr(news_crawler, "fetch_url_with_rate_limit", lambda url: (b"<html><body></body></html>", "utf-8"))
    source = {"name": "Broken", "language": "hi", "category": "regional", "type": "html", "url": "https://example.com", "selector": "a.x"}
    items, stats = news_crawler.fetch_source(source)
    assert items == [] and stats["status"] == "No items" and stats["category"] == "regional"
    assert isinstance(stats["duration_ms"], int)


# --------------------------------------------------------------------------- sign-ins and feedback


def test_sign_ins_are_recorded_with_the_real_failure_reason(fake_db, monkeypatch):
    saved = _capture(monkeypatch, "record_auth_event")
    client = TestClient(main.app)

    _headers(client, "alice")
    client.post("/auth/register", json={"username": "alice", "password": "password123"})
    client.post("/auth/login", json={"username": "ALICE", "password": "password123"})
    wrong = client.post("/auth/login", json={"username": "alice", "password": "wrong-password"})
    client.post("/auth/login", json={"username": "nobody", "password": "password123"})

    events = [(args[0], kw["username_tried"], kw.get("reason")) for args, kw in saved]
    assert events == [
        ("register", "alice", None),
        ("register_failed", "alice", "That username is already taken."),
        ("login_ok", "alice", None),
        ("login_failed", "alice", "wrong password"),
        ("login_failed", "nobody", "unknown user"),
    ]
    # The user still can't tell which: same message either way.
    assert wrong.json()["detail"] == "Incorrect username or password."


def test_feedback_is_saved_for_the_signed_in_user(fake_db, monkeypatch):
    saved = _capture(monkeypatch, "save_feedback")
    client = TestClient(main.app)
    headers = _headers(client)
    turn_id = "0b7c8f0e-4a43-4e7e-9a3c-1f6f0f2b9d11"

    assert client.post("/feedback", json={"turn_id": turn_id, "rating": -1, "comment": "wrong city"}, headers=headers).status_code == 204
    me = client.get("/auth/me", headers=headers).json()
    assert saved == [((turn_id, me["id"], -1, "wrong city"), {})]

    assert client.post("/feedback", json={"turn_id": turn_id, "rating": 5}, headers=headers).status_code == 422
    assert client.post("/feedback", json={"turn_id": "not-a-turn", "rating": 1}, headers=headers).status_code == 422
    assert client.post("/feedback", json={"turn_id": turn_id, "rating": 1}).status_code == 401
