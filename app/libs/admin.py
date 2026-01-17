# app/libs/admin.py
from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_DOWN
from typing import Any, Iterable

from app import get_counter_currency, get_luno_admin_client

MAX_MYR_BALANCE_SLOTS = 9
MYR_ACCOUNT_PREFIX = "MYR_"
MYR0_NAME = "MYR_0"
MYR_SCALE = 2


def parse_myr_balances_arg(raw: str | None) -> list[Decimal]:
    """Parse --myr-balances into a list of Decimal values."""
    if raw is None:
        return []
    parts = [p.strip() for p in str(raw).split(",") if p.strip()]
    if not parts:
        return []
    values = _coerce_decimal_list(parts)
    _validate_myr_balances(values)
    return values


def reset_myr_balances(
    client: Any | None = None, currency: str | None = None
) -> dict[str, Any]:
    """Rename the unnamed MYR account to MYR_0, then move all other MYR balances to MYR_0."""
    client = client or get_luno_admin_client()
    currency = (currency or get_counter_currency()).upper()

    accounts, myr0, renamed = _ensure_myr0_account(client, currency)
    myr0_id = str(myr0.get("account_id", "")).strip()
    if not myr0_id:
        raise ValueError("MYR_0 account_id missing.")

    moved_total = Decimal("0")
    move_count = 0
    for acct in accounts:
        acct_id = str(acct.get("account_id", "")).strip()
        if not acct_id or acct_id == myr0_id:
            continue
        available = _parse_available(
            acct.get("balance"), acct.get("reserved"), acct.get("unconfirmed")
        )
        amount = _quantize_down(available, MYR_SCALE)
        if amount <= 0:
            continue
        client.move(_format_amount(amount), myr0_id, acct_id)
        moved_total += amount
        move_count += 1

    return {
        "myr0_account_id": myr0_id,
        "renamed": renamed,
        "moved_total": _format_amount(moved_total),
        "move_count": move_count,
    }


def distribute_myr_balances(
    balances: Iterable[Any], client: Any | None = None, currency: str | None = None
) -> dict[str, Any]:
    """Reset MYR accounts, then distribute balances to MYR_1..MYR_9."""
    values = _coerce_decimal_list(balances)
    if not values:
        raise ValueError("No MYR balances provided.")
    _validate_myr_balances(values)

    client = client or get_luno_admin_client()
    currency = (currency or get_counter_currency()).upper()

    reset_summary = reset_myr_balances(client=client, currency=currency)

    accounts = _load_currency_accounts(client, currency)
    name_map = _index_accounts_by_name(accounts)
    myr0 = name_map.get(MYR0_NAME)
    if myr0 is None:
        raise ValueError("MYR_0 account not found after reset.")

    myr0_id = str(myr0.get("account_id", "")).strip()
    if not myr0_id:
        raise ValueError("MYR_0 account_id missing.")

    available = _parse_available(
        myr0.get("balance"), myr0.get("reserved"), myr0.get("unconfirmed")
    )
    available = _quantize_down(available, MYR_SCALE)

    total_needed = sum(
        (_quantize_down(v, MYR_SCALE) for v in values), Decimal("0")
    )
    if total_needed > available:
        raise ValueError(
            "Insufficient MYR_0 balance. "
            f"Available {_format_amount(available)} < requested {_format_amount(total_needed)}."
        )

    required_names = [
        f"{MYR_ACCOUNT_PREFIX}{i}" for i in range(1, len(values) + 1)
    ]
    accounts, created_accounts = _ensure_named_accounts(
        client, currency, required_names
    )

    name_map = _index_accounts_by_name(accounts)
    myr0 = name_map.get(MYR0_NAME)
    if myr0 is None:
        raise ValueError("MYR_0 account not found after account creation.")

    myr0_id = str(myr0.get("account_id", "")).strip()
    if not myr0_id:
        raise ValueError("MYR_0 account_id missing.")

    available_after = _parse_available(
        myr0.get("balance"), myr0.get("reserved"), myr0.get("unconfirmed")
    )
    available_after = _quantize_down(available_after, MYR_SCALE)
    if total_needed > available_after:
        raise ValueError(
            "Insufficient MYR_0 balance after account creation. "
            f"Available {_format_amount(available_after)} < requested {_format_amount(total_needed)}."
        )

    move_count = 0
    for idx, amount in enumerate(values, 1):
        amt = _quantize_down(amount, MYR_SCALE)
        if amt <= 0:
            continue
        target = name_map.get(f"{MYR_ACCOUNT_PREFIX}{idx}")
        if target is None:
            raise ValueError(f"Missing {MYR_ACCOUNT_PREFIX}{idx} account.")
        target_id = str(target.get("account_id", "")).strip()
        if not target_id:
            raise ValueError(f"Missing account_id for {MYR_ACCOUNT_PREFIX}{idx}.")
        client.move(_format_amount(amt), target_id, myr0_id)
        move_count += 1

    remaining = available_after - total_needed
    return {
        "myr0_account_id": myr0_id,
        "created_accounts": created_accounts,
        "distribution_total": _format_amount(total_needed),
        "move_count": move_count,
        "remaining_balance": _format_amount(remaining),
        "reset": reset_summary,
    }


def _coerce_decimal_list(values: Iterable[Any]) -> list[Decimal]:
    out: list[Decimal] = []
    for v in values:
        try:
            out.append(Decimal(str(v)))
        except InvalidOperation as exc:
            raise ValueError(f"Invalid MYR balance value: {v}") from exc
    return out


def _validate_myr_balances(values: list[Decimal]) -> None:
    if len(values) > MAX_MYR_BALANCE_SLOTS:
        raise ValueError(
            f"Too many MYR balances: {len(values)} (max {MAX_MYR_BALANCE_SLOTS})"
        )
    for v in values:
        if not v.is_finite():
            raise ValueError(f"Invalid MYR balance value: {v}")
        if v < 0:
            raise ValueError(f"Negative MYR balance not allowed: {v}")


def _normalize_name(raw: Any) -> str:
    return str(raw or "").strip()


def _to_decimal(raw: Any) -> Decimal:
    try:
        return Decimal(str(raw or "0"))
    except InvalidOperation:
        return Decimal("0")


def _quantize_down(value: Decimal, scale: int) -> Decimal:
    q = Decimal("1").scaleb(-scale)
    return value.quantize(q, rounding=ROUND_DOWN)


def _format_amount(value: Decimal) -> str:
    return format(_quantize_down(value, MYR_SCALE), "f")


def _parse_available(balance: Any, reserved: Any, unconfirmed: Any = None) -> Decimal:
    available = (
        _to_decimal(balance)
        - _to_decimal(reserved)
        - _to_decimal(unconfirmed)
    )
    return available if available > 0 else Decimal("0")


def _load_currency_accounts(
    client: Any, currency: str
) -> list[dict[str, Any]]:
    res = client.get_balances()
    rows = res.get("balance", [])
    if not isinstance(rows, list):
        raise ValueError("Unexpected balance response from Luno.")
    accounts: list[dict[str, Any]] = []
    for row in rows:
        asset = str(row.get("asset", "")).upper()
        if asset != currency:
            continue
        account = dict(row)
        account["account_id"] = str(account.get("account_id", "")).strip()
        account["name"] = _normalize_name(account.get("name"))
        accounts.append(account)
    if not accounts:
        raise ValueError(f"No {currency} accounts returned from Luno.")
    return accounts


def _index_accounts_by_name(
    accounts: list[dict[str, Any]]
) -> dict[str, dict[str, Any]]:
    name_map: dict[str, dict[str, Any]] = {}
    duplicates = set()
    for acct in accounts:
        name = acct.get("name", "")
        if not name:
            continue
        key = name.upper()
        if key in name_map:
            duplicates.add(key)
            continue
        name_map[key] = acct
    if duplicates:
        dup_names = ", ".join(sorted(duplicates))
        raise ValueError(f"Duplicate MYR account names: {dup_names}")
    return name_map


def _ensure_myr0_account(
    client: Any, currency: str
) -> tuple[list[dict[str, Any]], dict[str, Any], bool]:
    accounts = _load_currency_accounts(client, currency)
    name_map = _index_accounts_by_name(accounts)
    myr0 = name_map.get(MYR0_NAME)
    renamed = False

    if myr0 is None:
        unnamed = [a for a in accounts if not a.get("name")]
        if len(unnamed) != 1:
            raise ValueError(
                "Expected exactly one unnamed MYR account to rename to MYR_0."
            )
        target = unnamed[0]
        client.update_account_name(target["account_id"], MYR0_NAME)
        renamed = True
        accounts = _load_currency_accounts(client, currency)
        name_map = _index_accounts_by_name(accounts)
        myr0 = name_map.get(MYR0_NAME)
        if myr0 is None:
            raise ValueError("Failed to rename unnamed account to MYR_0.")
        return accounts, myr0, renamed

    if myr0.get("name") != MYR0_NAME:
        client.update_account_name(myr0["account_id"], MYR0_NAME)
        renamed = True
        accounts = _load_currency_accounts(client, currency)
        name_map = _index_accounts_by_name(accounts)
        myr0 = name_map.get(MYR0_NAME)
        if myr0 is None:
            raise ValueError("Failed to confirm MYR_0 rename.")

    return accounts, myr0, renamed


def _ensure_named_accounts(
    client: Any, currency: str, required_names: list[str]
) -> tuple[list[dict[str, Any]], list[str]]:
    accounts = _load_currency_accounts(client, currency)
    name_map = _index_accounts_by_name(accounts)
    missing = [n for n in required_names if n.upper() not in name_map]
    if not missing:
        return accounts, []

    if len(accounts) + len(missing) > 10:
        raise ValueError("Creating accounts would exceed Luno limit of 10 per currency.")

    for name in missing:
        client.create_account(currency, name)

    accounts = _load_currency_accounts(client, currency)
    return accounts, missing
