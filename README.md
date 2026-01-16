# Luno Trader App

## Overview

-   Multi-trader, schedule-driven crypto trading system for the Luno exchange.
-   Each trader has a local portfolio record in SQLite and runs a strategy on a schedule.
-   Supports optional LLM models for decision support, plus a CLI for operations and reporting.
-   This project is NOT financial advice. It is for research and educational use only.
-   This is not a high-frequency or arbitrage bot; it targets low-frequency, strategy-driven trades.

## Architecture

Scheduler -> Trader -> (Optional Researcher) -> Execution

-   Scheduler: Triggers each trader on its own interval, adds jitter, and enforces per-run timeouts.
-   Trader: Loads its strategy and portfolio, then decides trades (optionally with LLM assistance).
-   Optional Researcher: Gathers market context via tooling; not required for execution.
-   Execution: Places orders via the Luno API in live mode or simulates in dry_run mode.
-   Risk checks are not a standalone component; basic safeguards live in account helpers and scheduler settings.

## Safety & Risk Controls

-   Default mode is dry_run; no real orders are sent unless you pass `--live`.
-   Per-trader timeouts and scheduler jitter reduce runaway or synchronized behavior.
-   Order sizing relies on available balances and helper sizing functions, not a formal risk model.
-   There is no built-in stop-loss, portfolio-level exposure limit, backtest engine, or slippage model.
-   LLM-driven decisions are probabilistic; you must review logs and outcomes.
-   Start with small balances, and only move to live trading after careful testing.

## Installation

-   Python 3.10+ is required.

```bash
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

Copy `.env.example` to `.env` and fill in values:

-   `LUNO_API_KEY` / `LUNO_API_SECRET`: Required for market access and live orders.
-   `LUNO_ADMIN_KEY` / `LUNO_ADMIN_SECRET`: Required only for MYR distribution.
-   `LOG_LEVEL`, `LOG_DIR`, `RUN_EVERY_N_MINUTES`, `TRADER_TIMEOUT_SECONDS`, `TRADER_MAX_TURNS`: Scheduler settings.
-   `MODEL_DEFAULT`, `MODEL_1`, `MODEL_2`: Optional model selection overrides.
-   `OPENAI_API_KEY`, `DEEPSEEK_API_KEY`, `BRAVE_SEARCH_API`: Optional tool and LLM integrations.

Note: `RUN_MODE` is not currently used; use `--live` to enable live trading.

## Quick Start

Single trader, dry_run, one cycle:

```bash
python main.py --names Warren --once
```

Multiple traders on different schedules:

```bash
python main.py --names Warren,George --run-every 5,30
```

Dry-run mode is the default; avoid `--live` while testing:

```bash
python main.py --names Warren --run-every 15
```

## Configuration

Key CLI flags:

-   `--names`: Trader account names (comma-separated).
-   `--run-every`: Minutes per trader (aligned with `--names`).
-   `--strategies`: Strategy keys or text aligned with `--names`.
-   `--models`, `--model-default`, `--many-models`: Model selection for LLM-assisted runs.
-   `--once`: Run one cycle then exit.
-   `--force-run`: Run immediately even if still within the cooldown window.
-   `--live`: Enable live trading (default is dry_run).
-   `--holdings`: Initialize per-trader portfolio holdings.
-   `--myr-balances`: Admin reset and distribute MYR across trader accounts.
-   `--show-portfolio`, `--show-luno-balances`: Reporting helpers.

Holdings format:

-   `ASSET:QTY` pairs, comma-separated.
-   Use `;` to separate traders (aligned with `--names`).
-   A single group applies to all traders.

Example:

```
XBT:0.1,ETH:2;XBT:0.05
```

MYR capital distribution (conceptual):

-   Use `--myr-balances` with admin credentials to consolidate funds into `MYR_0`,
    then distribute amounts to `MYR_1..MYR_n`.
-   Trader accounts are assigned to MYR sub-accounts for spending; this command exits
    after completion and does not start the scheduler.

## Cooldown Behavior

-   Each trader stores `last_run` in the local DB, so restarts do not trigger immediate repeat runs.
-   The cooldown window is based on `--run-every` (or `RUN_EVERY_N_MINUTES`).
-   If you restart within the cooldown window, the scheduler waits and logs a countdown.
-   Use `--force-run` to bypass the initial cooldown and run immediately.

## Project Status

-   Experimental and evolving; APIs, prompts, and defaults may change.
-   Risk controls are minimal and not audited.
-   LLM prompting and execution behavior can drift with model updates.
-   Not production-ready; test with small balances and monitor closely.

## Contributing

-   Add or tweak strategies in `app/libs/strategy.py` and register them in `STRATEGY_REGISTRY`.
-   Add risk checks or guardrails in `app/libs/account.py`, or add a new guard layer in `app/libs/traders.py`.
-   Execution logic lives in `app/libs/account.py` and the Luno client wrapper in `app/libs/client.py`.
-   Small, focused PRs are welcome; include tests or examples when possible.

## Disclaimer

This software is provided as-is for research and educational use only. It does not
constitute financial advice and is not suitable for most users. You are solely
responsible for any trades placed and any losses incurred. Trading cryptocurrencies
involves substantial risk and can result in total loss of capital.

## License

This project is licensed under the MIT License.
