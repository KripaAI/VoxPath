"""In-memory request rate limiting for the VoxPath API.

Sliding-window counters keyed by client IP (sign-in, sign-up) or user id (chat),
so one caller cannot brute-force passwords or run up the OpenAI/RapidAPI bill.

Limits come from the environment as "COUNT/SECONDS" (e.g. "30/60"); "0" turns a
limit off. State lives in this process only: it resets on restart and is not
shared between workers, which is enough for a single-instance deployment. Behind
a reverse proxy every request shares the proxy's IP, so configure the proxy to
pass the real client address (uvicorn --proxy-headers) before relying on the
per-IP limits.
"""

from __future__ import annotations

import os
import threading
import time
from collections import deque

from fastapi import Depends, HTTPException, Request, status

import auth
from logging_config import get_logger

log = get_logger("voxpath.ratelimit")


def _parse(spec: str) -> tuple[int, float] | None:
    spec = spec.strip()
    if spec in ("", "0"):
        return None
    count, _, seconds = spec.partition("/")
    return int(count), float(seconds or 60)


class RateLimiter:
    def __init__(self, name: str, env_var: str, default: str):
        self.name = name
        self.limit = _parse(os.getenv(env_var, default))
        self._hits: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def check(self, key: str) -> None:
        """Record one request for `key`, or raise 429 if it is over the limit."""
        if self.limit is None:
            return
        max_count, window = self.limit
        now = time.monotonic()
        with self._lock:
            hits = self._hits.setdefault(key, deque())
            while hits and now - hits[0] >= window:
                hits.popleft()
            if len(hits) >= max_count:
                retry_after = max(1, int(window - (now - hits[0])) + 1)
                log.warning("rate limit %s hit by %s", self.name, key)
                raise HTTPException(
                    status.HTTP_429_TOO_MANY_REQUESTS,
                    f"Too many requests. Please wait {retry_after} seconds and try again.",
                    headers={"Retry-After": str(retry_after)},
                )
            hits.append(now)
            if len(self._hits) > 10_000:
                # Drop callers with no recent requests so memory stays bounded.
                for stale in [k for k, v in self._hits.items() if not v or now - v[-1] >= window]:
                    del self._hits[stale]

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()


login_limiter = RateLimiter("login", "LOGIN_RATE_LIMIT", "10/60")
register_limiter = RateLimiter("register", "REGISTER_RATE_LIMIT", "10/3600")
chat_limiter = RateLimiter("chat", "CHAT_RATE_LIMIT", "30/60")


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def limit_login(request: Request) -> None:
    login_limiter.check(_client_ip(request))


def limit_register(request: Request) -> None:
    register_limiter.check(_client_ip(request))


def limit_chat(user: auth.User = Depends(auth.current_user)) -> None:
    chat_limiter.check(user.id)
