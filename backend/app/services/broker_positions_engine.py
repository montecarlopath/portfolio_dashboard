from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

import requests

from app.config import (
    load_alpaca_base_url,
    load_alpaca_key,
    load_alpaca_secret,
)
from app.schemas import BrokerPositionRow, BrokerPositionsResponse


def _safe_float(value, default: float | None = None) -> float | None:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except Exception:
        return default


def _alpaca_base_url() -> str:
    env = os.getenv("ALPACA_ENV", "paper").strip().lower()
    configured_base_url = (load_alpaca_base_url() or "").strip()

    if configured_base_url:
        return configured_base_url.rstrip("/")

    if env == "paper":
        return "https://paper-api.alpaca.markets/v2"
    return "https://api.alpaca.markets/v2"


def _alpaca_headers() -> Dict[str, str]:
    api_key = os.getenv("ALPACA_API_KEY") or load_alpaca_key()
    api_secret = os.getenv("ALPACA_API_SECRET") or load_alpaca_secret()

    if not api_key or not api_secret:
        raise RuntimeError("Missing Alpaca credentials in env or config.json.")

    return {
        "accept": "application/json",
        "Apca-Api-Key-Id": api_key,
        "Apca-Api-Secret-Key": api_secret,
    }


def _broker_environment() -> str:
    return os.getenv("ALPACA_ENV", "paper").strip().lower()


def _normalize_position(row: Dict[str, Any]) -> BrokerPositionRow:
    return BrokerPositionRow(
        broker="alpaca",
        broker_environment=_broker_environment(),
        symbol=str(row.get("symbol") or ""),
        asset_class=row.get("asset_class"),
        exchange=row.get("exchange"),
        qty=_safe_float(row.get("qty")),
        side=row.get("side"),
        market_value=_safe_float(row.get("market_value")),
        cost_basis=_safe_float(row.get("cost_basis")),
        avg_entry_price=_safe_float(row.get("avg_entry_price")),
        unrealized_pl=_safe_float(row.get("unrealized_pl")),
        unrealized_plpc=_safe_float(row.get("unrealized_plpc")),
        unrealized_intraday_pl=_safe_float(row.get("unrealized_intraday_pl")),
        unrealized_intraday_plpc=_safe_float(row.get("unrealized_intraday_plpc")),
        current_price=_safe_float(row.get("current_price")),
        lastday_price=_safe_float(row.get("lastday_price")),
        change_today=_safe_float(row.get("change_today")),
        raw_position=row,
        notes=[],
    )


def get_broker_positions(
    *,
    broker: str = "alpaca",
    symbol: Optional[str] = None,
) -> BrokerPositionsResponse:
    if broker != "alpaca":
        raise ValueError(f"Unsupported broker: {broker}")

    base_url = _alpaca_base_url()
    headers = _alpaca_headers()

    if symbol:
        url = f"{base_url}/positions/{symbol}"
        resp = requests.get(url, headers=headers, timeout=20)

        if resp.status_code == 404:
            return BrokerPositionsResponse(
                broker=broker,
                broker_environment=_broker_environment(),
                positions=[],
                notes=[f"No open Alpaca position found for symbol {symbol}."],
            )

        if resp.status_code >= 400:
            raise RuntimeError(f"Alpaca get-position failed: {resp.status_code} {resp.text}")

        payload = resp.json()
        return BrokerPositionsResponse(
            broker=broker,
            broker_environment=_broker_environment(),
            positions=[_normalize_position(payload)],
            notes=["Fetched single open Alpaca position by symbol."],
        )

    url = f"{base_url}/positions"
    resp = requests.get(url, headers=headers, timeout=20)

    if resp.status_code >= 400:
        raise RuntimeError(f"Alpaca list-positions failed: {resp.status_code} {resp.text}")

    payload = resp.json()
    rows = [_normalize_position(row) for row in payload]

    return BrokerPositionsResponse(
        broker=broker,
        broker_environment=_broker_environment(),
        positions=rows,
        notes=["Fetched all open Alpaca positions."],
    )