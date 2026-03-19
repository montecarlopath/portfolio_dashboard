from __future__ import annotations

"""
order_monitor_loop.py

Polls open hedge orders and updates their lifecycle state.

Design:
  - This is NOT a background thread. It is a pure function you call on a
    schedule (e.g. every 5 minutes via a cron, or triggered manually via
    the API endpoint  GET /hedge/orders/monitor).
  - It only runs when there are open orders in the submission store.
    If there are none, it returns immediately with zero API calls made.
  - On each run it fetches each open order's current status from Alpaca,
    updates the store, and returns a structured summary.

Typical caller:
    result = run_order_monitor()
    if result.newly_filled:
        trigger_post_fill_reconciliation(result.newly_filled)
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from app.services.broker_order_status_engine import get_broker_order_status
from app.services.broker_submission_store import (
    list_open_orders,
    update_order_lifecycle,
    touch_last_checked,
)

logger = logging.getLogger(__name__)


# ── Config ────────────────────────────────────────────────────────────────────

# How old an open order must be before the reprice engine is asked to act.
STALE_THRESHOLD_MINUTES: int = 30

# Alpaca status strings that map to our lifecycle states.
_ALPACA_FILL_STATES = {"filled", "partially_filled"}
_ALPACA_CANCEL_STATES = {"canceled", "cancelled", "done_for_day"}
_ALPACA_EXPIRED_STATES = {"expired"}
_ALPACA_FAILED_STATES = {"rejected", "held", "suspended", "pending_cancel"}


# ── Result types (plain dicts — no Pydantic dependency here) ──────────────────

class OrderCheckResult:
    """Result for a single order check."""
    __slots__ = (
        "client_order_id", "broker_order_id", "previous_state",
        "new_state", "changed", "is_stale", "fill_price",
        "filled_qty", "actual_debit_dollars", "raw_status", "error",
    )

    def __init__(
        self,
        client_order_id: str,
        broker_order_id: Optional[str],
        previous_state: str,
        new_state: str,
        changed: bool,
        is_stale: bool = False,
        fill_price: Optional[float] = None,
        filled_qty: Optional[float] = None,
        actual_debit_dollars: Optional[float] = None,
        raw_status: Optional[Dict[str, Any]] = None,
        error: Optional[str] = None,
    ):
        self.client_order_id = client_order_id
        self.broker_order_id = broker_order_id
        self.previous_state = previous_state
        self.new_state = new_state
        self.changed = changed
        self.is_stale = is_stale
        self.fill_price = fill_price
        self.filled_qty = filled_qty
        self.actual_debit_dollars = actual_debit_dollars
        self.raw_status = raw_status or {}
        self.error = error

    def to_dict(self) -> Dict[str, Any]:
        return {
            "client_order_id": self.client_order_id,
            "broker_order_id": self.broker_order_id,
            "previous_state": self.previous_state,
            "new_state": self.new_state,
            "changed": self.changed,
            "is_stale": self.is_stale,
            "fill_price": self.fill_price,
            "filled_qty": self.filled_qty,
            "actual_debit_dollars": self.actual_debit_dollars,
            "error": self.error,
        }


class MonitorRunResult:
    """Aggregated result for one full monitor run."""

    def __init__(self, results: List[OrderCheckResult]):
        self.results = results
        self.newly_filled: List[OrderCheckResult] = [
            r for r in results if r.new_state == "filled" and r.changed
        ]
        self.newly_cancelled: List[OrderCheckResult] = [
            r for r in results if r.new_state in ("cancelled", "expired") and r.changed
        ]
        self.stale_orders: List[OrderCheckResult] = [
            r for r in results if r.is_stale and r.new_state in ("submitted", "open")
        ]
        self.errors: List[OrderCheckResult] = [
            r for r in results if r.error
        ]

    @property
    def has_fills(self) -> bool:
        return len(self.newly_filled) > 0

    @property
    def has_stale(self) -> bool:
        return len(self.stale_orders) > 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "orders_checked": len(self.results),
            "newly_filled": [r.to_dict() for r in self.newly_filled],
            "newly_cancelled": [r.to_dict() for r in self.newly_cancelled],
            "stale_orders": [r.to_dict() for r in self.stale_orders],
            "errors": [r.to_dict() for r in self.errors],
            "action_needed": self.has_stale or len(self.errors) > 0,
        }


# ── Core logic ─────────────────────────────────────────────────────────────────

def _map_alpaca_status(alpaca_status: str) -> Optional[str]:
    """
    Map an Alpaca order status string to our lifecycle state.
    Returns None if the status is still 'open' / unresolved.
    """
    s = (alpaca_status or "").lower()
    if s in _ALPACA_FILL_STATES:
        return "filled"
    if s in _ALPACA_CANCEL_STATES:
        return "cancelled"
    if s in _ALPACA_EXPIRED_STATES:
        return "expired"
    if s in _ALPACA_FAILED_STATES:
        return "failed"
    # "new", "accepted", "pending_new", "accepted_for_bidding" → still open
    return None


def _is_stale(submitted_at_utc: Optional[str], threshold_minutes: int) -> bool:
    if not submitted_at_utc:
        return False
    try:
        submitted = datetime.fromisoformat(submitted_at_utc)
        if submitted.tzinfo is None:
            submitted = submitted.replace(tzinfo=timezone.utc)
        age = datetime.now(timezone.utc) - submitted
        return age > timedelta(minutes=threshold_minutes)
    except Exception:
        return False


def _check_one_order(stored_row: Dict[str, Any]) -> OrderCheckResult:
    """
    Fetch live status for a single stored order and return what changed.
    """
    client_order_id: str = stored_row.get("client_order_id", "")
    broker_order_id: Optional[str] = stored_row.get("broker_order_id")
    previous_state: str = stored_row.get("lifecycle_state", "submitted")

    try:
        status_resp = get_broker_order_status(
            broker="alpaca",
            broker_order_id=broker_order_id if broker_order_id else None,
            client_order_id=client_order_id if not broker_order_id else None,
            open_only=False,
            hedge_only=False,   # we already know it's a hedge order
        )
    except Exception as exc:
        logger.warning("Monitor: failed to fetch status for %s: %s", client_order_id, exc)
        return OrderCheckResult(
            client_order_id=client_order_id,
            broker_order_id=broker_order_id,
            previous_state=previous_state,
            new_state=previous_state,
            changed=False,
            error=str(exc),
        )

    if not status_resp.orders:
        touch_last_checked(client_order_id)
        return OrderCheckResult(
            client_order_id=client_order_id,
            broker_order_id=broker_order_id,
            previous_state=previous_state,
            new_state=previous_state,
            changed=False,
            error="No order returned by Alpaca status endpoint.",
        )

    order_row = status_resp.orders[0]
    alpaca_status = order_row.status or ""
    new_lifecycle = _map_alpaca_status(alpaca_status)

    stale = _is_stale(stored_row.get("submitted_at_utc"), STALE_THRESHOLD_MINUTES)

    # If still open, just touch the timestamp and flag staleness.
    if new_lifecycle is None:
        touch_last_checked(client_order_id)
        return OrderCheckResult(
            client_order_id=client_order_id,
            broker_order_id=broker_order_id or order_row.broker_order_id,
            previous_state=previous_state,
            new_state="open",   # confirm it is open
            changed=(previous_state == "submitted"),  # first confirmation
            is_stale=stale,
        )

    # Terminal state reached — compute fill details if filled.
    fill_price: Optional[float] = None
    filled_qty: Optional[float] = None
    actual_debit: Optional[float] = None

    if new_lifecycle == "filled":
        fill_price = order_row.avg_fill_price
        filled_qty = order_row.filled_qty
        qty = stored_row.get("qty") or (filled_qty or 0)
        if fill_price and qty:
            actual_debit = round(float(fill_price) * float(qty) * 100, 2)

    update_order_lifecycle(
        client_order_id=client_order_id,
        new_state=new_lifecycle,
        avg_fill_price=fill_price,
        filled_qty=filled_qty,
        filled_at_utc=order_row.filled_at,
        actual_debit_dollars=actual_debit,
    )

    logger.info(
        "Monitor: %s  %s → %s  fill_price=%s  actual_debit=%s",
        client_order_id, previous_state, new_lifecycle, fill_price, actual_debit,
    )

    return OrderCheckResult(
        client_order_id=client_order_id,
        broker_order_id=broker_order_id or order_row.broker_order_id,
        previous_state=previous_state,
        new_state=new_lifecycle,
        changed=True,
        is_stale=stale,
        fill_price=fill_price,
        filled_qty=filled_qty,
        actual_debit_dollars=actual_debit,
        raw_status=order_row.raw_status,
    )


# ── Public entry point ────────────────────────────────────────────────────────

def run_order_monitor(
    stale_threshold_minutes: int = STALE_THRESHOLD_MINUTES,
) -> MonitorRunResult:
    """
    Check all open hedge orders against Alpaca and update the store.

    Returns a MonitorRunResult. The caller is responsible for acting on:
      - result.newly_filled  → trigger post-fill reconciliation
      - result.stale_orders  → pass to order_reprice_engine.reprice_stale_orders()

    This function never raises — errors per order are captured in result.errors.
    """
    open_orders = list_open_orders()

    if not open_orders:
        logger.info("Monitor: no open orders to check.")
        return MonitorRunResult([])

    logger.info("Monitor: checking %d open order(s).", len(open_orders))

    results: List[OrderCheckResult] = []
    for row in open_orders:
        result = _check_one_order(row)
        # Override stale threshold if caller passed a different value.
        if stale_threshold_minutes != STALE_THRESHOLD_MINUTES:
            result.is_stale = _is_stale(row.get("submitted_at_utc"), stale_threshold_minutes)
        results.append(result)

    run = MonitorRunResult(results)

    logger.info(
        "Monitor run complete: %d checked, %d filled, %d stale, %d errors",
        len(results),
        len(run.newly_filled),
        len(run.stale_orders),
        len(run.errors),
    )

    return run