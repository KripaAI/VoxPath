"""Tables for analysing VoxPath, one per kind of event, all queryable with SQL.

  chat_messages     one row per chat turn: question, answer, tools, status,
                    latency, tokens, cost, and the Phoenix trace id
  tool_calls        one row per tool call in a turn: input, result preview,
                    status, error, latency (joins chat_messages on turn_id)
  auth_events       one row per sign-up or sign-in attempt, failed ones included
  news_crawl_runs   one row per news source per background crawl
  message_feedback  one row per thumbs up/down a user gives an answer

The database answers "what, how much, how often"; Phoenix answers "why" for a
single turn, and chat_messages.trace_id leads from one to the other.

Recording is best-effort everywhere: writes run after the user's response has
been sent (or alongside the work), and a failure is logged and dropped. Nothing
here can delay, fail or change what a user gets.
"""

from __future__ import annotations

import asyncio
import json
import os
from typing import Any

from logging_config import get_logger

log = get_logger("voxpath.analytics")

_pool = None
_background: set[asyncio.Task] = set()

CRAWL_RUNS_KEEP_DAYS = int(os.getenv("CRAWL_RUNS_KEEP_DAYS", "30"))

# List prices in US dollars per million tokens (input, output). Matched by the
# longest prefix of the model name the API reports ("gpt-4o-mini-2024-07-18").
# Prices change: check them, or set LLM_PRICE_PER_MILLION_TOKENS="input,output".
MODEL_PRICES_PER_MILLION = {
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4o": (2.50, 10.00),
    "gpt-4.1-nano": (0.10, 0.40),
    "gpt-4.1-mini": (0.40, 1.60),
    "gpt-4.1": (2.00, 8.00),
}

_SCHEMA = (
    # chat_messages predates this module; created here for new databases and
    # extended in place on existing ones.
    """
    CREATE TABLE IF NOT EXISTS chat_messages (
        id           bigserial PRIMARY KEY,
        created_at   timestamptz NOT NULL DEFAULT now(),
        thread_id    text NOT NULL,
        user_id      text NOT NULL,
        username     text,
        user_input   text NOT NULL,
        agent_output text,
        tools_used   text[] NOT NULL DEFAULT '{}',
        status       text NOT NULL DEFAULT 'ok' CHECK (status IN ('ok', 'error')),
        error        text,
        duration_ms  integer
    )
    """,
    "ALTER TABLE chat_messages ADD COLUMN IF NOT EXISTS turn_id text",
    "ALTER TABLE chat_messages ADD COLUMN IF NOT EXISTS model text",
    "ALTER TABLE chat_messages ADD COLUMN IF NOT EXISTS prompt_tokens integer",
    "ALTER TABLE chat_messages ADD COLUMN IF NOT EXISTS completion_tokens integer",
    "ALTER TABLE chat_messages ADD COLUMN IF NOT EXISTS cost_usd numeric(12, 6)",
    "ALTER TABLE chat_messages ADD COLUMN IF NOT EXISTS trace_id text",
    "CREATE INDEX IF NOT EXISTS chat_messages_thread_idx ON chat_messages (thread_id, created_at)",
    "CREATE INDEX IF NOT EXISTS chat_messages_user_idx ON chat_messages (user_id, created_at)",
    "CREATE UNIQUE INDEX IF NOT EXISTS chat_messages_turn_idx ON chat_messages (turn_id) WHERE turn_id IS NOT NULL",
    """
    CREATE TABLE IF NOT EXISTS tool_calls (
        id             bigserial PRIMARY KEY,
        created_at     timestamptz NOT NULL DEFAULT now(),
        turn_id        text NOT NULL,
        thread_id      text NOT NULL,
        user_id        text NOT NULL,
        tool           text NOT NULL,
        input          jsonb,
        output_preview text,
        status         text NOT NULL CHECK (status IN ('ok', 'error')),
        error          text,
        duration_ms    integer
    )
    """,
    "CREATE INDEX IF NOT EXISTS tool_calls_turn_idx ON tool_calls (turn_id)",
    "CREATE INDEX IF NOT EXISTS tool_calls_tool_idx ON tool_calls (tool, created_at)",
    """
    CREATE TABLE IF NOT EXISTS auth_events (
        id             bigserial PRIMARY KEY,
        created_at     timestamptz NOT NULL DEFAULT now(),
        event          text NOT NULL CHECK (event IN ('register', 'register_failed', 'login_ok', 'login_failed')),
        username_tried text,
        user_id        text,
        reason         text,
        client_ip      text
    )
    """,
    "CREATE INDEX IF NOT EXISTS auth_events_time_idx ON auth_events (created_at)",
    "CREATE INDEX IF NOT EXISTS auth_events_username_idx ON auth_events (username_tried, created_at)",
    """
    CREATE TABLE IF NOT EXISTS news_crawl_runs (
        id            bigserial PRIMARY KEY,
        created_at    timestamptz NOT NULL DEFAULT now(),
        run_id        text NOT NULL,
        source        text NOT NULL,
        category      text,
        status        text NOT NULL,
        extracted     integer NOT NULL DEFAULT 0,
        saved         integer NOT NULL DEFAULT 0,
        used_fallback boolean NOT NULL DEFAULT false,
        duration_ms   integer
    )
    """,
    "CREATE INDEX IF NOT EXISTS news_crawl_runs_time_idx ON news_crawl_runs (created_at)",
    "CREATE INDEX IF NOT EXISTS news_crawl_runs_source_idx ON news_crawl_runs (source, created_at)",
    """
    CREATE TABLE IF NOT EXISTS message_feedback (
        id         bigserial PRIMARY KEY,
        created_at timestamptz NOT NULL DEFAULT now(),
        updated_at timestamptz NOT NULL DEFAULT now(),
        turn_id    text NOT NULL,
        user_id    text NOT NULL,
        rating     smallint NOT NULL CHECK (rating IN (-1, 1)),
        comment    text,
        UNIQUE (turn_id, user_id)
    )
    """,
)


async def setup(pool) -> None:
    """Create or upgrade the tables, and keep the pool."""
    global _pool
    async with pool.connection() as conn:
        # One statement per execute: the pool sends prepared statements, which
        # Postgres refuses to combine.
        for statement in _SCHEMA:
            await conn.execute(statement)
    _pool = pool
    log.info("analytics tables ready")


def spawn(coro) -> None:
    """Run a recording coroutine in the background without waiting for it.

    The task is kept referenced until it finishes, as asyncio only holds weak
    references to running tasks.
    """
    task = asyncio.get_running_loop().create_task(coro)
    _background.add(task)
    task.add_done_callback(_background.discard)


def cost_usd(usage_by_model: dict[str, dict] | None) -> float | None:
    """The turn's model cost in dollars, or None if any model's price is unknown."""
    if not usage_by_model:
        return None
    override = os.getenv("LLM_PRICE_PER_MILLION_TOKENS")
    total = 0.0
    for model, usage in usage_by_model.items():
        if override:
            prices = tuple(float(part) for part in override.split(","))
        else:
            known = [name for name in MODEL_PRICES_PER_MILLION if model.startswith(name)]
            if not known:
                return None
            prices = MODEL_PRICES_PER_MILLION[max(known, key=len)]
        total += usage.get("input_tokens", 0) * prices[0] / 1e6 + usage.get("output_tokens", 0) * prices[1] / 1e6
    return round(total, 6)


async def _write(what: str, statements: list[tuple[str, tuple]]) -> None:
    """Run the statements in one transaction; log and drop any failure."""
    if _pool is None or not statements:  # no database configured
        return
    try:
        async with _pool.connection() as conn:
            async with conn.transaction():
                for sql, params in statements:
                    await conn.execute(sql, params)
    except Exception as failure:
        log.warning("could not record %s: %s", what, failure)


async def record_turn(
    *,
    turn_id: str,
    thread_id: str,
    user: Any,
    user_input: str,
    agent_output: str | None,
    status: str,
    error: str | None = None,
    duration_ms: int | None = None,
    turn: Any = None,
) -> None:
    """Save one chat turn and its tool calls. `turn` is the agent's TurnRecord."""
    tool_calls = turn.tool_calls if turn else []
    statements = [(
        "INSERT INTO chat_messages (turn_id, thread_id, user_id, username, user_input, agent_output, "
        "tools_used, status, error, duration_ms, model, prompt_tokens, completion_tokens, cost_usd, trace_id) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (
            turn_id, thread_id, user.id, user.username, user_input, agent_output,
            turn.tools_used if turn else [], status, error, duration_ms,
            turn.model if turn else None,
            turn.prompt_tokens if turn else None,
            turn.completion_tokens if turn else None,
            cost_usd(turn.usage_by_model) if turn else None,
            turn.trace_id if turn else None,
        ),
    )]
    for call in tool_calls:
        statements.append((
            "INSERT INTO tool_calls (created_at, turn_id, thread_id, user_id, tool, input, output_preview, "
            "status, error, duration_ms) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                call["started_at"], turn_id, thread_id, user.id, call["tool"],
                json.dumps(call["input"], ensure_ascii=False), call["output_preview"],
                call["status"], call["error"], call["duration_ms"],
            ),
        ))
    await _write(f"chat turn {turn_id}", statements)


async def record_auth_event(
    event: str,
    *,
    username_tried: str | None = None,
    user_id: str | None = None,
    reason: str | None = None,
    client_ip: str | None = None,
) -> None:
    await _write(f"auth event {event}", [(
        "INSERT INTO auth_events (event, username_tried, user_id, reason, client_ip) VALUES (%s, %s, %s, %s, %s)",
        (event, username_tried, user_id, reason, client_ip),
    )])


async def record_crawl(run_id: str, summary: list[dict]) -> None:
    """Save one background crawl (a row per source) and drop rows past retention."""
    statements = [
        (
            "INSERT INTO news_crawl_runs (run_id, source, category, status, extracted, saved, used_fallback, "
            "duration_ms) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
            (
                run_id, source["name"], source.get("category"), source["status"], source.get("extracted", 0),
                source.get("saved", 0), bool(source.get("fallback")), source.get("duration_ms"),
            ),
        )
        for source in summary
    ]
    statements.append((
        "DELETE FROM news_crawl_runs WHERE created_at < now() - make_interval(days => %s)",
        (CRAWL_RUNS_KEEP_DAYS,),
    ))
    await _write(f"news crawl {run_id}", statements)


async def save_feedback(turn_id: str, user_id: str, rating: int, comment: str | None) -> None:
    """Set, change (rating 1 or -1) or clear (rating 0) a user's vote on an answer.

    Unlike the other writes this one is awaited and raises: the user asked for it
    and should hear if it failed.
    """
    if _pool is None:
        raise RuntimeError("no database configured")
    async with _pool.connection() as conn:
        if rating == 0:
            await conn.execute("DELETE FROM message_feedback WHERE turn_id = %s AND user_id = %s", (turn_id, user_id))
            return
        await conn.execute(
            "INSERT INTO message_feedback (turn_id, user_id, rating, comment) VALUES (%s, %s, %s, %s) "
            "ON CONFLICT (turn_id, user_id) DO UPDATE SET rating = EXCLUDED.rating, "
            "comment = EXCLUDED.comment, updated_at = now()",
            (turn_id, user_id, rating, comment),
        )
