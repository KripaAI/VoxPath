"""Connection-pool health checks tuned for a remote Neon database.

Neon closes connections that sit idle (and suspends the compute after a few
minutes), so a pooled connection can be dead when it is next handed out. The
blunt fix, pinging every connection on every checkout, costs a full round trip
each time: with the database in us-east-1 and the app in India that added ~8
extra trips and ~4.5 s to every chat message.

Here a connection is only pinged when it has been idle longer than
STALE_AFTER_SECONDS, which is when Neon may have dropped it. Connections in
active use are handed out with no extra round trip. If the ping fails, the pool
discards the connection and opens a fresh one.
"""

from __future__ import annotations

import os
import time
import weakref

from psycopg_pool import AsyncConnectionPool

STALE_AFTER_SECONDS = float(os.getenv("DB_STALE_AFTER_SECONDS", "60"))

_last_returned: "weakref.WeakKeyDictionary[object, float]" = weakref.WeakKeyDictionary()


async def mark_returned(conn) -> None:
    """Pool `reset` callback: remember when the connection was last in use."""
    _last_returned[conn] = time.monotonic()


async def check_if_idle(conn) -> None:
    """Pool `check` callback: ping only connections that may have gone stale."""
    last = _last_returned.get(conn)
    if last is not None and time.monotonic() - last < STALE_AFTER_SECONDS:
        return
    await AsyncConnectionPool.check_connection(conn)
