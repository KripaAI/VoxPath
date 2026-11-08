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

import contextlib
import contextvars
import os
import re
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


# --------------------------------------------------------------------------- "don't remember me"
# A user can ask VoxPath not to save anything about them for the rest of a conversation.
# The model was told this in the prompt and still saved facts one turn later (eval case
# sess-003, 2 of 2 runs), so the rule is enforced here in code instead (book Ch 22: if a
# required behaviour is still skipped despite clear instructions, put it in the workflow).
#
# agent.agent_chat opens a write policy for each run; the pre-model hook, which sees the
# whole conversation, sets it from the user's messages (the latest request wins); and
# save_memory / update_memory refuse while it is on. Reading and forgetting stay allowed.
# Because it is re-derived from the conversation on every model call, it lasts as long as
# the conversation does, across restarts, with no extra storage.

_BLOCK_PATTERNS = [
    # an instruction to the assistant: at the start of a sentence, or after please / and / so
    r"(?:^|[.!?,;]\s*|\bplease\s+|\band\s+|\bso\s+)(?:do not|don't|dont|never|stop)\s+"
    r"(?:save|saving|store|storing|record|recording|keep|memori[sz]e|remember|remembering)\b",
    r"\b(?:no|without)\s+(?:saving|memory|memories)\b",
    r"\bnothing\s+(?:to|in)\s+(?:your\s+)?memory\b",
    r"\boff the record\b",
    # Hindi / Hinglish imperative with मत / mat ("don't" in a request)
    r"याद\s*मत|मत\s*याद|सेव\s*मत|मत\s*सेव|save\s+mat\b|mat\s+save\b|yaad\s+mat\b|mat\s+yaad\b|"
    r"save\s+(?:na|nahi|nahin)\s+kar|yaad\s+(?:na|nahi|nahin)\s+rakh",
]
_ALLOW_PATTERNS = [
    r"\byou can (?:now\s+)?(?:save|remember|store)\b",
    r"\b(?:start|resume)\s+(?:saving|remembering|storing)\b",
    r"\b(?:remember|save|store)\s+(?:things|stuff)?\s*again\b",
    # an explicit request to remember a particular fact is consent for that fact
    r"(?:^|[.!?,;]\s*|\bplease\s+|\balso\s+|\band\s+)(?:remember|save|store|note)\s+(?:that|this|my|me|it)\b",
]
_BLOCK = re.compile("|".join(_BLOCK_PATTERNS), re.IGNORECASE)
_ALLOW = re.compile("|".join(_ALLOW_PATTERNS), re.IGNORECASE)


def memory_directive(text: str) -> str | None:
    """'block' if the message asks not to be remembered, 'allow' if it asks to be
    remembered (again), None otherwise. A refusal wins over a request in one message
    ("don't save this")."""
    text = text or ""
    if _BLOCK.search(text):
        return "block"
    if _ALLOW.search(text):
        return "allow"
    return None


_write_policy: contextvars.ContextVar[dict | None] = contextvars.ContextVar("memory_write_policy", default=None)


@contextlib.contextmanager
def write_policy():
    """Scope one agent run. Yields a mutable holder, so the hook and the tools (which run
    in copies of this context) all see the same decision."""
    holder = {"blocked": False}
    token = _write_policy.set(holder)
    try:
        yield holder
    finally:
        _write_policy.reset(token)


def apply_directives(user_messages: list[str]) -> None:
    """Set the current run's policy from the user's messages, oldest first."""
    holder = _write_policy.get()
    if holder is None:
        return
    for text in user_messages:
        directive = memory_directive(text)
        if directive:
            holder["blocked"] = directive == "block"


def writes_blocked() -> bool:
    holder = _write_policy.get()
    return bool(holder and holder.get("blocked"))


def _refused(fact: str) -> dict[str, object]:
    log.info("memory write refused: the user asked not to be remembered in this conversation")
    return {
        "status": "not_saved",
        "reason": "The user asked not to have anything saved in this conversation. Nothing was "
                  "saved. Do not try again, and do not say you saved or noted it.",
        "text": fact,
    }


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
    if writes_blocked():
        return _refused(fact)

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
    if writes_blocked():
        return _refused(fact)

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
