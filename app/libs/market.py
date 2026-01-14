# app/libs/market.py
from __future__ import annotations

from typing import Any, Dict, List
from functools import lru_cache

import pandas as pd
from app import get_luno_client, get_counter_currency


def normalize_market_id(market_id: str) -> str:
    """
    Accepts: 'GRTMYR', 'GRT/MYR', 'grt_myr'
    Returns: 'GRTMYR'
    """
    s = market_id.strip().upper()
    return s.replace("/", "").replace("_", "").replace("-", "")


@lru_cache(maxsize=1)
def get_markets_raw() -> Dict[str, Any]:
    client = get_luno_client()
    return client.markets()


def get_markets_df() -> pd.DataFrame:
    """
    ACTIVE markets filtered by counter_currency (MYR).
    Uses market_id directly (no derived pair).
    """
    counter = get_counter_currency()
    raw = get_markets_raw()
    df = pd.DataFrame(raw.get("markets", []))

    if df.empty:
        return df

    df = df[
        (df["counter_currency"] == counter) &
        (df["trading_status"] == "ACTIVE")
    ]

    df = df.sort_values("market_id")
    return df


def list_tradable_markets() -> List[str]:
    """
    Returns tradable MYR market_ids, e.g. ['XBTMYR', 'ETHMYR']
    """
    df = get_markets_df()
    if df.empty:
        return []
    return df["market_id"].astype(str).tolist()


def assert_tradable_myr_market(market_id: str) -> str:
    m = normalize_market_id(market_id)
    tradable = set(list_tradable_markets())
    if m not in tradable:
        raise ValueError(f"Invalid market (must be ACTIVE {get_counter_currency()}): {m}")
    return m


def get_market(market_id: str) -> Dict[str, Any]:
    m = assert_tradable_myr_market(market_id)
    df = get_markets_df()
    row = df.loc[df["market_id"] == m]
    if row.empty:
        raise ValueError(f"Market not found: {m}")
    return row.iloc[0].to_dict()


def get_market_ticker(market_id: str) -> Dict[str, Any]:
    m = assert_tradable_myr_market(market_id)
    client = get_luno_client()
    return client.get_ticker(pair=m)


def get_market_last_trade(market_id: str) -> float:
    ticker = get_market_ticker(market_id)
    return float(ticker["last_trade"])


def refresh_markets_cache() -> None:
    get_markets_raw.cache_clear()
