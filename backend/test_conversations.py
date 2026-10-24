"""Past conversations: titles, listing, opening, and hiding (never erasing) chats."""

from fastapi.testclient import TestClient

import agent
import auth
import main


def _headers(client, username):
    token = client.post("/auth/register", json={"username": username, "password": "password123"}).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


def _client(monkeypatch):
    async def fake_agent_chat(message, thread_id=None, user_id=None, username=None):
        return f"echo: {message}"

    async def fake_get_messages(thread_id):
        return [{"role": "user", "content": "Train from Patna to Delhi"}, {"role": "assistant", "content": "Here."}]

    monkeypatch.setattr(agent, "agent_chat", fake_agent_chat)
    monkeypatch.setattr(agent, "get_messages", fake_get_messages)
    return TestClient(main.app)


def test_make_title_skips_greetings():
    assert auth.make_title("HI") is None
    assert auth.make_title("hello there!") is None
    assert auth.make_title("Train from  Patna\nto Delhi") == "Train from Patna to Delhi"
    assert auth.make_title("x" * 80) == "x" * 60


def test_list_open_and_hide_conversations(fake_db, monkeypatch):
    client = _client(monkeypatch)
    alice = _headers(client, "alice")

    # A greeting doesn't name the chat; the first real message does, and sticks.
    for message in ("hi", "Train from Patna to Delhi", "what about tomorrow"):
        client.post("/chat", json={"message": message, "thread_id": "t-1"}, headers=alice)
    client.post("/chat", json={"message": "hello", "thread_id": "t-2"}, headers=alice)

    listed = client.get("/conversations", headers=alice).json()
    assert [(c["id"], c["title"]) for c in listed] == [("t-2", "Untitled chat"), ("t-1", "Train from Patna to Delhi")]

    opened = client.get("/conversations/t-1", headers=alice)
    assert opened.status_code == 200
    assert opened.json()["messages"][0] == {"role": "user", "content": "Train from Patna to Delhi"}

    # Deleting hides the chat everywhere in the app, but the row is kept.
    assert client.delete("/conversations/t-1", headers=alice).status_code == 204
    assert [c["id"] for c in client.get("/conversations", headers=alice).json()] == ["t-2"]
    assert client.get("/conversations/t-1", headers=alice).status_code == 404
    assert client.delete("/conversations/t-1", headers=alice).status_code == 404
    assert client.post("/chat", json={"message": "again", "thread_id": "t-1"}, headers=alice).status_code == 404
    assert fake_db.thread_rows["t-1"]["deleted_at"] is not None


def test_conversations_page_with_limit_and_offset(fake_db, monkeypatch):
    client = _client(monkeypatch)
    alice = _headers(client, "alice")
    for n in range(5):
        client.post("/chat", json={"message": f"Question number {n}", "thread_id": f"t-{n}"}, headers=alice)

    def page(offset):
        response = client.get(f"/conversations?limit=2&offset={offset}", headers=alice)
        return [c["id"] for c in response.json()]

    pages = [page(0), page(2), page(4), page(6)]
    assert [len(p) for p in pages] == [2, 2, 1, 0]
    assert sorted(sum(pages, [])) == [f"t-{n}" for n in range(5)]  # every chat once, no repeats
    assert client.get("/conversations?offset=-1", headers=alice).status_code == 422


def test_other_users_conversations_are_invisible(fake_db, monkeypatch):
    client = _client(monkeypatch)
    alice, bob = _headers(client, "alice"), _headers(client, "bob")
    client.post("/chat", json={"message": "Train from Patna to Delhi", "thread_id": "t-1"}, headers=alice)

    assert client.get("/conversations", headers=bob).json() == []
    assert client.get("/conversations/t-1", headers=bob).status_code == 404
    assert client.delete("/conversations/t-1", headers=bob).status_code == 404
    assert client.get("/conversations", headers=alice).json()[0]["id"] == "t-1"


def test_conversations_need_sign_in(fake_db, monkeypatch):
    client = _client(monkeypatch)
    assert client.get("/conversations").status_code == 401
    assert client.delete("/conversations/t-1").status_code == 401
