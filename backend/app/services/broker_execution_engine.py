from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

from app.config import (
    load_alpaca_base_url,
    load_alpaca_key,
    load_alpaca_secret,
)
from app.schemas import BrokerOrderPayloadResponse, BrokerSubmissionResult
from app.services.broker_payload_engine import build_broker_order_payloads
from app.services.broker_submission_store import log_submission_attempt



AUDIT_DIR = Path("data/broker_order_audit")
AUDIT_DIR.mkdir(parents=True, exist_ok=True)

RECENT_ORDER_MEMORY: dict[str, datetime] = {}


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _paper_only_guard(broker: str) -> tuple[bool, str]:
    env = os.getenv("ALPACA_ENV", "paper").strip().lower()
    live_enabled = os.getenv("ALPACA_LIVE_SUBMIT_ENABLED", "false").strip().lower() == "true"

    if broker != "alpaca":
        return False, f"Unsupported broker: {broker}"

    if env == "live" and not live_enabled:
        return False, "Live Alpaca submission is disabled by safety guard."

    return True, ""




# replace the existing _duplicate_order_guard function with this

def _duplicate_order_guard(client_order_id: str, lookback_minutes: int = 30) -> tuple[bool, str]:
    now = _utc_now()
    cutoff = now - timedelta(minutes=lookback_minutes)

    # 1. clean stale in-memory entries
    stale_keys = [k for k, ts in RECENT_ORDER_MEMORY.items() if ts < cutoff]
    for k in stale_keys:
        RECENT_ORDER_MEMORY.pop(k, None)

    # 2. check in-memory (catches same session, recent submits)
    if client_order_id in RECENT_ORDER_MEMORY:
        return False, "Duplicate client_order_id detected in recent submission window."

    # 3. check persistent store (catches server restarts + same-day replans)
    try:
        from app.services.broker_submission_store import find_recent_by_client_order_id
        existing = find_recent_by_client_order_id(client_order_id)
        if existing:
            state = existing.get("lifecycle_state", "")
            if state not in ("replaced", "cancelled", "expired", "failed"):
                return False, (
                    f"client_order_id already exists in store with state '{state}'. "
                    "Order not resubmitted."
                )
    except Exception as e:
        logger.warning("Could not check submission store for duplicate: %s", e)
        # don't block submission if store check fails

    return True, ""


def _persist_submission_log(order, mode: str) -> None:
    ts = _utc_now().strftime("%Y%m%dT%H%M%SZ")
    fname = AUDIT_DIR / f"{ts}_{order.client_order_id}_{mode}.json"

    payload = {
        "timestamp_utc": ts,
        "mode": mode,
        "client_order_id": order.client_order_id,
        "order_intent": order.order_intent,
        "broker": order.broker,
        "broker_environment": getattr(order, "broker_environment", None),
        "underlying": order.underlying,
        "alpaca_payload": order.alpaca_payload,
        "validation": order.validation.model_dump(),
        "submission_result": order.submission_result.model_dump() if order.submission_result else None,
    }

    with open(fname, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)


def _local_precheck(order) -> tuple[bool, str]:
    if not order.validation.executable_now:
        return False, "Local validation failed."
    if not order.validation.broker_payload_complete:
        return False, "Broker payload is incomplete."
    if order.limit_price is None or order.limit_price <= 0:
        return False, "Invalid limit price."
    if order.qty is None or order.qty <= 0:
        return False, "Invalid order quantity."
    if not order.order_intent or order.order_intent == "unknown":
        return False, "Unknown order intent."
    return True, ""


def _mark_precheck(order, passed: bool) -> None:
    order.validation.broker_precheck_passed = passed


def _submit_to_alpaca(order) -> BrokerSubmissionResult:
    ok, msg = _local_precheck(order)
    _mark_precheck(order, ok)
    if not ok:
        return BrokerSubmissionResult(
            mode="submit",
            submitted=False,
            broker_order_id=None,
            client_order_id=order.client_order_id,
            status="rejected_local",
            message=msg,
        )

    ok, msg = _paper_only_guard(order.broker)
    if not ok:
        return BrokerSubmissionResult(
            mode="submit",
            submitted=False,
            broker_order_id=None,
            client_order_id=order.client_order_id,
            status="blocked_safety_guard",
            message=msg,
        )

    ok, msg = _duplicate_order_guard(order.client_order_id)
    if not ok:
        return BrokerSubmissionResult(
            mode="submit",
            submitted=False,
            broker_order_id=None,
            client_order_id=order.client_order_id,
            status="duplicate_blocked",
            message=msg,
        )

    env = os.getenv("ALPACA_ENV", "paper").strip().lower()

    api_key = os.getenv("ALPACA_API_KEY") or load_alpaca_key()
    api_secret = os.getenv("ALPACA_API_SECRET") or load_alpaca_secret()

    if not api_key or not api_secret:
        return BrokerSubmissionResult(
            mode="submit",
            submitted=False,
            broker_order_id=None,
            client_order_id=order.client_order_id,
            status="missing_credentials",
            message="Missing Alpaca credentials in env or config.json.",
        )

    configured_base_url = (load_alpaca_base_url() or "").strip()

    if configured_base_url:
        base_url = configured_base_url.rstrip("/")
    else:
        if env == "paper":
            base_url = "https://paper-api.alpaca.markets/v2"
        else:
            base_url = "https://api.alpaca.markets/v2"

    url = f"{base_url}/orders"
    headers = {
        "accept": "application/json",
        "content-type": "application/json",
        "Apca-Api-Key-Id": api_key,
        "Apca-Api-Secret-Key": api_secret,
    }

    try:
        resp = requests.post(
            url,
            headers=headers,
            json=order.alpaca_payload,
            timeout=20,
        )
    except Exception as e:
        return BrokerSubmissionResult(
            mode="submit",
            submitted=False,
            broker_order_id=None,
            client_order_id=order.client_order_id,
            status="request_failed",
            message=f"Alpaca request failed: {e}",
        )

    if resp.status_code >= 400:
        return BrokerSubmissionResult(
            mode="submit",
            submitted=False,
            broker_order_id=None,
            client_order_id=order.client_order_id,
            status="broker_rejected",
            message=f"Alpaca rejected order: {resp.status_code} {resp.text}",
        )

    try:
        payload = resp.json()
    except Exception:
        payload = {}

    RECENT_ORDER_MEMORY[order.client_order_id] = _utc_now()

    # Write to lifecycle store so the monitor loop can track this order.
    try:
        log_submission_attempt(
            client_order_id=order.client_order_id,
            mode="submit",
            broker=order.broker,
            broker_environment=getattr(order, "broker_environment", "paper"),
            payload=order.alpaca_payload,
            status="submitted",
            broker_order_id=payload.get("id"),
            message="Order submitted to Alpaca successfully.",
            estimated_debit_dollars=getattr(order, "estimated_debit_dollars", None),
            estimated_coverage_dollars=getattr(order, "estimated_coverage_added_dollars", None),
            qty=getattr(order, "qty", None),
            underlying=getattr(order, "underlying", None),
            ticket_bucket=getattr(order, "ticket_bucket", None),
            ticket_action=getattr(order, "ticket_action", None),
        )
    except Exception as _log_exc:
        # Never let store writes break the submission response.
        import logging
        logging.getLogger(__name__).warning(
            "Failed to write to submission store for %s: %s",
            order.client_order_id, _log_exc,
        )
 
    return BrokerSubmissionResult(
        mode="submit",
        submitted=True,
        broker_order_id=payload.get("id"),
        client_order_id=order.client_order_id,
        status=str(payload.get("status") or "submitted"),
        message="Order submitted to Alpaca successfully.",
    )



def execute_broker_orders(
    *,
    db,
    account_ids: list[str],
    as_of_date: str,
    underlying: str,
    market_regime: str,
    hedge_style: str,
    portfolio_value: float,
    current_hedge_pct: float,
    recommended_hedge_pct: float,
    additional_hedge_pct: float,
    remaining_hedge_budget_pct: float,
    broker: str = "alpaca",
    mode: str = "preview",
    limit_price_buffer_pct: float = 0.0,
    max_slippage_pct: float = 0.02,
) -> BrokerOrderPayloadResponse:
    response = build_broker_order_payloads(
        db=db,
        account_ids=account_ids,
        as_of_date=as_of_date,
        underlying=underlying,
        market_regime=market_regime,
        hedge_style=hedge_style,
        portfolio_value=portfolio_value,
        current_hedge_pct=current_hedge_pct,
        recommended_hedge_pct=recommended_hedge_pct,
        additional_hedge_pct=additional_hedge_pct,
        remaining_hedge_budget_pct=remaining_hedge_budget_pct,
        broker=broker,
        mode=mode,
        limit_price_buffer_pct=limit_price_buffer_pct,
        max_slippage_pct=max_slippage_pct,
    )

    if mode == "preview":
        for order in response.orders:
            _mark_precheck(order, False)
            order.submission_result = BrokerSubmissionResult(
                mode=mode,
                submitted=False,
                broker_order_id=None,
                client_order_id=order.client_order_id,
                status="preview_ready",
                message="Preview only; not submitted.",
            )
            _persist_submission_log(order, mode)
        return response

    if mode == "dry_run":
        for order in response.orders:
            ok, msg = _local_precheck(order)
            _mark_precheck(order, ok)
            order.submission_result = BrokerSubmissionResult(
                mode=mode,
                submitted=False,
                broker_order_id=None,
                client_order_id=order.client_order_id,
                status="dry_run_ready" if ok else "dry_run_failed",
                message="Dry run only; validated locally and not submitted." if ok else msg,
            )
            _persist_submission_log(order, mode)
        return response

    if mode == "submit":
        for order in response.orders:
            order.submission_result = _submit_to_alpaca(order)
            _persist_submission_log(order, mode)
        return response

    return response
