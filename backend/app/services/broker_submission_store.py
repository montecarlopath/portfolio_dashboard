from __future__ import annotations

"""
broker_submission_store.py

Persistent flat-file store for hedge order lifecycle tracking.

Each order record moves through these states:
    submitted → open → filled
                     → cancelled
                     → expired
                     → failed

The store is the single source of truth for:
  - what was submitted
  - what actually filled (price, time, actual debit)
  - reprice history
  - remaining gap after fills

File: data/broker_submission_log.json
"""

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional

logger = logging.getLogger(__name__)

STORE_PATH = Path("data/broker_submission_log.json")
STORE_PATH.parent.mkdir(parents=True, exist_ok=True)

# All valid terminal states — once here, no further updates are expected.
TERMINAL_STATES = {"filled", "cancelled", "expired", "failed", "replaced"}

OrderLifecycleState = Literal[
    "submitted",   # accepted by Alpaca, not yet confirmed open
    "open",        # confirmed open / pending fill
    "filled",      # fully filled
    "cancelled",   # cancelled (by us or by Alpaca)
    "expired",     # day order expired without fill
    "failed",      # Alpaca reported an error state
    "replaced",    # this order was cancelled and superseded by a reprice order
]


# ── Internal I/O ──────────────────────────────────────────────────────────────

def _load_store() -> dict:
    if not STORE_PATH.exists():
        return {"orders": []}
    try:
        return json.loads(STORE_PATH.read_text())
    except Exception:
        return {"orders": []}


def _save_store(data: dict) -> None:
    STORE_PATH.write_text(json.dumps(data, indent=2))


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── Write operations ──────────────────────────────────────────────────────────

def log_submission_attempt(
    *,
    client_order_id: str,
    mode: str,
    broker: str,
    broker_environment: str,
    payload: dict,
    status: str,
    broker_order_id: str | None = None,
    message: str = "",
    # optional order sizing context (set on submit, used for actual-vs-estimated later)
    estimated_debit_dollars: float | None = None,
    estimated_coverage_dollars: float | None = None,
    qty: int | None = None,
    underlying: str | None = None,
    ticket_bucket: str | None = None,   # "primary" or "tail"
    ticket_action: str | None = None,
) -> None:
    """
    Record a new order submission attempt.
    Sets lifecycle_state = "submitted" if the order was accepted, else "failed".
    """
    data = _load_store()
    lifecycle_state: OrderLifecycleState = (
        "submitted" if status in ("accepted", "submitted") else "failed"
    )

    data.setdefault("orders", []).append(
        {
            # ── Identity ──────────────────────────────────────
            "client_order_id": client_order_id,
            "broker_order_id": broker_order_id,
            "broker": broker,
            "broker_environment": broker_environment,
            "underlying": underlying,
            "ticket_bucket": ticket_bucket,
            "ticket_action": ticket_action,
            # ── Submission ────────────────────────────────────
            "mode": mode,
            "submitted_at_utc": _utc_now_iso(),
            "submission_status": status,
            "submission_message": message,
            "payload": payload,
            # ── Estimates (at time of submission) ─────────────
            "qty": qty,
            "estimated_debit_dollars": estimated_debit_dollars,
            "estimated_coverage_dollars": estimated_coverage_dollars,
            # ── Lifecycle state ───────────────────────────────
            "lifecycle_state": lifecycle_state,
            "last_checked_utc": _utc_now_iso(),
            # ── Fill fields (populated on fill) ───────────────
            "filled_at_utc": None,
            "avg_fill_price": None,
            "filled_qty": None,
            "actual_debit_dollars": None,
            # ── Reprice tracking ──────────────────────────────
            "reprice_count": 0,
            "reprice_history": [],          # list of {at_utc, old_price, new_price, new_client_order_id}
            "replaced_by_client_order_id": None,
        }
    )
    _save_store(data)


def update_order_lifecycle(
    *,
    client_order_id: str,
    new_state: OrderLifecycleState,
    # fill fields — supply on "filled"
    avg_fill_price: float | None = None,
    filled_qty: float | None = None,
    filled_at_utc: str | None = None,
    # convenience — computed actual debit on fill
    actual_debit_dollars: float | None = None,
    # replacement pointer — supply when marking state = "replaced"
    replaced_by_client_order_id: str | None = None,
) -> bool:
    """
    Update the lifecycle state of a tracked order.
    Returns True if the record was found and updated, False otherwise.
    """
    data = _load_store()
    updated = False

    for row in reversed(data.get("orders", [])):
        if row.get("client_order_id") != client_order_id:
            continue
        if row.get("lifecycle_state") in TERMINAL_STATES:
            # Already terminal — don't overwrite.
            return False

        row["lifecycle_state"] = new_state
        row["last_checked_utc"] = _utc_now_iso()

        if new_state == "filled":
            row["filled_at_utc"] = filled_at_utc or _utc_now_iso()
            if avg_fill_price is not None:
                row["avg_fill_price"] = avg_fill_price
            if filled_qty is not None:
                row["filled_qty"] = filled_qty
            if actual_debit_dollars is not None:
                row["actual_debit_dollars"] = actual_debit_dollars
            elif avg_fill_price is not None and row.get("qty"):
                # compute actual debit = fill_price × qty × 100 (option multiplier)
                row["actual_debit_dollars"] = round(avg_fill_price * row["qty"] * 100, 2)

        if new_state == "replaced" and replaced_by_client_order_id:
            row["replaced_by_client_order_id"] = replaced_by_client_order_id

        updated = True
        break

    if updated:
        _save_store(data)
    else:
        logger.warning("update_order_lifecycle: client_order_id %s not found", client_order_id)

    return updated


def record_reprice(
    *,
    old_client_order_id: str,
    new_client_order_id: str,
    old_limit_price: float,
    new_limit_price: float,
) -> None:
    """
    Append a reprice event to the original order's reprice_history.
    Also increments reprice_count.
    """
    data = _load_store()
    for row in reversed(data.get("orders", [])):
        if row.get("client_order_id") != old_client_order_id:
            continue
        row.setdefault("reprice_history", []).append(
            {
                "repriced_at_utc": _utc_now_iso(),
                "old_limit_price": old_limit_price,
                "new_limit_price": new_limit_price,
                "new_client_order_id": new_client_order_id,
            }
        )
        row["reprice_count"] = len(row["reprice_history"])
        row["replaced_by_client_order_id"] = new_client_order_id
        break
    _save_store(data)


def touch_last_checked(client_order_id: str) -> None:
    """Update last_checked_utc without changing state — used by the monitor loop."""
    data = _load_store()
    for row in reversed(data.get("orders", [])):
        if row.get("client_order_id") == client_order_id:
            row["last_checked_utc"] = _utc_now_iso()
            break
    _save_store(data)


# ── Read operations ───────────────────────────────────────────────────────────

def find_recent_by_client_order_id(client_order_id: str) -> Optional[Dict[str, Any]]:
    data = _load_store()
    for row in reversed(data.get("orders", [])):
        if row.get("client_order_id") == client_order_id:
            return row
    return None


def list_open_orders() -> List[Dict[str, Any]]:
    """
    Return orders that are not yet in a terminal state.
    These are the orders the monitor loop needs to poll.
    """
    data = _load_store()
    return [
        row for row in data.get("orders", [])
        if row.get("lifecycle_state") not in TERMINAL_STATES
        and row.get("mode") == "submit"
    ]


def list_all_orders(
    *,
    limit: int = 200,
    state_filter: Optional[List[OrderLifecycleState]] = None,
) -> List[Dict[str, Any]]:
    """
    Return all tracked orders, newest first.
    Optionally filter to specific lifecycle states.
    """
    data = _load_store()
    rows = list(reversed(data.get("orders", [])))

    if state_filter:
        rows = [r for r in rows if r.get("lifecycle_state") in state_filter]

    return rows[:limit]


def summary_stats() -> Dict[str, Any]:
    """Quick stats for the /hedge/orders/history response header."""
    all_rows = list_all_orders()
    total_actual_debit = sum(
        r.get("actual_debit_dollars") or 0
        for r in all_rows
        if r.get("lifecycle_state") == "filled"
    )
    return {
        "total_orders": len(all_rows),
        "filled": sum(1 for r in all_rows if r.get("lifecycle_state") == "filled"),
        "open": sum(1 for r in all_rows if r.get("lifecycle_state") in ("submitted", "open")),
        "cancelled": sum(1 for r in all_rows if r.get("lifecycle_state") == "cancelled"),
        "expired": sum(1 for r in all_rows if r.get("lifecycle_state") == "expired"),
        "total_actual_debit_dollars": round(total_actual_debit, 2),
    }