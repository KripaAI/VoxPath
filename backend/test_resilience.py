"""When the database is unreachable, users get a clear 503 and a safe-to-retry flag."""

import psycopg
from fastapi.testclient import TestClient
from psycopg_pool import PoolTimeout

import auth
import main


def _token(client):
    return {"Authorization": f"Bearer {client.post('/auth/register', json={'username': 'alice', 'password': 'password123'}).json()['access_token']}"}


def test_db_down_before_chat_runs_is_retryable(fake_db, monkeypatch):
    client = TestClient(main.app)
    headers = _token(client)

    async def unreachable(thread_id, user):
        raise PoolTimeout("couldn't get a connection after 15.00 sec")

    monkeypatch.setattr(auth, "claim_thread", unreachable)
    response = client.post("/chat", json={"message": "hi"}, headers=headers)
    assert response.status_code == 503
    assert response.json()["retryable"] is True
    assert "can't reach its database" in response.json()["error"]
    assert response.headers["Retry-After"] == "5"


def test_db_down_during_chat_is_not_auto_retryable(fake_db, monkeypatch):
    import agent

    client = TestClient(main.app)
    headers = _token(client)

    async def save_failed(message, thread_id=None, user_id=None, username=None):
        raise psycopg.OperationalError("SSL connection has been closed unexpectedly")

    monkeypatch.setattr(agent, "agent_chat", save_failed)
    response = client.post("/chat", json={"message": "hi"}, headers=headers)
    assert response.status_code == 503
    assert response.json()["retryable"] is False  # the agent may already have run


def test_wrapped_db_error_is_recognised():
    try:
        try:
            raise PoolTimeout("timeout")
        except PoolTimeout as inner:
            raise RuntimeError("graph failed") from inner
    except RuntimeError as outer:
        assert main.is_db_unavailable(outer)
    assert not main.is_db_unavailable(ValueError("unrelated"))


def test_sign_in_with_db_down_gets_clear_503(fake_db, monkeypatch):
    async def unreachable(sql, params=()):
        raise PoolTimeout("couldn't get a connection after 15.00 sec")

    monkeypatch.setattr(fake_db, "execute", unreachable)
    response = TestClient(main.app).post("/auth/login", json={"username": "alice", "password": "password123"})
    assert response.status_code == 503
    assert "can't reach its database" in response.json()["detail"]


def test_other_errors_are_still_500(fake_db, monkeypatch):
    import agent

    client = TestClient(main.app)
    headers = _token(client)

    async def bug(message, thread_id=None, user_id=None, username=None):
        raise ValueError("something else broke")

    monkeypatch.setattr(agent, "agent_chat", bug)
    assert client.post("/chat", json={"message": "hi"}, headers=headers).status_code == 500
