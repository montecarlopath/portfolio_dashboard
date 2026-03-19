from __future__ import annotations

import os
from typing import Any, Dict

import requests

from app.config import (
    load_alpaca_base_url,
    load_alpaca_key,
    load_alpaca_secret,
)
from app.schemas import BrokerCancelResponse


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


def _get_order_by_client_order_id(client_order_id: str) -> Dict[str, Any]:
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

    return resp.json()


def cancel_broker_order(
    *,
    broker: str = "alpaca",
    broker_order_id: str | None = None,
    client_order_id: str | None = None,
) -> BrokerCancelResponse:
    if broker != "alpaca":
        raise ValueError(f"Unsupported broker: {broker}")

    if not broker_order_id and not client_order_id:
        raise ValueError("Either broker_order_id or client_order_id must be provided.")

    resolved_client_order_id = client_order_id

    if not broker_order_id and client_order_id:
        order = _get_order_by_client_order_id(client_order_id)
        broker_order_id = order.get("id")
        resolved_client_order_id = order.get("client_order_id") or client_order_id

        if not broker_order_id:
            return BrokerCancelResponse(
                broker=broker,
                broker_environment=_broker_environment(),
                canceled=False,
                broker_order_id=None,
                client_order_id=client_order_id,
                status="not_found",
                message="No broker order id found for supplied client_order_id.",
                raw_response=order,
            )

    url = f"{_alpaca_base_url()}/orders/{broker_order_id}"
    resp = requests.delete(
        url,
        headers=_alpaca_headers(),
        timeout=20,
    )

    # Alpaca cancel by id is accepted with 204 No Content when successful.
    if resp.status_code == 204:
        return BrokerCancelResponse(
            broker=broker,
            broker_environment=_broker_environment(),
            canceled=True,
            broker_order_id=broker_order_id,
            client_order_id=resolved_client_order_id,
            status="cancel_requested",
            message="Cancel accepted by Alpaca.",
            raw_response={},
        )

    if resp.status_code >= 400:
        return BrokerCancelResponse(
            broker=broker,
            broker_environment=_broker_environment(),
            canceled=False,
            broker_order_id=broker_order_id,
            client_order_id=resolved_client_order_id,
            status="cancel_rejected",
            message=f"Alpaca rejected cancel request: {resp.status_code} {resp.text}",
            raw_response={},
        )

    # Defensive fallback if Alpaca changes response shape
    try:
        payload = resp.json()
    except Exception:
        payload = {}

    return BrokerCancelResponse(
        broker=broker,
        broker_environment=_broker_environment(),
        canceled=resp.ok,
        broker_order_id=broker_order_id,
        client_order_id=resolved_client_order_id,
        status="cancel_requested" if resp.ok else "cancel_unknown",
        message="Cancel request processed.",
        raw_response=payload,
    )