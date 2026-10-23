"""Tests for auth flow, rate limits, memory, railway, news storage and stock-list resilience."""

import asyncio
import json
import tempfile
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from langgraph.store.memory import InMemoryStore

import logging_config
import main
import memory
import news_crawler
import news_storage
import railway
import ratelimit
import stock_resolver


# --------------------------------------------------------------------------- auth + chat


def _register(client, email="a@example.com", password="password123"):
    return client.post("/auth/register", json={"email": email, "password": password})


def test_register_login_me_and_chat_flow(fake_db, monkeypatch):
    import agent

    async def fake_agent_chat(message, thread_id=None, user_id=None):
        return f"echo: {message}"

    monkeypatch.setattr(agent, "agent_chat", fake_agent_chat)
    client = TestClient(main.app)

    registered = _register(client)
    assert registered.status_code == 200
    assert registered.json()["user"]["email"] == "a@example.com"

    login = client.post("/auth/login", json={"email": "A@Example.com", "password": "password123"})
    assert login.status_code == 200
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

    me = client.get("/auth/me", headers=headers)
    assert me.status_code == 200 and me.json()["email"] == "a@example.com"

    chat = client.post("/chat", json={"message": "hi", "thread_id": "t-1"}, headers=headers)
    assert chat.status_code == 200
    assert chat.json() == {"text": "echo: hi", "audio": None}
    assert fake_db.threads["t-1"] == me.json()["id"]


def test_other_users_thread_is_not_found(fake_db, monkeypatch):
    import agent

    async def fake_agent_chat(message, thread_id=None, user_id=None):
        return "ok"

    monkeypatch.setattr(agent, "agent_chat", fake_agent_chat)
    client = TestClient(main.app)
    alice = {"Authorization": f"Bearer {_register(client, 'alice@example.com').json()['access_token']}"}
    bob = {"Authorization": f"Bearer {_register(client, 'bob@example.com').json()['access_token']}"}

    assert client.post("/chat", json={"message": "hi", "thread_id": "shared"}, headers=alice).status_code == 200
    assert client.post("/chat", json={"message": "hi", "thread_id": "shared"}, headers=bob).status_code == 404


def test_wrong_password_and_duplicate_email(fake_db):
    client = TestClient(main.app)
    assert _register(client).status_code == 200
    assert client.post("/auth/login", json={"email": "a@example.com", "password": "wrong-pass"}).status_code == 401
    assert _register(client).status_code == 409


def test_racing_duplicate_registration_returns_409_not_500(fake_db):
    client = TestClient(main.app)
    assert _register(client).status_code == 200
    fake_db.hide_existing_emails = True  # the SELECT check misses, as in a race
    response = _register(client)
    assert response.status_code == 409
    assert response.json()["detail"] == "That email is already registered."


def test_registration_can_be_closed(fake_db, monkeypatch):
    monkeypatch.setenv("REGISTRATION_OPEN", "false")
    response = _register(TestClient(main.app))
    assert response.status_code == 403


# --------------------------------------------------------------------------- rate limits


def test_login_rate_limit(fake_db, monkeypatch):
    monkeypatch.setattr(ratelimit.login_limiter, "limit", (2, 60))
    client = TestClient(main.app)
    bad = {"email": "nobody@example.com", "password": "password123"}
    assert client.post("/auth/login", json=bad).status_code == 401
    assert client.post("/auth/login", json=bad).status_code == 401
    limited = client.post("/auth/login", json=bad)
    assert limited.status_code == 429
    assert int(limited.headers["Retry-After"]) >= 1


def test_chat_rate_limit_is_per_user(fake_db, monkeypatch):
    import agent

    async def fake_agent_chat(message, thread_id=None, user_id=None):
        return "ok"

    monkeypatch.setattr(agent, "agent_chat", fake_agent_chat)
    monkeypatch.setattr(ratelimit.chat_limiter, "limit", (1, 60))
    client = TestClient(main.app)
    alice = {"Authorization": f"Bearer {_register(client, 'alice@example.com').json()['access_token']}"}
    bob = {"Authorization": f"Bearer {_register(client, 'bob@example.com').json()['access_token']}"}

    assert client.post("/chat", json={"message": "hi"}, headers=alice).status_code == 200
    assert client.post("/chat", json={"message": "hi"}, headers=alice).status_code == 429
    assert client.post("/chat", json={"message": "hi"}, headers=bob).status_code == 200


def test_rate_limiter_window_and_disable(monkeypatch):
    limiter = ratelimit.RateLimiter("t", "UNSET_TEST_LIMIT", "2/0.2")
    limiter.check("k")
    limiter.check("k")
    with pytest.raises(Exception) as exc:
        limiter.check("k")
    assert exc.value.status_code == 429
    time.sleep(0.25)
    limiter.check("k")  # window has passed

    monkeypatch.setenv("UNSET_TEST_LIMIT", "0")
    off = ratelimit.RateLimiter("off", "UNSET_TEST_LIMIT", "1/60")
    for _ in range(5):
        off.check("k")


# --------------------------------------------------------------------------- MCP surface


def test_capabilities_resource_matches_registered_tools():
    import mcp_server

    tools = {t.name for t in asyncio.run(mcp_server.mcp.list_tools())}
    assert set(mcp_server.capabilities()["tools"]) == tools
    for retired in ("route_chat", "general_chat", "extract_market_tickers", "market_report", "handle_chat"):
        assert retired not in tools


# --------------------------------------------------------------------------- privacy switch


def test_redact_follows_log_user_content(monkeypatch):
    monkeypatch.delenv("LOG_USER_CONTENT", raising=False)
    assert logging_config.redact("hello world", 5) == "hello"
    monkeypatch.setenv("LOG_USER_CONTENT", "false")
    assert logging_config.redact("hello world", 5) == "<redacted 11 chars>"
    assert "hello" not in logging_config.redact({"q": "hello"})


# --------------------------------------------------------------------------- long-term memory


@pytest.fixture
def memory_store(monkeypatch):
    store = InMemoryStore()
    monkeypatch.setattr(memory, "get_store", lambda: store)
    monkeypatch.setattr(memory, "get_config", lambda: {"configurable": {"user_id": "u1"}})
    return store


def test_memory_save_list_update_delete(memory_store):
    saved = asyncio.run(memory.save_memory("  I live in Patna  "))
    assert saved["status"] == "saved" and saved["text"] == "I live in Patna"

    listed = asyncio.run(memory.list_memories())
    assert listed["count"] == 1 and listed["memories"][0]["text"] == "I live in Patna"

    updated = asyncio.run(memory.update_memory(saved["id"], "I live in Lucknow"))
    assert updated["status"] == "updated" and updated["replaced"] == "I live in Patna"

    found = asyncio.run(memory.search_memories("where do I live"))
    assert found["memories"][0]["text"] == "I live in Lucknow"

    assert asyncio.run(memory.delete_memory(saved["id"]))["status"] == "deleted"
    assert asyncio.run(memory.list_memories())["count"] == 0


def test_memory_edge_cases(memory_store, monkeypatch):
    assert asyncio.run(memory.save_memory("   "))["status"] == "ignored"
    assert asyncio.run(memory.update_memory("missing", "x"))["status"] == "not_found"
    assert asyncio.run(memory.delete_memory("missing"))["status"] == "not_found"

    # Memories are per user.
    asyncio.run(memory.save_memory("likes tea"))
    monkeypatch.setattr(memory, "get_config", lambda: {"configurable": {"user_id": "u2"}})
    assert asyncio.run(memory.list_memories())["count"] == 0

    monkeypatch.setattr(memory, "get_store", lambda: None)
    with pytest.raises(RuntimeError):
        asyncio.run(memory.list_memories())


# --------------------------------------------------------------------------- railway


class _FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


def _fake_get(routes):
    calls = []

    def get(url, headers=None, params=None, timeout=None):
        calls.append((url.rsplit("/", 1)[-1], params))
        return routes[url.rsplit("/", 1)[-1]]

    get.calls = calls
    return get


def test_pnr_status_validation_and_parsing(monkeypatch):
    monkeypatch.setenv("RAPIDAPI_KEY", "k")
    assert "error" in railway.get_pnr_status("")
    assert railway.get_pnr_status("12345")["error"] == "PNR must be exactly 10 digits"

    payload = {"data": {
        "trainName": "RAJDHANI", "trainNumber": "12424", "doj": "10-10-2026", "from": "NDLS", "to": "DBRT",
        "passengers": [{"bookingStatus": "CNF", "currentStatus": "CNF"}],
    }}
    monkeypatch.setattr(railway.requests, "get", _fake_get({"pnrStatus": _FakeResponse(payload)}))
    result = railway.get_pnr_status(" 1234567890 ")
    assert result["train_info"] == {"name": "RAJDHANI", "number": "12424"}
    assert result["journey_details"]["date_of_journey"] == "10-10-2026"
    assert result["passengers"] == [{"number": 1, "booking_status": "CNF", "current_status": "CNF"}]


def test_railway_reports_missing_api_key(monkeypatch):
    monkeypatch.delenv("RAPIDAPI_KEY", raising=False)
    assert "RAPIDAPI_KEY" in railway.get_pnr_status("1234567890")["error"]


def test_train_schedule_and_fare(monkeypatch):
    monkeypatch.setenv("RAPIDAPI_KEY", "k")
    schedule = {"data": [
        {"station_name": "New Delhi", "station_code": "NDLS", "std_min": 1000, "stop": True},
        {"station_name": "Somewhere", "station_code": "SMW", "std_min": 1100, "stop": False},
    ]}
    availability = {"data": [
        {"trainNumber": "11111", "trainName": "OTHER"},
        {"trainNumber": "12424", "trainName": "RAJDHANI", "from": {"name": "New Delhi"}, "to": {"name": "Patna"},
         "classAvailability": [{"class": "3A", "fare": 2500, "displayStatus": "AVL 10"}]},
    ]}
    get = _fake_get({"trainSchedule": _FakeResponse(schedule), "trainAvailability": _FakeResponse(availability)})
    monkeypatch.setattr(railway.requests, "get", get)

    sched = railway.get_train_schedule("12424")
    assert sched["total_stations"] == 2 and sched["total_stops"] == 1
    assert sched["major_stops"][0]["departure_time"] == "16:40"
    assert railway.get_train_schedule("12")["error"] == "Train number must be 4-5 digits"

    fare = railway.get_fare("12424", "ndls", "pnbe", "2026/10/10")
    assert fare["train_name"] == "RAJDHANI"
    assert fare["fares_by_class"] == [{"class": "3A", "fare": "₹2500", "availability": "AVL 10"}]
    assert get.calls[-1][1] == {"source": "NDLS", "destination": "PNBE", "date": "10-10-2026"}


def test_normalize_date_formats():
    assert railway.normalize_date("10-10-2026") == "10-10-2026"
    assert railway.normalize_date("10/10/2026") == "10-10-2026"
    assert railway.normalize_date("2026-10-10") == "10-10-2026"
    assert railway.normalize_date("tomorrow") is None


# --------------------------------------------------------------------------- news storage


def test_concurrent_crawls_do_not_save_duplicates(monkeypatch):
    # Own temp dir rather than pytest's tmp_path, whose shared base folder can be
    # unwritable on Windows.
    with tempfile.TemporaryDirectory() as tmp:
        _run_concurrent_crawls(Path(tmp), monkeypatch)


def _run_concurrent_crawls(tmp_path, monkeypatch):
    monkeypatch.setattr(news_storage, "DATA_DIR", tmp_path)
    monkeypatch.setattr(news_storage, "SEEN_HASHES_FILE", tmp_path / "seen_hashes.json")
    sources = [{"name": "A", "url": "u", "type": "rss", "language": "en"}]
    headlines = [
        {"headline": f"Completely distinct headline number {i} about topic {i * 7}", "link": "l",
         "source": "A", "language": "en", "timestamp": "t"}
        for i in range(5)
    ]
    start = threading.Barrier(2)

    def slow_fetch(source, limit=None):
        start.wait()  # both crawls reach the save step together
        return [dict(h) for h in headlines], {"name": source["name"]}

    monkeypatch.setattr(news_crawler, "_resolve_sources", lambda q: sources)
    monkeypatch.setattr(news_crawler, "fetch_source", slow_fetch)

    results = []
    threads = [threading.Thread(target=lambda: results.append(news_crawler.crawl_news())) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sorted(r["saved"] for r in results) == [0, 5]
    saved_lines = [json.loads(l) for f in tmp_path.glob("headlines_*.jsonl") for l in f.read_text("utf-8").splitlines()]
    assert len(saved_lines) == 5
    assert len(news_storage.load_seen_hashes()) == 5


# --------------------------------------------------------------------------- stock list resilience


def test_stock_list_failure_backs_off_and_keeps_last_good_list(monkeypatch):
    calls = []

    def failing_get(*args, **kwargs):
        calls.append(1)
        raise ConnectionError("NSE blocked")

    monkeypatch.setattr(stock_resolver.requests, "get", failing_get)
    monkeypatch.setattr(stock_resolver, "_rows", [("TCS", "TATA CONSULTANCY SERVICES LIMITED")])
    monkeypatch.setattr(stock_resolver, "_fetched_at", 0.0)  # stale, so a refresh is due
    monkeypatch.setattr(stock_resolver, "_last_attempt", 0.0)

    assert stock_resolver._load() == [("TCS", "TATA CONSULTANCY SERVICES LIMITED")]
    assert stock_resolver._load() == [("TCS", "TATA CONSULTANCY SERVICES LIMITED")]
    assert len(calls) == 1  # second call did not retry inside the back-off window

    monkeypatch.setattr(stock_resolver, "_last_attempt", time.time() - stock_resolver.RETRY_SECONDS - 1)
    stock_resolver._load()
    assert len(calls) == 2  # retried once the window passed
