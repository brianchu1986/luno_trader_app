from __future__ import annotations

import asyncio
import time
from typing import Any

from agents.mcp import MCPServerStdio, MCPServerStdioParams
from mcp.types import CallToolResult


class AsyncRateLimiter:
    def __init__(self, min_interval_seconds: float) -> None:
        self._min_interval_seconds = max(min_interval_seconds, 0.0)
        self._next_allowed_at = 0.0
        self._lock = asyncio.Lock()

    async def wait(self) -> None:
        if self._min_interval_seconds <= 0:
            return
        async with self._lock:
            now = time.monotonic()
            if now < self._next_allowed_at:
                await asyncio.sleep(self._next_allowed_at - now)
            self._next_allowed_at = time.monotonic() + self._min_interval_seconds


_RATE_LIMITERS: dict[float, AsyncRateLimiter] = {}


def get_rate_limiter(requests_per_second: float) -> AsyncRateLimiter:
    rps = float(requests_per_second)
    if rps <= 0:
        raise ValueError("requests_per_second must be > 0")
    key = round(rps, 6)
    limiter = _RATE_LIMITERS.get(key)
    if limiter is None:
        limiter = AsyncRateLimiter(1.0 / rps)
        _RATE_LIMITERS[key] = limiter
    return limiter


class RateLimitedMCPServerStdio(MCPServerStdio):
    def __init__(
        self,
        params: MCPServerStdioParams,
        rate_limiter: AsyncRateLimiter,
        **kwargs: Any,
    ) -> None:
        super().__init__(params, **kwargs)
        self._rate_limiter = rate_limiter

    async def call_tool(
        self, tool_name: str, arguments: dict[str, Any] | None
    ) -> CallToolResult:
        await self._rate_limiter.wait()
        return await super().call_tool(tool_name, arguments)
