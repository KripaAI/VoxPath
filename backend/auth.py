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

import hashlib
import hmac
import json
import os
import re
import secrets
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
        # Upgrade: conversations get a title (from their first real message), and
        # deleting one only stamps deleted_at -- the chat is hidden, never erased.
        await conn.execute("ALTER TABLE threads ADD COLUMN IF NOT EXISTS title text")
        await conn.execute("ALTER TABLE threads ADD COLUMN IF NOT EXISTS deleted_at timestamptz")
        # One live password-reset code per user (see request_password_reset).
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS password_resets (
                user_id    text PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
                code_hash  text NOT NULL,
                expires_at timestamptz NOT NULL,
                attempts   integer NOT NULL DEFAULT 0
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


class LoginFailed(HTTPException):
    """A refused sign-in. The user always sees the same message, so it doesn't
    reveal which usernames exist; `reason` keeps the real cause for analytics."""

    def __init__(self, reason: str):
        super().__init__(status.HTTP_401_UNAUTHORIZED, "Incorrect username or password.")
        self.reason = reason


async def login(creds: LoginRequest) -> TokenResponse:
    identifier = creds.username.strip().lower()  # a username or an email
    async with _pool.connection() as conn:
        row = await (
            await conn.execute(
                "SELECT id, username, email, password_hash FROM users WHERE username = %s OR email = %s LIMIT 1",
                (identifier, identifier),
            )
        ).fetchone()


    if not row or not verify_password(creds.password, row["password_hash"]):
        log.warning("failed login for %s", redact(identifier))
        raise LoginFailed("unknown user" if not row else "wrong password")
    # Accounts from before usernames existed fall back to their email until one is set.
    user = User(id=row["id"], username=row["username"] or row["email"], email=row["email"])
    log.info("login %s", redact(user.username))
    return TokenResponse(access_token=create_token(user), user=user)


# --------------------------------------------------------------------------- password reset
#
# There is no mail service, so the one-time code goes to the backend's console and
# log (deliver_reset_code), which only whoever runs the server can read. On a real
# deployment, replace deliver_reset_code with an email to the account's address.
# The code is random (about 40 bits), lives RESET_CODE_MINUTES, allows
# RESET_MAX_ATTEMPTS guesses and works once; only its SHA-256 is stored.

RESET_CODE_MINUTES = int(os.getenv("RESET_CODE_MINUTES", "15"))
RESET_MAX_ATTEMPTS = 5
_CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"  # no 0/O or 1/I/L to misread


class ForgotPasswordRequest(BaseModel):
    username: str = Field(min_length=1, max_length=254)  # username or email


class ResetPasswordRequest(BaseModel):
    username: str = Field(min_length=1, max_length=254)
    code: str = Field(min_length=1, max_length=20)
    password: str = Field(min_length=MIN_PASSWORD_CHARS)


class ResetFailed(HTTPException):
    """Wrong, expired or used-up code, or no such account: one message for all, so
    the reply never confirms which accounts exist; `reason` is for analytics."""

    def __init__(self, reason: str):
        super().__init__(status.HTTP_400_BAD_REQUEST, "That reset code is wrong or has expired. Request a new one.")
        self.reason = reason


def _code_hash(code: str) -> str:
    cleaned = re.sub(r"[^A-Z0-9]", "", code.upper())
    return hashlib.sha256(cleaned.encode("ascii", "ignore")).hexdigest()


def _new_code() -> str:
    raw = "".join(secrets.choice(_CODE_ALPHABET) for _ in range(8))
    return f"{raw[:4]}-{raw[4:]}"


def deliver_reset_code(username: str, code: str) -> None:
    """Show the code to whoever runs the backend (its console window and log file)."""
    log.warning(
        "\n%s\n  PASSWORD RESET CODE for %s:  %s   (valid %d minutes, one use)\n%s",
        "=" * 64, username, code, RESET_CODE_MINUTES, "=" * 64,
    )


async def _find_account(conn, identifier: str):
    return await (
        await conn.execute(
            "SELECT id, username, email, password_hash FROM users WHERE username = %s OR email = %s LIMIT 1",
            (identifier, identifier),
        )
    ).fetchone()


async def request_password_reset(identifier: str) -> str | None:
    """Issue a new code for the account (replacing any earlier one). Returns the
    user id, or None for an unknown account; callers answer both the same way."""
    identifier = identifier.strip().lower()
    async with _pool.connection() as conn:
        row = await _find_account(conn, identifier)
        if not row:
            log.warning("password reset asked for unknown account %s", redact(identifier))
            return None
        code = _new_code()
        await conn.execute(
            "INSERT INTO password_resets (user_id, code_hash, expires_at, attempts) "
            "VALUES (%s, %s, now() + make_interval(mins => %s), 0) "
            "ON CONFLICT (user_id) DO UPDATE SET code_hash = EXCLUDED.code_hash, "
            "expires_at = EXCLUDED.expires_at, attempts = 0",
            (row["id"], _code_hash(code), RESET_CODE_MINUTES),
        )
    deliver_reset_code(row["username"] or row["email"], code)
    return row["id"]


async def reset_password(req: ResetPasswordRequest) -> TokenResponse:
    """Check the code, set the new password, and sign the user in."""
    identifier = req.username.strip().lower()
    new_hash = hash_password(req.password)  # checks the length before touching the code
    async with _pool.connection() as conn:
        row = await _find_account(conn, identifier)
        if not row:
            raise ResetFailed("unknown user")
        reset = await (
            await conn.execute(
                "SELECT code_hash, expires_at < now() AS expired, attempts FROM password_resets WHERE user_id = %s",
                (row["id"],),
            )
        ).fetchone()
        if not reset:
            raise ResetFailed("no code requested")
        if reset["expired"] or reset["attempts"] >= RESET_MAX_ATTEMPTS:
            await conn.execute("DELETE FROM password_resets WHERE user_id = %s", (row["id"],))
            raise ResetFailed("code expired" if reset["expired"] else "too many attempts")
        if not hmac.compare_digest(reset["code_hash"], _code_hash(req.code)):
            await conn.execute("UPDATE password_resets SET attempts = attempts + 1 WHERE user_id = %s", (row["id"],))
            raise ResetFailed("wrong code")
        await conn.execute("UPDATE users SET password_hash = %s WHERE id = %s", (new_hash, row["id"]))
        await conn.execute("DELETE FROM password_resets WHERE user_id = %s", (row["id"],))
    user = User(id=row["id"], username=row["username"] or row["email"], email=row["email"])
    log.info("password reset for %s", redact(user.username))
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


def require_token(app, on_user=None):
    """Wrap a mounted ASGI app so every HTTP request needs a valid bearer token.

    Mounted sub-apps sit outside FastAPI's dependency system, so `current_user`
    cannot guard them; this does the same check at the ASGI layer. `on_user`, if
    given, is called with the verified user and may refuse the request by raising
    an HTTPException (e.g. a 429 from a rate limiter).
    """

    async def refuse(send, status_code: int, detail: str, headers: dict | None = None):
        await send({
            "type": "http.response.start",
            "status": status_code,
            "headers": [(b"content-type", b"application/json")]
            + [(k.lower().encode("latin-1"), str(v).encode("latin-1")) for k, v in (headers or {}).items()],
        })
        await send({"type": "http.response.body", "body": json.dumps({"detail": detail}).encode("utf-8")})

    async def guarded(scope, receive, send):
        if scope["type"] != "http":
            await app(scope, receive, send)
            return
        header = dict(scope.get("headers") or []).get(b"authorization", b"").decode("latin-1")
        scheme, _, token = header.partition(" ")
        try:
            if scheme.lower() != "bearer" or not token:
                raise jwt.InvalidTokenError("missing bearer token")
            user = decode_token(token)
        except jwt.InvalidTokenError:
            await refuse(send, 401, "Not authenticated.", {"WWW-Authenticate": "Bearer"})
            return
        if on_user is not None:
            try:
                on_user(user)
            except HTTPException as refused:
                await refuse(send, refused.status_code, str(refused.detail), refused.headers)
                return
        await app(scope, receive, send)

    return guarded


# A first message made only of these words ("hi", "hello there", "good morning")
# doesn't name the chat; the next real message does.
_GREETINGS = {
    "hi", "hii", "hiii", "hello", "helo", "hlo", "hey", "heya", "yo", "namaste", "hola",
    "good", "morning", "afternoon", "evening", "there", "ok", "okay", "thanks", "thank", "you",
}


def make_title(message: str) -> str | None:
    """The chat's title: the start of the message, or None for a bare greeting."""
    text = " ".join(message.split())
    words = re.findall(r"[a-z]+", text.lower())
    if not words or all(word in _GREETINGS for word in words):
        return None
    return text[:60]


async def claim_thread(thread_id: str, user: User, title: str | None = None) -> None:
    """Record ownership on first use; refuse someone else's thread.

    Also names the thread: `title` is saved while the thread has none yet, so
    the first real message names it and later messages leave it alone. A thread
    the user deleted is refused like a stranger's: it stays hidden.

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
                "INSERT INTO threads (thread_id, owner_id, title) VALUES (%s, %s, %s) "
                "ON CONFLICT (thread_id) DO UPDATE SET last_used = now(), "
                "title = COALESCE(threads.title, EXCLUDED.title) "
                "WHERE threads.owner_id = EXCLUDED.owner_id AND threads.deleted_at IS NULL "
                "RETURNING owner_id",
                (thread_id, user.id, title),
            )
        ).fetchone()
        if row is None:
            log.warning("user %s tried to use a thread owned by another user", user.id)
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Conversation not found.")
