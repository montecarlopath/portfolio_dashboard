from __future__ import annotations

"""
hedge_close_engine.py

Handles closing existing hedge positions when exit triggers fire.

Design:
  - Alpaca positions: auto-execute via sell_to_close limit order at mid price
  - Composer positions: generate alert only — user closes manually

Called by: EOD engine (same 3:15-3:45 PM window as new spread submissions)
Triggered by: close_profit_take / close_regime_exit / close_decay tickets
              from hedge_trade_ticket_engine

Close pricing:
  - Limit = mid price (bid + ask) / 2, rounded to $0.01
  - If unfilled by 3:45 PM final attempt: drop to bid price
  - Same 4-attempt retry logic as new spread submissions

Order structure:
  - Naked put (single leg): sell_to_close, single-leg order
  - Put spread (two legs): mleg order, sell long + buy short
"""

import logging
from datetime import datetime, timezone
from typing import Optional

from app.services.option_chain_read import get_option_snapshots_alpaca
from app.services.broker_submission_store import (
    log_submission_attempt,
    list_open_orders,
)
from app.services.eod_hedge_engine import add_eod_alert
from app.config import load_alpaca_key, load_alpaca_secret, load_alpaca_base_url
import os
import requests

logger = logging.getLogger(__name__)

# ── Config ─────────────────────────────────────────────────────────────────────
# Bid-ask width threshold for close orders (more lenient than open — you want out)
CLOSE_WIDTH_THRESHOLD = 0.25   # 25% — wider than open threshold since exiting

COMPOSER_SYMBOLS = {
    # Symbols held in Composer symphonies — cannot be auto-closed via Alpaca
    # Update this set when Composer positions change
    "QQQ260515P00550000",
    "QQQ260618P00550000",
}


# ── Alpaca helpers ─────────────────────────────────────────────────────────────

def _alpaca_base_url() -> str:
    env = os.getenv("ALPACA_ENV", "paper").strip().lower()
    configured = (load_alpaca_base_url() or "").strip()
    if configured:
        return configured.rstrip("/")
    return "https://paper-api.alpaca.markets/v2" if env == "paper" else "https://api.alpaca.markets/v2"


def _alpaca_headers() -> dict:
    key = os.getenv("ALPACA_API_KEY") or load_alpaca_key()
    secret = os.getenv("ALPACA_API_SECRET") or load_alpaca_secret()
    if not key or not secret:
        raise RuntimeError("Missing Alpaca credentials")
    return {
        "accept": "application/json",
        "content-type": "application/json",
        "Apca-Api-Key-Id": key,
        "Apca-Api-Secret-Key": secret,
    }


# ── Pricing ────────────────────────────────────────────────────────────────────

def _compute_close_limit(bid: Optional[float], ask: Optional[float], use_bid: bool = False) -> Optional[float]:
    """
    Compute limit price for a close (sell) order.
    Mid price = (bid + ask) / 2 for normal attempts.
    Bid price for final attempt (guaranteed fill).
    """
    if bid is None or ask is None:
        return None
    if use_bid:
        price = bid
    else:
        price = (bid + ask) / 2.0
    return round(max(price, 0.01), 2)


def _width_acceptable(bid: Optional[float], ask: Optional[float]) -> tuple[bool, float]:
    if bid is None or ask is None:
        return False, 1.0
    mid = (bid + ask) / 2.0
    if mid <= 0:
        return False, 1.0
    width_pct = (ask - bid) / mid
    return width_pct <= CLOSE_WIDTH_THRESHOLD, width_pct


# ── Single-leg close order ─────────────────────────────────────────────────────

def _submit_single_leg_close(
    *,
    symbol: str,
    qty: int,
    limit_price: float,
    client_order_id: str,
    mode: str = "submit",
) -> dict:
    """
    Submit a sell_to_close order for a single naked put.
    """
    payload = {
        "symbol": symbol,
        "qty": str(qty),
        "side": "sell",
        "type": "limit",
        "limit_price": str(round(limit_price, 2)),
        "time_in_force": "day",
        "client_order_id": client_order_id,
        "position_intent": "sell_to_close",
    }

    if mode != "submit":
        return {
            "submitted": False,
            "mode": mode,
            "payload": payload,
            "message": f"{mode} mode — not submitted",
        }

    try:
        resp = requests.post(
            f"{_alpaca_base_url()}/orders",
            headers=_alpaca_headers(),
            json=payload,
            timeout=20,
        )
        if resp.status_code in (200, 201):
            data = resp.json()
            return {
                "submitted": True,
                "broker_order_id": data.get("id"),
                "client_order_id": client_order_id,
                "status": data.get("status", "pending_new"),
                "message": "Close order submitted to Alpaca.",
            }
        else:
            return {
                "submitted": False,
                "message": f"Alpaca rejected: {resp.status_code} {resp.text[:200]}",
            }
    except Exception as e:
        return {"submitted": False, "message": str(e)}


# ── Multi-leg close order (spread unwind) ─────────────────────────────────────

def _submit_spread_close(
    *,
    long_symbol: str,    # the put we're long — sell to close
    short_symbol: str,   # the put we're short — buy to close
    qty: int,
    limit_price: float,
    client_order_id: str,
    mode: str = "submit",
) -> dict:
    """
    Submit a multi-leg order to unwind a put spread.
    Sell the long leg, buy back the short leg.
    Net credit should be the current spread value.
    """
    payload = {
        "order_class": "mleg",
        "qty": str(qty),
        "type": "limit",
        "limit_price": str(round(limit_price, 2)),
        "time_in_force": "day",
        "client_order_id": client_order_id,
        "legs": [
            {
                "symbol": long_symbol,
                "ratio_qty": "1",
                "side": "sell",
                "position_intent": "sell_to_close",
            },
            {
                "symbol": short_symbol,
                "ratio_qty": "1",
                "side": "buy",
                "position_intent": "buy_to_close",
            },
        ],
    }

    if mode != "submit":
        return {
            "submitted": False,
            "mode": mode,
            "payload": payload,
            "message": f"{mode} mode — not submitted",
        }

    try:
        resp = requests.post(
            f"{_alpaca_base_url()}/orders",
            headers=_alpaca_headers(),
            json=payload,
            timeout=20,
        )
        if resp.status_code in (200, 201):
            data = resp.json()
            return {
                "submitted": True,
                "broker_order_id": data.get("id"),
                "client_order_id": client_order_id,
                "status": data.get("status", "pending_new"),
                "message": "Spread close order submitted to Alpaca.",
            }
        else:
            return {
                "submitted": False,
                "message": f"Alpaca rejected: {resp.status_code} {resp.text[:200]}",
            }
    except Exception as e:
        return {"submitted": False, "message": str(e)}


# ── Main entry point ───────────────────────────────────────────────────────────

def execute_close_tickets(
    *,
    tickets: list,
    mode: str = "preview",
    use_bid: bool = False,
) -> list[dict]:
    """
    Execute close tickets generated by hedge_trade_ticket_engine.

    Args:
        tickets: List of HedgeTradeTicket with action in
                 (close_profit_take, close_regime_exit, close_decay)
        mode: "preview" | "dry_run" | "submit"
        use_bid: True on final EOD attempt (3:45 PM) — use bid not mid

    Returns:
        List of result dicts per ticket.
    """
    close_actions = {"close_profit_take", "close_regime_exit", "close_decay"}
    close_tickets = [t for t in tickets if getattr(t, "action", "") in close_actions]

    if not close_tickets:
        return []

    results = []

    for ticket in close_tickets:
        symbol = getattr(ticket, "long_leg_symbol", None)
        short_symbol = getattr(ticket, "short_leg_symbol", None)
        contracts = int(getattr(ticket, "contracts", 0) or 0)
        action = getattr(ticket, "action", "")
        description = getattr(ticket, "description", "")

        if not symbol or contracts <= 0:
            results.append({"symbol": symbol, "submitted": False, "message": "Missing symbol or contracts"})
            continue

        # ── Composer position — alert only ────────────────────────────────────
        if symbol in COMPOSER_SYMBOLS:
            msg = (
                f"EXIT ALERT — Composer position: {symbol} ×{contracts}. "
                f"Trigger: {action}. {description}. "
                f"Close manually in Composer symphony."
            )
            add_eod_alert(
                alert_type="no_fill",   # reuse alert type for now
                bucket="primary",
                message=msg,
                width_pct=None,
            )
            logger.info("CLOSE ALERT (Composer): %s", msg)
            results.append({
                "symbol": symbol,
                "source": "composer",
                "submitted": False,
                "action": action,
                "message": "Composer position — alert generated, manual close required",
            })
            continue

        # ── Alpaca position — get live bid/ask ────────────────────────────────
        snap_symbols = [symbol]
        if short_symbol:
            snap_symbols.append(short_symbol)

        snapshots = get_option_snapshots_alpaca(snap_symbols)
        long_snap = snapshots.get(symbol, {}) or {}
        bid = long_snap.get("bid")
        ask = long_snap.get("ask")

        # Width check
        ok, width_pct = _width_acceptable(bid, ask)
        if not ok and not use_bid:
            logger.warning(
                "CLOSE: %s bid-ask too wide (%.1f%%) — skipping",
                symbol, width_pct * 100,
            )
            results.append({
                "symbol": symbol,
                "submitted": False,
                "action": action,
                "width_pct": width_pct,
                "message": f"Bid-ask too wide ({width_pct:.1%}) for close — skipping",
            })
            continue

        limit_price = _compute_close_limit(bid, ask, use_bid=use_bid)
        if not limit_price:
            results.append({"symbol": symbol, "submitted": False, "message": "Could not compute limit price"})
            continue

        # Generate unique client order ID for this close
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        client_order_id = f"close-{symbol[:12].lower()}-{ts}"

        # ── Execute close ─────────────────────────────────────────────────────
        if short_symbol:
            # Spread unwind
            result = _submit_spread_close(
                long_symbol=symbol,
                short_symbol=short_symbol,
                qty=contracts,
                limit_price=limit_price,
                client_order_id=client_order_id,
                mode=mode,
            )
        else:
            # Single leg (naked put)
            result = _submit_single_leg_close(
                symbol=symbol,
                qty=contracts,
                limit_price=limit_price,
                client_order_id=client_order_id,
                mode=mode,
            )

        result.update({
            "symbol": symbol,
            "action": action,
            "contracts": contracts,
            "limit_price": limit_price,
            "bid": bid,
            "ask": ask,
            "source": "alpaca",
            "description": description,
        })

        if result.get("submitted"):
            # Log to submission store so monitor can track
            log_submission_attempt(
                client_order_id=client_order_id,
                broker_order_id=result.get("broker_order_id"),
                broker="alpaca",
                underlying="QQQ",
                ticket_bucket="primary",
                ticket_action=action,
                qty=contracts,
                estimated_debit_dollars=0.0,  # close = credit, not debit
                payload=result.get("payload", {}),
                submission_result=result,
            )
            logger.info(
                "CLOSE submitted: %s ×%d @ $%.2f (%s) broker_id=%s",
                symbol, contracts, limit_price, action,
                result.get("broker_order_id", "—"),
            )
        else:
            logger.warning(
                "CLOSE failed: %s ×%d — %s",
                symbol, contracts, result.get("message", "unknown"),
            )

        results.append(result)

    return results