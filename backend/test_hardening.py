"""Review fixes on /chat: message and thread-id limits, and no raw error text to the browser."""

from fastapi.testclient import TestClient

import agent
import main


def _signed_in(monkeypatch, chat=None):
    async def default_chat(message, thread_id=None, user_id=None, username=None):
        return f"echo: {message}"

    monkeypatch.setattr(agent, "agent_chat", chat or default_chat)
    client = TestClient(main.app)
    token = client.post("/auth/register", json={"username": "alice", "password": "password123"}).json()["access_token"]
    return client, {"Authorization": f"Bearer {token}"}


def test_long_messages_are_refused_before_the_model(fake_db, monkeypatch):
    called = []

    async def chat(message, **kwargs):
        called.append(message)
        return "x"

    client, headers = _signed_in(monkeypatch, chat)
    ok = client.post("/chat", json={"message": "a" * main.MAX_MESSAGE_CHARS, "thread_id": "t-1"}, headers=headers)
    too_long = client.post("/chat", json={"message": "a" * (main.MAX_MESSAGE_CHARS + 1), "thread_id": "t-2"}, headers=headers)
    assert ok.status_code == 200
    assert too_long.status_code == 400 and "too long" in too_long.json()["error"]
    assert len(called) == 1  # the oversized message never reached the agent (or the bill)


def test_thread_ids_must_look_like_thread_ids(fake_db, monkeypatch):
    client, headers = _signed_in(monkeypatch)
    for good in ("2f1c9a7e-3b4d-4e8f-9a1b-2c3d4e5f6a7b", "t_u_1234abcd", "t-1731050000000-ab12cd34"):
        assert client.post("/chat", json={"message": "hi", "thread_id": good}, headers=headers).status_code == 200
    for bad in ("x" * 101, "../../etc", "t 1", "<script>", ""):
        response = client.post("/chat", json={"message": "hi", "thread_id": bad}, headers=headers)
        assert response.status_code == 400, bad
        assert response.json()["error"] == "Invalid conversation id."


def test_agent_errors_reach_the_log_not_the_browser(fake_db, monkeypatch):
    async def broken(message, **kwargs):
        raise RuntimeError("connect to db-prod-7.internal:5432 failed for user admin")

    client, headers = _signed_in(monkeypatch, broken)
    response = client.post("/chat", json={"message": "hi", "thread_id": "t-1"}, headers=headers)
    assert response.status_code == 500
    body = response.json()
    assert "db-prod-7" not in body["error"] and "admin" not in body["error"]
    assert body["error"].startswith("Something went wrong") and body["turn_id"] in body["error"]
