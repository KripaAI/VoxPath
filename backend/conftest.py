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

    `hide_existing_emails` makes the "is this email taken?" SELECT miss, to
    simulate two sign-ups racing past it.
    """

    def __init__(self):
        self.users: dict[str, dict] = {}  # email -> row
        self.threads: dict[str, str] = {}  # thread_id -> owner_id
        self.hide_existing_emails = False

    async def execute(self, sql, params: tuple = ()):
        s = " ".join(sql.split())
        if s.startswith("CREATE TABLE"):
            return _Cursor(None)
        if s.startswith("SELECT 1 FROM users WHERE email"):
            found = params[0] in self.users and not self.hide_existing_emails
            return _Cursor({"?column?": 1} if found else None)
        if s.startswith("INSERT INTO users"):
            user_id, email, password_hash = params
            if email in self.users:
                raise psycopg.errors.UniqueViolation("duplicate key value violates unique constraint")
            self.users[email] = {"id": user_id, "email": email, "password_hash": password_hash}
            return _Cursor(None)
        if s.startswith("SELECT id, password_hash FROM users WHERE email"):
            return _Cursor(self.users.get(params[0]))
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
