# app/mcp_servers/mcp_params.py
from __future__ import annotations

import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(override=True)

# -------------------------------------------------
# Paths (safe + explicit)
# -------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = BASE_DIR.parent.parent  # adjust if needed

MCP_DIR = BASE_DIR
MEMORY_DIR = PROJECT_ROOT / "memory"
MEMORY_DIR.mkdir(parents=True, exist_ok=True)


# -------------------------------------------------
# Env vars
# -------------------------------------------------
def _float_env(name: str, default: float) -> float:
    raw = os.getenv(name, "")
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


BRAVE_API_KEY = os.getenv("BRAVE_SEARCH_API")
BRAVE_REQUESTS_PER_SECOND = _float_env("BRAVE_REQUESTS_PER_SECOND", 1.0)

brave_env = {}
if BRAVE_API_KEY:
    brave_env["BRAVE_API_KEY"] = BRAVE_API_KEY

LUNO_API_KEY = os.getenv("LUNO_API_KEY")
LUNO_API_SECRET = os.getenv("LUNO_API_SECRET")
luno_env = {}
if LUNO_API_KEY:
    luno_env["LUNO_API_KEY"] = LUNO_API_KEY
if LUNO_API_SECRET:
    luno_env["LUNO_API_SECRET"] = LUNO_API_SECRET

# -------------------------------------------------
# Trader MCP servers
# -------------------------------------------------
# Use module execution so imports work reliably
trader_mcp_server_params = [
    {
        "command": "uv",
        "args": ["run", "-m", "app.mcp_servers.accounts_server"],
    },
    {
        "command": "uv",
        "args": ["run", "-m", "app.mcp_servers.market_server"],
    },
    {
        "command": "uv",
        "args": ["run", "-m", "app.mcp_servers.risk_server"],
    },
]

if luno_env:
    for params in trader_mcp_server_params:
        params["env"] = luno_env


# -------------------------------------------------
# Researcher MCP servers
# -------------------------------------------------
def researcher_mcp_server_params(name: str):
    params = [
        # Simple fetch server (local, more robust)
        {
            "command": "uv",
            "args": ["run", "-m", "app.mcp_servers.fetch_server"],
        },
    ]

    # Brave search (only if API key exists)
    if brave_env:
        params.append(
            {
                "command": "npx",
                "args": ["-y", "@modelcontextprotocol/server-brave-search"],
                "env": brave_env,
                "rate_limit_rps": BRAVE_REQUESTS_PER_SECOND,
                "use_structured_content": True,
            }
        )

    # Memory (per-trader persistent memory)
    params.append(
        {
            "command": "npx",
            "args": ["-y", "mcp-memory-libsql"],
            "env": {"LIBSQL_URL": f"file:{MEMORY_DIR / f'{name}.db'}"},
        }
    )

    return params
