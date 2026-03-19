from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

import requests

from app.config import (
    load_alpaca_base_url,
    load_alpaca_key,
    load_alpaca_secret,
)
from app.schemas import (
    BrokerOrderStatusLeg,
    BrokerOrderStatusResponse,
    BrokerOrderStatusRow,
)


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


def _normalize_order(order: Dict[str, Any]) -> BrokerOrderStatusRow:
    legs: List[BrokerOrderStatusLeg] = []
    for leg in order.get("legs", []) or []:
        legs.append(
            BrokerOrderStatusLeg(
                symbol=str(leg.get("symbol") or ""),
                side=leg.get("side"),
                ratio_qty=str(leg.get("ratio_qty")) if leg.get("ratio_qty") is not None else None,
                position_intent=leg.get("position_intent"),
            )
        )

    return BrokerOrderStatusRow(
        broker_order_id=order.get("id"),
        client_order_id=order.get("client_order_id"),
        status=str(order.get("status") or "unknown"),
        order_class=order.get("order_class"),
        order_type=order.get("type"),
        time_in_force=order.get("time_in_force"),
        limit_price=_safe_float(order.get("limit_price")),
        qty=_safe_float(order.get("qty")),
        filled_qty=_safe_float(order.get("filled_qty"), 0.0),
        avg_fill_price=_safe_float(order.get("filled_avg_price")),
        submitted_at=order.get("submitted_at"),
        filled_at=order.get("filled_at"),
        canceled_at=order.get("canceled_at"),
        expired_at=order.get("expired_at"),
        failed_at=order.get("failed_at"),
        legs=legs,
        raw_status=order,
        notes=[],
    )


def _get_order_by_id(order_id: str) -> BrokerOrderStatusRow:
    url = f"{_alpaca_base_url()}/orders/{order_id}"
    resp = requests.get(url, headers=_alpaca_headers(), timeout=20)

    if resp.status_code >= 400:
        raise RuntimeError(f"Alpaca get-order failed: {resp.status_code} {resp.text}")

    payload = resp.json()
    return _normalize_order(payload)


def _get_order_by_client_order_id(client_order_id: str) -> BrokerOrderStatusRow:
    # Alpaca has a dedicated client-order-id retrieval endpoint.
    url = f"{_alpaca_base_url()}/orders:by_client_order_id"
    resp = requests.get(
        url,
        headers=_alpaca_headers(),
        params={"client_order_id": client_order_id},
        timeout=20,
    )

    if resp.status_code >= 400:
        raise RuntimeError(
            f"Alpaca get-order-by-client-id failed: {resp.status_code} {resp.text}"
        )

    payload = resp.json()
    return _normalize_order(payload)


def _list_orders(
    *,
    status: str = "open",
    limit: int = 50,
    nested: bool = True,
) -> List[BrokerOrderStatusRow]:
    url = f"{_alpaca_base_url()}/orders"
    params = {
        "status": status,
        "limit": limit,
        "nested": str(nested).lower(),
        "direction": "desc",
    }

    resp = requests.get(url, headers=_alpaca_headers(), params=params, timeout=20)

    if resp.status_code >= 400:
        raise RuntimeError(f"Alpaca list-orders failed: {resp.status_code} {resp.text}")

    payload = resp.json()
    return [_normalize_order(row) for row in payload]


def get_broker_order_status(
    *,
    broker: str = "alpaca",
    broker_order_id: Optional[str] = None,
    client_order_id: Optional[str] = None,
    open_only: bool = True,
    limit: int = 50,
    hedge_only: bool = True,
) -> BrokerOrderStatusResponse:
    if broker != "alpaca":
        raise ValueError(f"Unsupported broker: {broker}")

    notes: List[str] = []
    rows: List[BrokerOrderStatusRow] = []

    if broker_order_id:
        rows = [_get_order_by_id(broker_order_id)]
        notes.append("Fetched single order by broker order id.")
    elif client_order_id:
        rows = [_get_order_by_client_order_id(client_order_id)]
        notes.append("Fetched single order by client order id.")
    else:
        rows = _list_orders(
            status="open" if open_only else "all",
            limit=limit,
            nested=True,
        )
        notes.append(
            "Fetched order list from Alpaca."
        )
    if hedge_only:
        rows = [
            row for row in rows
            if (row.client_order_id or "").startswith("hedge-")
        ]
        notes.append("Filtered to hedge orders only by client_order_id prefix.")


    return BrokerOrderStatusResponse(
        broker=broker,
        broker_environment=_broker_environment(),
        queried_open_only=open_only if not broker_order_id and not client_order_id else False,
        orders=rows,
        notes=notes,
    )
