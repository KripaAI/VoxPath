"""Copy past conversations from LangGraph's checkpoints into chat_messages.

One row per exchange: the user's message, the agent's final reply, and the
tools it called in between. Safe to run more than once: exchanges already in
chat_messages (from an earlier run, or logged live by /chat) are skipped.

Run from the backend folder:
    .venv\\Scripts\\python backfill_chat_messages.py --dry-run   (show what would be copied)
    .venv\\Scripts\\python backfill_chat_messages.py             (copy it)
"""

import asyncio
import os
import sys

from dotenv import load_dotenv
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
import psycopg
from psycopg.rows import dict_row

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    sys.stdout.reconfigure(encoding="utf-8")


def split_into_exchanges(messages):
    """Group a conversation into (user message, final reply, tools used, index of last message)."""
    exchanges = []
    current = None
    for index, message in enumerate(messages):
        if isinstance(message, HumanMessage):
            if current:
                exchanges.append(current)
            current = {"user_input": str(message.content), "agent_output": None, "tools_used": [], "end": index}
        elif current is not None and isinstance(message, AIMessage):
            for call in message.tool_calls or []:
                if call["name"] not in current["tools_used"]:
                    current["tools_used"].append(call["name"])
            if message.content and not message.tool_calls:
                current["agent_output"] = str(message.content)
            current["end"] = index
    if current:
        exchanges.append(current)
    return exchanges


async def main(dry_run: bool) -> None:
    load_dotenv(".env")
    url = os.environ["DATABASE_URL"]
    async with AsyncPostgresSaver.from_conn_string(url) as saver:
        conn = await psycopg.AsyncConnection.connect(url, autocommit=True, row_factory=dict_row)
        threads = await (await conn.execute(
            "SELECT t.thread_id, t.owner_id, u.username FROM threads t "
            "LEFT JOIN users u ON u.id = t.owner_id ORDER BY t.created_at"
        )).fetchall()
        total = 0
        for thread in threads:
            config = {"configurable": {"thread_id": thread["thread_id"]}}
            # Every saved checkpoint, oldest first: used to find when each exchange finished.
            history = [c async for c in saver.alist(config)][::-1]
            if not history:
                continue
            messages = history[-1].checkpoint["channel_values"].get("messages", [])
            print(f"\nConversation {thread['thread_id']} ({thread['username']}): {len(messages)} messages")
            for exchange in split_into_exchanges(messages):
                # Time of the first checkpoint that already contains the exchange's last message.
                finished = next(
                    (c.checkpoint["ts"] for c in history
                     if len(c.checkpoint["channel_values"].get("messages", [])) > exchange["end"]),
                    history[-1].checkpoint["ts"],
                )
                status = "ok" if exchange["agent_output"] else "error"
                print(f"  {finished[:19]}  {exchange['user_input'][:50]!r} -> "
                      f"{(exchange['agent_output'] or '(no reply saved)')[:45]!r}  tools={exchange['tools_used']}")
                if dry_run:
                    total += 1
                    continue
                # Already there, whether copied by an earlier run or logged live by
                # /chat (which stamps the reply time, a few seconds off the checkpoint's).
                exists = await (await conn.execute(
                    "SELECT 1 FROM chat_messages WHERE thread_id = %s AND user_input = %s "
                    "AND agent_output IS NOT DISTINCT FROM %s "
                    "AND created_at BETWEEN %s::timestamptz - interval '10 minutes' "
                    "AND %s::timestamptz + interval '10 minutes'",
                    (thread["thread_id"], exchange["user_input"], exchange["agent_output"], finished, finished),
                )).fetchone()
                if exists:
                    continue
                await conn.execute(
                    "INSERT INTO chat_messages (created_at, thread_id, user_id, username, user_input, "
                    "agent_output, tools_used, status) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                    (finished, thread["thread_id"], thread["owner_id"], thread["username"],
                     exchange["user_input"], exchange["agent_output"], exchange["tools_used"], status),
                )
                total += 1
        await conn.close()
    print(f"\n{'Would copy' if dry_run else 'Copied'} {total} exchanges.")


if __name__ == "__main__":
    asyncio.run(main(dry_run="--dry-run" in sys.argv))
