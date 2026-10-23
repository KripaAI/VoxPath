"""Shared pytest fixtures for the VoxPath backend tests."""

import os

# Tests must never send traces to the developer's Phoenix, whatever backend/.env
# says. Set before any test imports main (whose load_dotenv won't override it).
os.environ["PHOENIX_TRACING"] = "false"

from contextlib import asynccontextmanager

import psycopg
import pytest

import auth
import ratelimit


@pytest.fixture(autouse=True)
def _reset_rate_limits():
    """Each test starts with empty rate-limit counters."""
    for limiter in (ratelimit.login_limiter, ratelimit.register_limiter, ratelimit.chat_limiter):
        limiter.reset()
    yield


class _Cursor:
    def __init__(self, row):
        self._row = row

    async def fetchone(self):
        return self._row


class FakeDB:
    """Just enough of the users/threads tables for auth.py, kept in memory.

    `hide_existing_users` makes the "is this username/email taken?" SELECT miss,
    to simulate two sign-ups racing past it.
    """

    def __init__(self):
        self.users: list[dict] = []
        self.threads: dict[str, str] = {}  # thread_id -> owner_id
        self.hide_existing_users = False

    def _find(self, username, email):
        for row in self.users:
            if row["username"] == username or (email is not None and row["email"] == email):
                return row
        return None

    async def execute(self, sql, params: tuple = ()):
        s = " ".join(sql.split())
        if s.startswith(("CREATE TABLE", "ALTER TABLE")):
            return _Cursor(None)
        if s.startswith("SELECT username, email FROM users"):
            row = None if self.hide_existing_users else self._find(*params)
            return _Cursor({"username": row["username"], "email": row["email"]} if row else None)
        if s.startswith("INSERT INTO users"):
            user_id, username, email, password_hash = params
            if self._find(username, email):
                raise psycopg.errors.UniqueViolation("duplicate key value violates unique constraint")
            self.users.append({"id": user_id, "username": username, "email": email, "password_hash": password_hash})
            return _Cursor(None)
        if s.startswith("SELECT id, username, email, password_hash FROM users"):
            username_or_email = params[0]
            return _Cursor(self._find(username_or_email, username_or_email))
        if s.startswith("INSERT INTO threads") and "RETURNING owner_id" in s:
            # Upsert that only succeeds for the thread's owner (see auth.claim_thread).
            thread_id, user_id = params
            owner = self.threads.setdefault(thread_id, user_id)
            return _Cursor({"owner_id": owner} if owner == user_id else None)
        raise AssertionError(f"FakeDB got unexpected SQL: {s}")


class _FakePool:
    def __init__(self, db):
        self._db = db

    @asynccontextmanager
    async def connection(self):
        yield self._db


@pytest.fixture
def fake_db(monkeypatch):
    """Point auth at an in-memory database and a known JWT secret."""
    db = FakeDB()
    monkeypatch.setattr(auth, "_pool", _FakePool(db))
    monkeypatch.setattr(auth, "JWT_SECRET", "test-secret-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx")
    return db
