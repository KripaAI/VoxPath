"""Past conversations: list, open and hide the signed-in user's chats.

Every query is limited to the caller's own threads, and a chat that isn't
theirs, doesn't exist, or was deleted all answer 404 alike. Deleting only
stamps deleted_at: the chat disappears from the app but stays in the database.
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from pydantic import BaseModel

import agent
import auth

router = APIRouter(prefix="/conversations", tags=["conversations"])


class ConversationSummary(BaseModel):
    id: str
    title: str
    created_at: datetime
    last_used: datetime


class ChatMessage(BaseModel):
    role: str  # "user" or "assistant"
    content: str


class Conversation(ConversationSummary):
    messages: list[ChatMessage]


def _summary(row: dict) -> ConversationSummary:
    return ConversationSummary(
        id=row["thread_id"],
        title=row["title"] or "Untitled chat",
        created_at=row["created_at"],
        last_used=row["last_used"],
    )


@router.get("", response_model=list[ConversationSummary])
async def list_conversations(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    user: auth.User = Depends(auth.current_user),
):
    """The user's chats, most recently used first; `offset` skips that many (for paging)."""
    async with auth._pool.connection() as conn:
        rows = await (
            await conn.execute(
                "SELECT thread_id, title, created_at, last_used FROM threads "
                "WHERE owner_id = %s AND deleted_at IS NULL "
                "ORDER BY last_used DESC, thread_id LIMIT %s OFFSET %s",
                (user.id, limit, offset),
            )
        ).fetchall()
    return [_summary(row) for row in rows]


@router.get("/{thread_id}", response_model=Conversation)
async def open_conversation(thread_id: str, user: auth.User = Depends(auth.current_user)):
    """One chat with its messages, as the user saw them."""
    async with auth._pool.connection() as conn:
        row = await (
            await conn.execute(
                "SELECT thread_id, title, created_at, last_used FROM threads "
                "WHERE thread_id = %s AND owner_id = %s AND deleted_at IS NULL",
                (thread_id, user.id),
            )
        ).fetchone()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Conversation not found.")
    messages = await agent.get_messages(thread_id)
    return Conversation(**_summary(row).model_dump(), messages=messages)


@router.delete("/{thread_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_conversation(thread_id: str, user: auth.User = Depends(auth.current_user)):
    """Hide a chat. Its messages stay in the database; it just leaves the app."""
    async with auth._pool.connection() as conn:
        row = await (
            await conn.execute(
                "UPDATE threads SET deleted_at = now() "
                "WHERE thread_id = %s AND owner_id = %s AND deleted_at IS NULL "
                "RETURNING thread_id",
                (thread_id, user.id),
            )
        ).fetchone()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Conversation not found.")
    return Response(status_code=status.HTTP_204_NO_CONTENT)
