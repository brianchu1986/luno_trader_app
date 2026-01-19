# app/libs/traders.py
from __future__ import annotations

import json
import os
import traceback
from contextlib import AsyncExitStack
from typing import Any
from dotenv import load_dotenv
from openai import AsyncOpenAI

from agents import Agent, Tool, Runner, OpenAIChatCompletionsModel, trace, get_current_trace
from agents.exceptions import MaxTurnsExceeded
from agents.mcp import MCPServerStdio

from app.libs.accounts_client import read_accounts_resource, read_strategy_resource
from app.libs.database import write_agent_output, write_log
from app.libs.mcp_rate_limiter import RateLimitedMCPServerStdio, get_rate_limiter
from app.libs.tracers import make_trace_id
from app.libs.templates import (
    researcher_instructions,
    trader_instructions,
    trade_message,
    rebalance_message,
    research_tool,
)

from app.mcp_servers.mcp_params import (
    trader_mcp_server_params,
    researcher_mcp_server_params,
)

_STDIO_PARAM_KEYS = {
    "command",
    "args",
    "env",
    "cwd",
    "encoding",
    "encoding_error_handler",
}

load_dotenv(override=True)


def _parse_max_turns(value, default: int = 30) -> int:
    if value is None:
        return default
    try:
        turns = int(value)
    except (TypeError, ValueError):
        return default
    return turns if turns > 0 else default


MAX_TURNS = _parse_max_turns(os.getenv("TRADER_MAX_TURNS"), default=30)


def _parse_rate_limit_rps(value):
    if value is None:
        return None
    try:
        rps = float(value)
    except (TypeError, ValueError):
        return None
    return rps if rps > 0 else None


def _stdio_params(params: dict) -> dict:
    return {key: params[key] for key in _STDIO_PARAM_KEYS if key in params}


def _build_mcp_stdio_server(params: dict, client_session_timeout_seconds: float):
    rate_limit_rps = _parse_rate_limit_rps(params.get("rate_limit_rps"))
    stdio_params = _stdio_params(params)
    server_kwargs = {
        "client_session_timeout_seconds": client_session_timeout_seconds,
    }
    if "use_structured_content" in params:
        server_kwargs["use_structured_content"] = bool(params["use_structured_content"])
    if rate_limit_rps:
        limiter = get_rate_limiter(rate_limit_rps)
        return RateLimitedMCPServerStdio(
            stdio_params,
            rate_limiter=limiter,
            **server_kwargs,
        )
    return MCPServerStdio(
        stdio_params,
        **server_kwargs,
    )

DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "")
DEEPSEEK_BASE_URL = "https://api.deepseek.com/v1"

deepseek_client = AsyncOpenAI(base_url=DEEPSEEK_BASE_URL, api_key=DEEPSEEK_API_KEY)


def get_model(model_name: str):
    """
    Return an Agents SDK model config.
    - If model_name contains 'deepseek', route via DeepSeek OpenAI-compatible endpoint.
    - Else return the raw name (Agents SDK will use default OpenAI client).
    """
    if "deepseek" in (model_name or "").lower():
        return OpenAIChatCompletionsModel(
            model=model_name, openai_client=deepseek_client
        )
    return model_name


async def build_researcher_agent(mcp_servers, model_name: str) -> Agent:
    return Agent(
        name="Researcher",
        instructions=researcher_instructions(),
        model=get_model(model_name),
        mcp_servers=mcp_servers,
    )


async def build_researcher_tool(mcp_servers, model_name: str) -> Tool:
    researcher = await build_researcher_agent(mcp_servers, model_name)
    return researcher.as_tool(
        tool_name="Researcher",
        tool_description=research_tool(),
    )


def _safe_account_text(account_payload: str) -> str:
    """
    Your accounts resource might return JSON or markdown text.
    We try to parse JSON; if it fails, return as-is.
    """
    try:
        data = json.loads(account_payload)
        # optional: remove big arrays if present
        if isinstance(data, dict):
            data.pop("portfolio_value_time_series", None)
        return json.dumps(data, ensure_ascii=False)
    except Exception:
        return account_payload


def _safe_output_text(output: Any) -> str:
    if output is None:
        return ""
    if isinstance(output, str):
        return output
    try:
        return json.dumps(output, ensure_ascii=False)
    except Exception:
        return str(output)


class Trader:
    def __init__(
        self, name: str, lastname: str = "Trader", model_name: str = "gpt-5-mini"
    ):
        self.name = name
        self.lastname = lastname
        self.model_name = model_name
        self.agent: Agent | None = None

        # toggle: trade -> rebalance -> trade...
        self.do_trade = True

    async def create_agent(self, trader_mcp_servers, researcher_mcp_servers) -> Agent:
        researcher_tool = await build_researcher_tool(
            researcher_mcp_servers, self.model_name
        )

        self.agent = Agent(
            name=self.name,
            instructions=trader_instructions(self.name),
            model=get_model(self.model_name),
            tools=[researcher_tool],
            mcp_servers=trader_mcp_servers,
        )
        return self.agent

    async def get_account_report(self) -> str:
        payload = await read_accounts_resource(self.name)
        return _safe_account_text(payload)

    async def run_agent(self, trader_mcp_servers, researcher_mcp_servers) -> None:
        self.agent = await self.create_agent(trader_mcp_servers, researcher_mcp_servers)

        account = await self.get_account_report()
        strategy = await read_strategy_resource(self.name)

        prompt = (
            trade_message(self.name, strategy, account)
            if self.do_trade
            else rebalance_message(self.name, strategy, account)
        )

        try:
            result = await Runner.run(self.agent, prompt, max_turns=MAX_TURNS)
            final_output = result.final_output
            output_text = _safe_output_text(final_output)
            current_trace = get_current_trace()
            trace_id = current_trace.trace_id if current_trace else None
            run_type = "trade" if self.do_trade else "rebalance"
            write_agent_output(
                self.name,
                final_output,
                trace_id=trace_id,
                run_type=run_type,
            )
            write_log(self.name, "agent_output", output_text)
        except MaxTurnsExceeded:
            print(
                f"Trader {self.name} hit max turns ({MAX_TURNS}). "
                "Consider increasing TRADER_MAX_TURNS or tightening prompts."
            )

    async def run_with_mcp_servers(self) -> None:
        """
        Open ALL MCP servers under ONE AsyncExitStack so they are properly closed.
        """
        async with AsyncExitStack() as stack:
            trader_mcp_servers = [
                await stack.enter_async_context(
                    _build_mcp_stdio_server(
                        params, client_session_timeout_seconds=120
                    )
                )
                for params in trader_mcp_server_params
            ]

            researcher_params_list = researcher_mcp_server_params(self.name)
            researcher_mcp_servers = [
                await stack.enter_async_context(
                    _build_mcp_stdio_server(
                        params, client_session_timeout_seconds=120
                    )
                )
                for params in researcher_params_list
            ]

            await self.run_agent(trader_mcp_servers, researcher_mcp_servers)

    async def run_with_trace(self) -> None:
        trace_name = (
            f"{self.name}-trading" if self.do_trade else f"{self.name}-rebalancing"
        )
        trace_id = make_trace_id(self.name.lower())

        # If you have a LogTracer processor somewhere else, you can register it globally in your app bootstrap.
        with trace(trace_name, trace_id=trace_id):
            await self.run_with_mcp_servers()

    import traceback

    async def run(self):
        try:
            await self.run_with_trace()

        except* Exception as eg:
            print(
                f"❌ Trader {self.name} failed with ExceptionGroup ({len(eg.exceptions)} errors):"
            )

            for i, e in enumerate(eg.exceptions, 1):
                print(f"\n--- sub-exception #{i} ---")
                traceback.print_exception(type(e), e, e.__traceback__)

        finally:
            # Toggle trade / rebalance
            self.do_trade = not self.do_trade
