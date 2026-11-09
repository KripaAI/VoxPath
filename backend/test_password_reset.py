"""Forgot password: a one-time code, shown only to whoever runs the backend."""

from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

import auth
import main


def _client_with_codes(monkeypatch):
    """A client, plus the list the reset codes get 'delivered' to."""
    delivered = []
    monkeypatch.setattr(auth, "deliver_reset_code", lambda username, code: delivered.append((username, code)))
    client = TestClient(main.app)
    client.post("/auth/register", json={"username": "krish", "password": "old-password-1", "email": "k@example.com"})
    return client, delivered


def _login(client, who, password):
    return client.post("/auth/login", json={"username": who, "password": password}).status_code


def test_reset_with_the_code_sets_a_new_password_and_signs_in(fake_db, monkeypatch):
    client, delivered = _client_with_codes(monkeypatch)

    asked = client.post("/auth/forgot-password", json={"username": "K@Example.com"})  # email works too
    assert asked.status_code == 202 and "backend" in asked.json()["message"]
    [(username, code)] = delivered
    assert username == "krish" and len(code) == 9 and code[4] == "-"
    assert all(user["password_hash"] != code for user in fake_db.users)  # never stored as is
    assert code.replace("-", "") not in str(fake_db.resets)

    reset = client.post("/auth/reset-password", json={"username": "krish", "code": code.lower(), "password": "new-password-2"})
    assert reset.status_code == 200 and reset.json()["user"]["username"] == "krish"
    assert client.get("/auth/me", headers={"Authorization": f"Bearer {reset.json()['access_token']}"}).status_code == 200

    assert _login(client, "krish", "new-password-2") == 200
    assert _login(client, "krish", "old-password-1") == 401
    # One use only.
    again = client.post("/auth/reset-password", json={"username": "krish", "code": code, "password": "third-password-3"})
    assert again.status_code == 400


def test_unknown_accounts_get_the_same_answer_and_no_code(fake_db, monkeypatch):
    client, delivered = _client_with_codes(monkeypatch)
    known = client.post("/auth/forgot-password", json={"username": "krish"})
    unknown = client.post("/auth/forgot-password", json={"username": "nobody"})
    assert (known.status_code, known.json()) == (unknown.status_code, unknown.json())
    assert [name for name, _ in delivered] == ["krish"]

    refused = client.post("/auth/reset-password", json={"username": "nobody", "code": "ABCD-EFGH", "password": "whatever-123"})
    assert refused.status_code == 400 and refused.json()["detail"].startswith("That reset code is wrong")


def test_wrong_guesses_and_expiry_kill_the_code(fake_db, monkeypatch):
    client, delivered = _client_with_codes(monkeypatch)
    client.post("/auth/forgot-password", json={"username": "krish"})
    code = delivered[-1][1]
    body = lambda c: {"username": "krish", "code": c, "password": "new-password-2"}

    for _ in range(auth.RESET_MAX_ATTEMPTS):
        assert client.post("/auth/reset-password", json=body("WRNG-CODE")).status_code == 400
    # Out of guesses: even the right code no longer works.
    assert client.post("/auth/reset-password", json=body(code)).status_code == 400
    assert _login(client, "krish", "old-password-1") == 200

    # A fresh code works, but not once it has expired.
    client.post("/auth/forgot-password", json={"username": "krish"})
    code = delivered[-1][1]
    for row in fake_db.resets.values():
        row["expires_at"] = datetime.now(timezone.utc) - timedelta(seconds=1)
    assert client.post("/auth/reset-password", json=body(code)).status_code == 400


def test_asking_again_replaces_the_earlier_code(fake_db, monkeypatch):
    client, delivered = _client_with_codes(monkeypatch)
    client.post("/auth/forgot-password", json={"username": "krish"})
    client.post("/auth/forgot-password", json={"username": "krish"})
    first, second = delivered[0][1], delivered[1][1]
    body = lambda c: {"username": "krish", "code": c, "password": "new-password-2"}
    assert client.post("/auth/reset-password", json=body(first)).status_code == 400
    assert client.post("/auth/reset-password", json=body(second)).status_code == 200


def test_new_password_must_meet_the_rules(fake_db, monkeypatch):
    client, delivered = _client_with_codes(monkeypatch)
    client.post("/auth/forgot-password", json={"username": "krish"})
    short = client.post("/auth/reset-password", json={"username": "krish", "code": delivered[-1][1], "password": "short"})
    assert short.status_code == 422
