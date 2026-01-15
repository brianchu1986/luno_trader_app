# app/mcp_servers/market_server.py
from __future__ import annotations

import asyncio
from typing import Any, Dict, List

from mcp.server.fastmcp import FastMCP
from app.libs.market import (
    list_tradable_markets,
    get_market,
    get_market_last_trade,
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
async def refresh_markets() -> str:
    """Refresh the cached market list (MYR only)."""
    refresh_markets_cache()
    return "Markets cache refreshed"


if __name__ == "__main__":
    # print("✅ market_server MCP started (stdio)")
    mcp.run(transport="stdio")
