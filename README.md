# Luno Trader App (CLI Args)

This repo runs a multi-trader scheduler from `main.py`. Each trader has a
portfolio stored in the local DB. The CLI lets you configure timing, models,
accounts, and starting holdings.

## Quick start

```bash
python main.py
```

## Environment

- `LUNO_API_KEY` / `LUNO_API_SECRET` for live trading.
- `LUNO_ADMIN_KEY` / `LUNO_ADMIN_SECRET` for admin account actions.

## Common examples

Run two traders every 5 and 30 minutes:
```bash
python main.py --names Warren,George --run-every 5,30
```

Show current trader portfolios:
```bash
python main.py --show-portfolio
```
If you omit `--names`, it prints every account stored in the DB.
Filter by mode:
```bash
python main.py --show-portfolio --live-only
python main.py --show-portfolio --dry-run-only
```
Show balances from Luno (all accounts):
```bash
python main.py --show-luno-balances
```

Set per-trader holdings (order matches `--names`):
```bash
python main.py --names Warren,George --holdings "XBT:0.1,ETH:2;XBT:0.05"
```

Set per-trader strategies (registry keys or text):
```bash
python main.py --names Warren,George --strategies warren,george
```

Use one holdings group for all traders:
```bash
python main.py --holdings "ETH:1.5"
```

Admin: reset MYR accounts to MYR_0 and distribute to MYR_1..:
```bash
python main.py --myr-balances 20,10,3.5
```

## CLI arguments

- `--run-every`: Comma-separated minutes per trader (aligned with `--names`).
- `--many-models`: Use different models per trader (env `USE_MANY_MODELS`).
- `--model-default`: Override `MODEL_DEFAULT` for all traders.
- `--models`: Comma-separated model names aligned with `--names`.
- `--once`: Run one cycle then exit.
- `--names`: Comma-separated trader names (default: `Warren,George`).
- `--strategies`: Comma-separated strategy keys or text aligned with `--names`.
- `--log-level`: Override `LOG_LEVEL` (DEBUG/INFO/...).
- `--timeout-seconds`: Per-trader timeout override.
- `--live`: Use live trading mode (sends orders to Luno).
- `--myr-balances`: Admin reset + distribute MYR balances to `MYR_1..MYR_9`.
- `--holdings`: Per-trader portfolio holdings.
- `--show-portfolio`: Print trader balances/holdings from DB and exit.
- `--show-luno-balances`: Print balances from `client.get_balances()` and exit.
- `--live-only`: Filter `--show-portfolio` to live accounts.
- `--dry-run-only`: Filter `--show-portfolio` to dry_run accounts.

### Holdings format

- Use `ASSET:QTY` pairs, separated by commas.
- Use `;` to separate traders.
- If you provide one group, it applies to all traders.

Example:
```
XBT:0.1,ETH:2;XBT:0.05
```

### Strategy defaults

If a trader has no strategy set, the app applies a default based on name:
`warren`, `george`, `ray`, `cathie` (from `app/libs/strategy.py`).

### Admin MYR balances

`--myr-balances` uses the admin API key to:
1) Rename the unnamed MYR account to `MYR_0`.
2) Move all MYR balances into `MYR_0`.
3) Distribute amounts to `MYR_1..MYR_n`.

This command exits after completion and does not start the scheduler.
