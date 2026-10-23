"""Username and password authentication for VoxPath.

Accounts have a unique, case-insensitive username and an optional email; users
sign in with either. The username travels in the session token so logs and
traces can name the user without a database lookup.

Identity is the foundation the memory system needs: until a request carries a
verified user, every conversation and every stored memory shares one bucket.
So the only thing that may set a user id is a token this module signed -- never
a request field, a tool argument, or anything the model produced.

Deliberately local: users live in the same Postgres the agent already uses, so
there is no third-party account to configure. Swapping in a hosted identity
provider later means replacing `current_user` and leaving the rest alone.
"""

from __future__ import annotations

import os
import re
import uuid
from datetime import datetime, timedelta, timezone

import bcrypt
import jwt
import psycopg
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import AliasChoices, BaseModel, EmailStr, Field, field_validator

from logging_config import get_logger, redact

log = get_logger("voxpath.auth")



JWT_SECRET = os.getenv("JWT_SECRET", "")
JWT_ALGORITHM = "HS256"
TOKEN_TTL_HOURS = int(os.getenv("TOKEN_TTL_HOURS", "168"))



MAX_PASSWORD_BYTES = 72
MIN_PASSWORD_CHARS = 8

_pool = None
_bearer = HTTPBearer(auto_error=False)


USERNAME_RULE = (
    "Username must be 3-30 characters: letters, numbers, dot, dash or underscore, "
    "starting with a letter or number."
)
_USERNAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{2,29}$")


def normalize_username(value: str) -> str:
    """Usernames are case-insensitive: stored and compared in lowercase."""
    username = value.strip().lower()
    if not _USERNAME_PATTERN.match(username):
        raise ValueError(USERNAME_RULE)
    return username


class User(BaseModel):
    id: str
    username: str
    email: str | None = None


class RegisterRequest(BaseModel):
    username: str
    password: str = Field(min_length=MIN_PASSWORD_CHARS)
    email: EmailStr | None = None

    @field_validator("username")
    @classmethod
    def _valid_username(cls, value: str) -> str:
        return normalize_username(value)

    @field_validator("email", mode="before")
    @classmethod
    def _blank_email_is_none(cls, value):
        return None if isinstance(value, str) and not value.strip() else value


class LoginRequest(BaseModel):
    # Username or email. "email" is still accepted as the field name so older
    # clients that post {"email": ..., "password": ...} keep working.
    username: str = Field(min_length=1, validation_alias=AliasChoices("username", "email"))
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: User


async def setup(pool) -> None:
    """Create the users table and keep the pool for later queries."""
    global _pool
    _pool = pool
    async with pool.connection() as conn:
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                id            text PRIMARY KEY,
                username      text,
                email         text UNIQUE,
                password_hash text NOT NULL,
                created_at    timestamptz NOT NULL DEFAULT now()
            )
            """
        )
        # Upgrade databases created before usernames existed (safe to re-run):
        # add the username column, make email optional, keep usernames unique.
        # One statement per execute: the pool sends prepared statements, which
        # Postgres refuses to combine.
        for statement in (
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS username text",
            "ALTER TABLE users ALTER COLUMN email DROP NOT NULL",
            "CREATE UNIQUE INDEX IF NOT EXISTS users_username_key ON users (username)",
        ):
            await conn.execute(statement)


        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS threads (
                thread_id  text PRIMARY KEY,
                owner_id   text NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                created_at timestamptz NOT NULL DEFAULT now(),
                last_used  timestamptz NOT NULL DEFAULT now()
            )
            """
        )
    if not JWT_SECRET:
        raise RuntimeError(
            "JWT_SECRET is not set. Generate one with `python -c \"import "
            "secrets; print(secrets.token_urlsafe(48))\"` and add it to backend/.env"
        )

    log.info("auth ready (token ttl %dh)", TOKEN_TTL_HOURS)


def hash_password(password: str) -> str:
    encoded = password.encode("utf-8")
    if len(encoded) > MAX_PASSWORD_BYTES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Password must be at most {MAX_PASSWORD_BYTES} bytes.",
        )
    return bcrypt.hashpw(encoded, bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    encoded = password.encode("utf-8")[:MAX_PASSWORD_BYTES]
    try:
        return bcrypt.checkpw(encoded, password_hash.encode("utf-8"))
    except ValueError:
        return False


def create_token(user: User) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "sub": user.id,
        "username": user.username,
        "email": user.email,
        "iat": now,
        "exp": now + timedelta(hours=TOKEN_TTL_HOURS),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


async def register(creds: RegisterRequest) -> TokenResponse:
    user_id = f"u_{uuid.uuid4().hex[:16]}"
    username = creds.username  # already normalized by RegisterRequest
    email = creds.email.lower().strip() if creds.email else None
    password_hash = hash_password(creds.password)
    async with _pool.connection() as conn:
        taken = await (
            await conn.execute(
                "SELECT username, email FROM users WHERE username = %s OR (email IS NOT NULL AND email = %s)",
                (username, email),
            )
        ).fetchone()
        if taken:
            what = "username is already taken" if taken["username"] == username else "email is already registered"
            raise HTTPException(status.HTTP_409_CONFLICT, f"That {what}.")
        try:
            await conn.execute(
                "INSERT INTO users (id, username, email, password_hash) VALUES (%s, %s, %s, %s)",
                (user_id, username, email, password_hash),
            )
        except psycopg.errors.UniqueViolation:
            # Two sign-ups for the same username or email raced past the SELECT above.
            raise HTTPException(status.HTTP_409_CONFLICT, "That username or email is already registered.")
    user = User(id=user_id, username=username, email=email)
    log.info("registered %s", redact(username))
    return TokenResponse(access_token=create_token(user), user=user)


async def login(creds: LoginRequest) -> TokenResponse:
    identifier = creds.username.strip().lower()  # a username or an email
    async with _pool.connection() as conn:
        row = await (
            await conn.execute(
                "SELECT id, username, email, password_hash FROM users WHERE username = %s OR email = %s LIMIT 1",
                (identifier, identifier),
            )
        ).fetchone()


    invalid = HTTPException(status.HTTP_401_UNAUTHORIZED, "Incorrect username or password.")
    if not row or not verify_password(creds.password, row["password_hash"]):
        log.warning("failed login for %s", redact(identifier))
        raise invalid
    # Accounts from before usernames existed fall back to their email until one is set.
    user = User(id=row["id"], username=row["username"] or row["email"], email=row["email"])
    log.info("login %s", redact(user.username))
    return TokenResponse(access_token=create_token(user), user=user)


async def current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> User:
    """The signed-in user, or 401. The only trusted source of a user id."""
    unauthorized = HTTPException(
        status.HTTP_401_UNAUTHORIZED,
        "Not authenticated.",
        headers={"WWW-Authenticate": "Bearer"},
    )
    if credentials is None:
        raise unauthorized
    try:
        return decode_token(credentials.credentials)
    except jwt.ExpiredSignatureError:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "Session expired. Please sign in again.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    except jwt.InvalidTokenError:
        raise unauthorized


def decode_token(token: str) -> User:
    """Verify a token this module signed. Raises jwt.InvalidTokenError otherwise."""
    if not JWT_SECRET:

        raise jwt.InvalidTokenError("JWT_SECRET is not set")
    payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
    user_id, username = payload.get("sub"), payload.get("username")
    if not user_id or not username:
        # Includes tokens issued before usernames existed: the user signs in once more.
        raise jwt.InvalidTokenError("token is missing sub or username")
    return User(id=user_id, username=username, email=payload.get("email"))


def require_token(app):
    """Wrap a mounted ASGI app so every HTTP request needs a valid bearer token.

    Mounted sub-apps sit outside FastAPI's dependency system, so `current_user`
    cannot guard them; this does the same check at the ASGI layer.
    """

    async def guarded(scope, receive, send):
        if scope["type"] != "http":
            await app(scope, receive, send)
            return
        header = dict(scope.get("headers") or []).get(b"authorization", b"").decode("latin-1")
        scheme, _, token = header.partition(" ")
        try:
            if scheme.lower() != "bearer" or not token:
                raise jwt.InvalidTokenError("missing bearer token")
            decode_token(token)
        except jwt.InvalidTokenError:
            await send({
                "type": "http.response.start",
                "status": 401,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"www-authenticate", b"Bearer"),
                ],
            })
            await send({"type": "http.response.body", "body": b'{"detail":"Not authenticated."}'})
            return
        await app(scope, receive, send)

    return guarded


async def claim_thread(thread_id: str, user: User) -> None:
    """Record ownership on first use; refuse someone else's thread.

    Returns 404 rather than 403 for a thread owned by another user: a 403 would
    confirm the thread exists, which is itself a small leak.
    """
    async with _pool.connection() as conn:
        # One round trip: claim a new thread, or touch last_used on our own. When the
        # thread belongs to someone else the WHERE fails and no row comes back. Being
        # a single statement, two users racing to claim the same new thread can't
        # both win (a separate SELECT-then-INSERT allowed that).
        row = await (
            await conn.execute(
                "INSERT INTO threads (thread_id, owner_id) VALUES (%s, %s) "
                "ON CONFLICT (thread_id) DO UPDATE SET last_used = now() "
                "WHERE threads.owner_id = EXCLUDED.owner_id "
                "RETURNING owner_id",
                (thread_id, user.id),
            )
        ).fetchone()
        if row is None:
            log.warning("user %s tried to use a thread owned by another user", user.id)
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Conversation not found.")
