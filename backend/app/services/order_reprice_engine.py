from __future__ import annotations

"""
order_reprice_engine.py

Handles stale order repricing: cancel → widen limit price → resubmit.

Design decisions:
  - Maximum 3 reprice attempts per order. After that, we abandon and log.
    (Beyond 3 attempts the market has moved too far for the original spread.)
  - Each reprice widens the limit price by REPRICE_BUFFER_PCT (default 5%).
    For a debit spread this means we're willing to pay a bit more to get filled.
  - The new order gets a fresh client_order_id (which includes a "-r{n}" suffix
    so it's traceable back to the original).
  - The original order is marked as "replaced" in the store, pointing to the
    new client_order_id.

Usage (called from the monitor loop or the API route):
    from app.services.order_reprice_engine import reprice_stale_orders
    results = reprice_stale_orders(stale_order_results)
"""

import hashlib
import logging
import os
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import requests

from app.config import load_alpaca_base_url, load_alpaca_key, load_alpaca_secret
from app.services.broker_cancel_engine import cancel_broker_order
from app.services.broker_submission_store import (
    find_recent_by_client_order_id,
    log_submission_attempt,
    record_reprice,
    update_order_lifecycle,
)

logger = logging.getLogger(__name__)


# ── Config ─────────────────────────────────────────────────────────────────────

REPRICE_BUFFER_PCT: float = 0.05     # widen limit price by 5% per reprice
MAX_REPRICE_ATTEMPTS: int = 3        # abandon after this many reprices
CANCEL_SETTLE_SECONDS: float = 2.0  # brief wait after cancel before resubmit


# ── Helpers ───────────────────────────────────────────────────────────────────

def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _alpaca_base_url() -> str:
    env = os.getenv("ALPACA_ENV", "paper").strip().lower()
    configured = (load_alpaca_base_url() or "").strip()
    if configured:
        return configured.rstrip("/")
    return (
        "https://paper-api.alpaca.markets/v2"
        if env == "paper"
        else "https://api.alpaca.markets/v2"
    )


def _alpaca_headers() -> Dict[str, str]:
    key = os.getenv("ALPACA_API_KEY") or load_alpaca_key()
    secret = os.getenv("ALPACA_API_SECRET") or load_alpaca_secret()
    if not key or not secret:
        raise RuntimeError("Missing Alpaca credentials.")
    return {
        "accept": "application/json",
        "content-type": "application/json",
        "Apca-Api-Key-Id": key,
        "Apca-Api-Secret-Key": secret,
    }


def _new_client_order_id(original_id: str, reprice_number: int) -> str:
    """
    Derive a fresh, deterministic client_order_id from the original.
    Format: hedge-{hash}-r{n}
    Keeps it traceable while being unique to Alpaca.
    """
    seed = f"{original_id}-reprice-{reprice_number}"
    h = hashlib.sha256(seed.encode()).hexdigest()[:12]
    return f"hedge-{h}-r{reprice_number}"


def _round_limit(price: float) -> float:
    return round(price, 2)


# ── Result type ───────────────────────────────────────────────────────────────

class RepriceResult:
    __slots__ = (
        "original_client_order_id", "new_client_order_id",
        "old_limit_price", "new_limit_price", "reprice_number",
        "submitted", "broker_order_id", "status", "message",
    )

    def __init__(
        self,
        original_client_order_id: str,
        new_client_order_id: Optional[str],
        old_limit_price: float,
        new_limit_price: float,
        reprice_number: int,
        submitted: bool,
        broker_order_id: Optional[str],
        status: str,
        message: str,
    ):
        self.original_client_order_id = original_client_order_id
        self.new_client_order_id = new_client_order_id
        self.old_limit_price = old_limit_price
        self.new_limit_price = new_limit_price
        self.reprice_number = reprice_number
        self.submitted = submitted
        self.broker_order_id = broker_order_id
        self.status = status
        self.message = message

    def to_dict(self) -> Dict[str, Any]:
        return {
            "original_client_order_id": self.original_client_order_id,
            "new_client_order_id": self.new_client_order_id,
            "old_limit_price": self.old_limit_price,
            "new_limit_price": self.new_limit_price,
            "reprice_number": self.reprice_number,
            "submitted": self.submitted,
            "broker_order_id": self.broker_order_id,
            "status": self.status,
            "message": self.message,
        }


# ── Core reprice logic ────────────────────────────────────────────────────────

def reprice_one_order(client_order_id: str) -> RepriceResult:
    """
    Reprice a single stale order:
      1. Look up the stored order to get the original payload and limit price.
      2. Check reprice_count — abandon if already at MAX_REPRICE_ATTEMPTS.
      3. Cancel the open order on Alpaca.
      4. Build a new payload with the widened limit price and a new client_order_id.
      5. Submit the new order to Alpaca.
      6. Update the store: old order → "replaced", new order → "submitted".
    """
    stored = find_recent_by_client_order_id(client_order_id)
    if not stored:
        return RepriceResult(
            original_client_order_id=client_order_id,
            new_client_order_id=None,
            old_limit_price=0.0, new_limit_price=0.0, reprice_number=0,
            submitted=False, broker_order_id=None,
            status="not_found", message="Order not found in submission store.",
        )

    reprice_count: int = stored.get("reprice_count", 0)
    if reprice_count >= MAX_REPRICE_ATTEMPTS:
        logger.warning("Reprice: %s already repriced %d times — abandoning.", client_order_id, reprice_count)
        return RepriceResult(
            original_client_order_id=client_order_id,
            new_client_order_id=None,
            old_limit_price=0.0, new_limit_price=0.0,
            reprice_number=reprice_count,
            submitted=False, broker_order_id=None,
            status="max_reprices_reached",
            message=f"Already repriced {reprice_count} times — order abandoned.",
        )

    # ── Step 1: extract the original Alpaca payload ──────────────────────────
    original_payload: Dict[str, Any] = stored.get("payload", {})
    old_limit_price_str = original_payload.get("limit_price", "0")
    try:
        old_limit_price = float(old_limit_price_str)
    except (ValueError, TypeError):
        old_limit_price = 0.0

    if old_limit_price <= 0:
        return RepriceResult(
            original_client_order_id=client_order_id,
            new_client_order_id=None,
            old_limit_price=0.0, new_limit_price=0.0,
            reprice_number=reprice_count,
            submitted=False, broker_order_id=None,
            status="invalid_limit_price",
            message="Cannot reprice: original limit_price is missing or zero.",
        )

    # ── Step 2: compute new limit price ──────────────────────────────────────
    new_limit_price = _round_limit(old_limit_price * (1 + REPRICE_BUFFER_PCT))
    reprice_number = reprice_count + 1
    new_client_order_id = _new_client_order_id(client_order_id, reprice_number)

    # ── Step 3: cancel the existing open order ────────────────────────────────
    broker_order_id: Optional[str] = stored.get("broker_order_id")
    cancel_result = cancel_broker_order(
        broker="alpaca",
        broker_order_id=broker_order_id,
        client_order_id=client_order_id if not broker_order_id else None,
    )

    if not cancel_result.canceled:
        # Order may have already filled or expired — check before giving up.
        logger.warning(
            "Reprice: cancel failed for %s: %s", client_order_id, cancel_result.message
        )
        return RepriceResult(
            original_client_order_id=client_order_id,
            new_client_order_id=None,
            old_limit_price=old_limit_price, new_limit_price=new_limit_price,
            reprice_number=reprice_number,
            submitted=False, broker_order_id=broker_order_id,
            status="cancel_failed",
            message=f"Cancel rejected by Alpaca: {cancel_result.message}",
        )

    logger.info("Reprice: cancelled %s — waiting %.1fs before resubmit.", client_order_id, CANCEL_SETTLE_SECONDS)
    time.sleep(CANCEL_SETTLE_SECONDS)

    # ── Step 4: build new payload ─────────────────────────────────────────────
    new_payload = dict(original_payload)
    new_payload["limit_price"] = str(new_limit_price)
    new_payload["client_order_id"] = new_client_order_id

    # ── Step 5: submit to Alpaca ──────────────────────────────────────────────
    try:
        resp = requests.post(
            f"{_alpaca_base_url()}/orders",
            headers=_alpaca_headers(),
            json=new_payload,
            timeout=20,
        )
    except Exception as exc:
        logger.error("Reprice: submit request failed for %s: %s", new_client_order_id, exc)
        return RepriceResult(
            original_client_order_id=client_order_id,
            new_client_order_id=new_client_order_id,
            old_limit_price=old_limit_price, new_limit_price=new_limit_price,
            reprice_number=reprice_number,
            submitted=False, broker_order_id=None,
            status="submit_request_failed",
            message=str(exc),
        )

    if resp.status_code >= 400:
        logger.error("Reprice: Alpaca rejected resubmit for %s: %s %s", new_client_order_id, resp.status_code, resp.text)
        return RepriceResult(
            original_client_order_id=client_order_id,
            new_client_order_id=new_client_order_id,
            old_limit_price=old_limit_price, new_limit_price=new_limit_price,
            reprice_number=reprice_number,
            submitted=False, broker_order_id=None,
            status="submit_rejected",
            message=f"Alpaca rejected: {resp.status_code} {resp.text[:200]}",
        )

    try:
        resp_payload = resp.json()
    except Exception:
        resp_payload = {}

    new_broker_order_id: Optional[str] = resp_payload.get("id")

    # ── Step 6: update the store ──────────────────────────────────────────────
    # Mark old order as replaced.
    update_order_lifecycle(
        client_order_id=client_order_id,
        new_state="replaced",
        replaced_by_client_order_id=new_client_order_id,
    )
    record_reprice(
        old_client_order_id=client_order_id,
        new_client_order_id=new_client_order_id,
        old_limit_price=old_limit_price,
        new_limit_price=new_limit_price,
    )

    # Log new order as a fresh submission.
    log_submission_attempt(
        client_order_id=new_client_order_id,
        mode="submit",
        broker=stored.get("broker", "alpaca"),
        broker_environment=stored.get("broker_environment", "paper"),
        payload=new_payload,
        status="submitted",
        broker_order_id=new_broker_order_id,
        message=f"Reprice #{reprice_number} of {client_order_id}. "
                f"Limit {old_limit_price} → {new_limit_price}.",
        estimated_debit_dollars=stored.get("estimated_debit_dollars"),
        estimated_coverage_dollars=stored.get("estimated_coverage_dollars"),
        qty=stored.get("qty"),
        underlying=stored.get("underlying"),
        ticket_bucket=stored.get("ticket_bucket"),
        ticket_action=stored.get("ticket_action"),
    )

    logger.info(
        "Reprice: %s → %s  limit %.2f → %.2f  broker_id=%s",
        client_order_id, new_client_order_id,
        old_limit_price, new_limit_price, new_broker_order_id,
    )

    return RepriceResult(
        original_client_order_id=client_order_id,
        new_client_order_id=new_client_order_id,
        old_limit_price=old_limit_price,
        new_limit_price=new_limit_price,
        reprice_number=reprice_number,
        submitted=True,
        broker_order_id=new_broker_order_id,
        status="repriced",
        message=f"Order repriced and resubmitted. Limit {old_limit_price} → {new_limit_price}.",
    )


def reprice_stale_orders(
    stale_orders,  # List[OrderCheckResult] from monitor loop
) -> List[RepriceResult]:
    """
    Reprice all stale orders identified by the monitor loop.
    Accepts the stale_orders list from MonitorRunResult.stale_orders.
    Returns a list of RepriceResult, one per order attempted.
    """
    results: List[RepriceResult] = []
    for order in stale_orders:
        logger.info("Reprice: processing stale order %s", order.client_order_id)
        result = reprice_one_order(order.client_order_id)
        results.append(result)
    return results