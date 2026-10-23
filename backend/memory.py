"""Long-term memory for the VoxPath agent.

Facts worth remembering across conversations live in the LangGraph store,
which is Postgres-backed with a pgvector index (see main.lifespan). That is a
different thing from the checkpointer: the checkpointer replays one thread's
raw messages, while this survives across every thread for a given user.

The store is reached through get_store() rather than being passed in, so these
stay plain functions whose signatures are exactly what the model sees. The cost
is that they only work inside a LangGraph run -- calling them over the /mcp HTTP
mount raises, which is why each one says so in its error.
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone

from langgraph.config import get_config, get_store

from logging_config import get_logger, redact

log = get_logger("voxpath.memory")





DEFAULT_USER_ID = "default"
MEMORY_NAMESPACE = "memories"









MIN_SCORE = float(os.getenv("MEMORY_MIN_SCORE", "0.12"))















DEDUPE_SCORE = float(os.getenv("MEMORY_DEDUPE_SCORE", "0.85"))




OVERFETCH = int(os.getenv("MEMORY_OVERFETCH", "3"))


def _require_store():
    store = get_store()
    if store is None:
        raise RuntimeError(
            "No memory store configured. Set DATABASE_URL and restart the API."
        )
    return store


def _user_id() -> str:
    """Current user id, from the run config when the caller supplies one."""
    try:
        config = get_config()
    except RuntimeError:
        return DEFAULT_USER_ID
    configurable = (config or {}).get("configurable") or {}
    return configurable.get("user_id") or DEFAULT_USER_ID


def _namespace() -> tuple[str, str]:
    return (MEMORY_NAMESPACE, _user_id())


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


async def save_memory(text: str) -> dict[str, object]:
    """Store one durable fact, replacing a restatement of the same fact."""
    fact = text.strip()
    if not fact:
        return {"status": "ignored", "reason": "empty memory"}

    store = _require_store()
    namespace = _namespace()

    for hit in await store.asearch(namespace, query=fact, limit=3):
        if hit.score is not None and hit.score >= DEDUPE_SCORE:
            previous = hit.value.get("text", "")
            await store.aput(
                namespace,
                hit.key,
                {"text": fact, "created_at": hit.value.get("created_at", _now()), "updated_at": _now()},
            )
            log.info("updated memory %s for %s: %r -> %r", hit.key, namespace[1], redact(previous, 60), redact(fact, 60))
            return {"status": "updated", "id": hit.key, "text": fact, "replaced": previous}

    key = uuid.uuid4().hex[:12]
    await store.aput(namespace, key, {"text": fact, "created_at": _now(), "updated_at": _now()})
    log.info("saved memory %s for %s: %r", key, namespace[1], redact(fact, 80))
    return {"status": "saved", "id": key, "text": fact}


async def update_memory(memory_id: str, text: str) -> dict[str, object]:
    """Replace the text of one memory, for a fact the user has corrected."""
    fact = text.strip()
    if not fact:
        return {"status": "ignored", "reason": "empty memory"}

    store = _require_store()
    namespace = _namespace()
    existing = await store.aget(namespace, memory_id)
    if existing is None:
        return {"status": "not_found", "id": memory_id}

    previous = existing.value.get("text", "")
    await store.aput(
        namespace,
        memory_id,
        {"text": fact, "created_at": existing.value.get("created_at", _now()), "updated_at": _now()},
    )
    log.info("updated memory %s for %s: %r -> %r", memory_id, namespace[1], redact(previous, 60), redact(fact, 60))
    return {"status": "updated", "id": memory_id, "text": fact, "replaced": previous}


async def delete_memory(memory_id: str) -> dict[str, object]:
    """Forget one memory. The vector row goes with it (ON DELETE CASCADE)."""
    store = _require_store()
    namespace = _namespace()
    existing = await store.aget(namespace, memory_id)
    if existing is None:
        return {"status": "not_found", "id": memory_id}

    await store.adelete(namespace, memory_id)
    log.info("deleted memory %s for %s", memory_id, namespace[1])
    return {"status": "deleted", "id": memory_id, "text": existing.value.get("text", "")}


async def list_memories(limit: int = 50) -> dict[str, object]:
    """Everything stored about this user, newest first."""
    store = _require_store()
    namespace = _namespace()
    limit = max(1, min(limit, 200))
    items = await store.asearch(namespace, limit=limit)
    memories = [
        {
            "id": item.key,
            "text": item.value.get("text", ""),
            "updated_at": item.value.get("updated_at", ""),
        }
        for item in items
    ]
    memories.sort(key=lambda m: m["updated_at"], reverse=True)
    return {"memories": memories, "count": len(memories)}


async def search_memories(query: str, limit: int = 3) -> dict[str, object]:
    """Recall previously saved facts most similar in meaning to the query."""
    store = _require_store()
    namespace = _namespace()
    limit = max(1, min(limit, 10))

    hits = await store.asearch(namespace, query=query, limit=limit * OVERFETCH)
    memories = [
        {"id": hit.key, "text": hit.value.get("text", ""), "score": round(hit.score, 3) if hit.score is not None else None}
        for hit in hits


        if hit.score is None or hit.score >= MIN_SCORE
    ][:limit]
    log.info(
        "memory search %r for %s: %d/%d above %.2f",
        redact(query, 60), namespace[1], len(memories), len(hits), MIN_SCORE,
    )
    return {"memories": memories, "count": len(memories)}
