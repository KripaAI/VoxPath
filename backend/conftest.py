"""Shared pytest fixtures for the VoxPath backend tests."""

import os

# Tests must never send traces to the developer's Phoenix, whatever backend/.env
# says. Set before any test imports main (whose load_dotenv won't override it).
os.environ["PHOENIX_TRACING"] = "false"

from contextlib import asynccontextmanager
from datetime import datetime, timezone

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

    async def fetchall(self):
        return self._row or []


class FakeDB:
    """Just enough of the users/threads tables for auth.py, kept in memory.

    `hide_existing_users` makes the "is this username/email taken?" SELECT miss,
    to simulate two sign-ups racing past it.
    """

    def __init__(self):
        self.users: list[dict] = []
        self.threads: dict[str, str] = {}  # thread_id -> owner_id
        self.thread_rows: dict[str, dict] = {}  # thread_id -> title, times, deleted_at
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
            # Upsert that only succeeds for the thread's owner, and never for a
            # thread they deleted (see auth.claim_thread).
            thread_id, user_id, *rest = params
            title = rest[0] if rest else None
            owner = self.threads.setdefault(thread_id, user_id)
            now = datetime.now(timezone.utc)
            row = self.thread_rows.setdefault(
                thread_id, {"thread_id": thread_id, "title": None, "created_at": now, "deleted_at": None}
            )
            if owner != user_id or row["deleted_at"]:
                return _Cursor(None)
            row["title"] = row["title"] or title
            row["last_used"] = now
            return _Cursor({"owner_id": owner})
        if s.startswith("SELECT thread_id, title, created_at, last_used FROM threads"):
            if "thread_id = %s" in s:
                thread_id, user_id = params
                return _Cursor(self._visible_thread(thread_id, user_id))
            user_id, limit, offset = params
            rows = [r for t, r in self.thread_rows.items() if self._visible_thread(t, user_id)]
            rows = sorted(rows, key=lambda r: r["last_used"], reverse=True)
            return _Cursor(rows[offset:offset + limit])
        if s.startswith("UPDATE threads SET deleted_at = now()"):
            thread_id, user_id = params
            row = self._visible_thread(thread_id, user_id)
            if row:
                row["deleted_at"] = datetime.now(timezone.utc)
            return _Cursor({"thread_id": thread_id} if row else None)
        raise AssertionError(f"FakeDB got unexpected SQL: {s}")

    def _visible_thread(self, thread_id, user_id):
        row = self.thread_rows.get(thread_id)
        if row and self.threads.get(thread_id) == user_id and not row["deleted_at"]:
            return row
        return None


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
