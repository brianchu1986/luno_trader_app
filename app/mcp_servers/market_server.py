# app/mcp_servers/market_server.py
from __future__ import annotations

import asyncio
from typing import Any, Dict, List

from mcp.server.fastmcp import FastMCP
from app.libs.market import (
    list_tradable_markets,
    get_market,
    get_market_last_trade,
    get_market_orderbook_top_levels,
    refresh_markets_cache,
)

mcp = FastMCP("market_server")


@mcp.tool()
async def list_tradable_myr_markets() -> List[str]:
    """This tool lists all tradable MYR markets available on Luno.

    Returns:
        A list of market_ids (e.g. ['XBTMYR', 'ETHMYR'])
    """
    return await asyncio.to_thread(list_tradable_markets)


@mcp.tool()
async def get_market_info(market_id: str) -> Dict[str, Any]:
    """This tool returns market details for a given MYR market.

    Args:
        market_id: the market identifier (e.g. XBTMYR, ETHMYR)
    """
    return await asyncio.to_thread(get_market, market_id)


@mcp.tool()
async def lookup_market_price(market_id: str) -> float:
    """This tool provides the current price of the given MYR market.

    Args:
        market_id: the market identifier (e.g. XBTMYR, ETHMYR)
    """
    return await asyncio.to_thread(get_market_last_trade, market_id)


@mcp.tool()
async def get_orderbook_top_levels(
    market_id: str, side: str, top_n: int = 10
) -> List[Dict[str, float]]:
    """
    Fetch a snapshot of the largest orderbook levels (by volume) for a given MYR market.

    This tool returns the most significant bid/ask price levels from the
    orderbook_top snapshot so agents can gauge liquidity and anchor LIMIT prices
    near meaningful size (avoid unreachable "wish prices").

    Args:
        market_id: MYR market identifier (e.g. "XBTMYR", "ETHMYR").
        side: Orderbook side to fetch: "bid" (buyers) or "ask" (sellers).
        top_n: Number of price levels to return (default 10).

    Returns:
        A list of order levels, each containing:
        - price (float): order price at that level
        - volume (float): total volume available at that level
    """
    return await asyncio.to_thread(
        get_market_orderbook_top_levels, market_id, side, top_n
    )


@mcp.tool()
async def refresh_markets() -> str:
    """Refresh the cached market list (MYR only)."""
    refresh_markets_cache()
    return "Markets cache refreshed"


if __name__ == "__main__":
    # print("✅ market_server MCP started (stdio)")
    mcp.run(transport="stdio")
