from __future__ import annotations

"""
eod_hedge_engine.py

End-of-day hedge submission engine.

Schedule (all ET, weekdays only):
  3:00 PM  → monitor: check fills, update store
  3:15 PM  → attempt 1: check spread width, submit at mid if acceptable
  3:25 PM  → attempt 2: cancel stale + resubmit at mid
  3:35 PM  → attempt 3: cancel stale + resubmit at mid
  3:45 PM  → attempt 4 (final): cancel stale + resubmit at ask if width acceptable
  4:30 PM  → write daily hedge snapshot

Bid-ask width thresholds (% of net debit mid):
  Primary spread: reject if width > 15%  (submit at ask only if ≤ 15%)
  Tail spread:    reject if width > 40%  (submit at ask only if ≤ 40%)

Aggression:
  Attempts 1-3: limit = mid price (no buffer)
  Attempt 4:    limit = ask price (bucket-specific slippage buffer applied)
                Primary: +5% above mid
                Tail:    +12% above mid
"""

import logging
import os
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import requests

from app.config import load_alpaca_key, load_alpaca_secret
from app.market_hours import now_et

logger = logging.getLogger(__name__)

# ── Thresholds ────────────────────────────────────────────────────────────────
# Max bid-ask width as % of net debit mid before we skip this attempt
MAX_SPREAD_WIDTH_PCT = {
    "primary": 0.15,   # 15% — primary is deep liquid QQQ near-ATM
    "tail":    0.40,   # 40% — tail is OTM, wider markets acceptable
}

# Slippage buffer applied only on the final ask-price attempt
FINAL_ATTEMPT_SLIPPAGE = {
    "primary": 0.05,   # 5% above mid → pays up to ask + small buffer
    "tail":    0.12,   # 12% above mid → more aggressive for OTM fills
}

# ── Alpaca helpers ────────────────────────────────────────────────────────────
_ALPACA_DATA_URL = "https://data.alpaca.markets"


def _alpaca_headers() -> Dict[str, str]:
    api_key = os.getenv("ALPACA_API_KEY") or load_alpaca_key()
    api_secret = os.getenv("ALPACA_API_SECRET") or load_alpaca_secret()
    if not api_key or not api_secret:
        raise RuntimeError("Missing Alpaca credentials.")
    return {
        "accept": "application/json",
        "Apca-Api-Key-Id": api_key,
        "Apca-Api-Secret-Key": api_secret,
    }


def _safe_float(v, default: float = 0.0) -> float:
    try:
        return float(v) if v is not None else default
    except Exception:
        return default


# ── Bid-ask spread width check ────────────────────────────────────────────────

def compute_net_spread_width(
    long_bid: Optional[float],
    long_ask: Optional[float],
    short_bid: Optional[float],
    short_ask: Optional[float],
) -> Tuple[Optional[float], Optional[float], Optional[float]]:
    """
    Compute the net bid, ask, and mid for a debit spread.

    For a debit spread (buy long put, sell short put):
      net_bid = long_bid - short_ask   (worst case: sell short at bid, buy long at bid)
      net_ask = long_ask - short_bid   (worst case: buy long at ask, sell short at bid)
      net_mid = (net_bid + net_ask) / 2

    Returns (net_bid, net_ask, net_mid). Any may be None if inputs missing.
    """
    if any(x is None for x in [long_bid, long_ask, short_bid, short_ask]):
        return None, None, None

    lb = _safe_float(long_bid)
    la = _safe_float(long_ask)
    sb = _safe_float(short_bid)
    sa = _safe_float(short_ask)

    if la <= 0 or la < lb:
        return None, None, None

    net_bid = max(lb - sa, 0.0)
    net_ask = max(la - sb, 0.0)

    if net_ask <= 0:
        return None, None, None

    net_mid = (net_bid + net_ask) / 2.0
    return net_bid, net_ask, net_mid


def check_spread_width_acceptable(
    bucket: str,
    long_bid: Optional[float],
    long_ask: Optional[float],
    short_bid: Optional[float],
    short_ask: Optional[float],
) -> Tuple[bool, float, str]:
    """
    Returns (is_acceptable, width_pct, reason_string).

    width_pct = (net_ask - net_bid) / net_mid
    is_acceptable = width_pct <= threshold for bucket
    """
    bucket_key = "primary" if "primary" in (bucket or "").lower() else "tail"
    threshold = MAX_SPREAD_WIDTH_PCT[bucket_key]

    net_bid, net_ask, net_mid = compute_net_spread_width(
        long_bid, long_ask, short_bid, short_ask
    )

    if net_bid is None or net_mid is None or net_mid <= 0:
        return False, 1.0, "Could not compute net spread — missing bid/ask data"

    raw_width = net_ask - net_bid
    width_pct = raw_width / net_mid

    ok = width_pct <= threshold
    reason = (
        f"bid-ask width {width_pct:.1%} {'≤' if ok else '>'} "
        f"threshold {threshold:.1%} for {bucket_key} spread "
        f"(net_bid={net_bid:.2f} net_ask={net_ask:.2f} mid={net_mid:.2f})"
    )
    return ok, width_pct, reason


# ── Limit price calculation ───────────────────────────────────────────────────

def compute_limit_price(
    bucket: str,
    long_bid: Optional[float],
    long_ask: Optional[float],
    short_bid: Optional[float],
    short_ask: Optional[float],
    use_ask: bool = False,
) -> Optional[float]:
    """
    Compute the limit price for a spread order.

    use_ask=False (attempts 1-3): mid price
    use_ask=True  (attempt 4):    mid + slippage buffer (approaches ask)

    Returns None if bid/ask data is insufficient.
    """
    net_bid, net_ask, net_mid = compute_net_spread_width(
        long_bid, long_ask, short_bid, short_ask
    )

    if net_mid is None or net_mid <= 0:
        return None

    if not use_ask:
        return round(net_mid, 2)

    # Final attempt: apply bucket-specific slippage above mid
    bucket_key = "primary" if "primary" in (bucket or "").lower() else "tail"
    slippage = FINAL_ATTEMPT_SLIPPAGE[bucket_key]
    limit = net_mid * (1.0 + slippage)

    # Cap at ask price — never pay more than ask
    if net_ask and net_ask > 0:
        limit = min(limit, net_ask)

    return round(limit, 2)


# ── EOD attempt logic ─────────────────────────────────────────────────────────

def get_eod_attempt_number() -> int:
    """
    Return which EOD attempt this is based on current ET time.

    3:15 → attempt 1 (mid)
    3:25 → attempt 2 (mid)
    3:35 → attempt 3 (mid)
    3:45 → attempt 4 / final (ask if width OK)
    """
    now = now_et()
    minute = now.hour * 60 + now.minute  # minutes since midnight ET

    # 3:15 = 915, 3:25 = 925, 3:35 = 935, 3:45 = 945
    if minute < 915:
        return 0   # too early
    elif minute < 925:
        return 1
    elif minute < 935:
        return 2
    elif minute < 945:
        return 3
    elif minute < 955:
        return 4   # final
    else:
        return 0   # past 3:55, don't submit


def is_final_attempt() -> bool:
    return get_eod_attempt_number() == 4


def minutes_to_close() -> int:
    """Minutes until 4:00 PM ET. Negative after close."""
    now = now_et()
    close = now.replace(hour=16, minute=0, second=0, microsecond=0)
    return int((close - now).total_seconds() / 60)


# ── Dashboard alert store ─────────────────────────────────────────────────────
# Simple in-memory store for EOD alerts — cleared on server restart,
# written to /tmp/eod_alerts.json for persistence across restarts.

import json
from pathlib import Path

_ALERT_PATH = Path("data/eod_alerts.json")
_ALERT_PATH.parent.mkdir(parents=True, exist_ok=True)


def _load_alerts() -> List[Dict[str, Any]]:
    try:
        if _ALERT_PATH.exists():
            return json.loads(_ALERT_PATH.read_text()).get("alerts", [])
    except Exception:
        pass
    return []


def _save_alerts(alerts: List[Dict[str, Any]]) -> None:
    try:
        _ALERT_PATH.write_text(json.dumps({"alerts": alerts}, indent=2))
    except Exception:
        pass


def add_eod_alert(
    alert_type: str,  # "wide_spread" | "no_fill" | "skipped"
    bucket: str,
    message: str,
    width_pct: Optional[float] = None,
) -> None:
    """Add an EOD alert visible in the dashboard."""
    alerts = _load_alerts()
    alerts.append({
        "timestamp_utc": datetime.utcnow().isoformat(),
        "date": now_et().date().isoformat(),
        "alert_type": alert_type,
        "bucket": bucket,
        "message": message,
        "width_pct": width_pct,
        "resolved": False,
    })
    # Keep last 30 alerts
    _save_alerts(alerts[-30:])
    logger.warning("EOD ALERT [%s] %s: %s", alert_type, bucket, message)


def get_eod_alerts(date: Optional[str] = None) -> List[Dict[str, Any]]:
    """Return EOD alerts, optionally filtered to a specific date."""
    alerts = _load_alerts()
    if date:
        alerts = [a for a in alerts if a.get("date") == date]
    return alerts


def clear_eod_alerts(date: Optional[str] = None) -> None:
    """Clear alerts for a date (called when spread is eventually filled)."""
    alerts = _load_alerts()
    if date:
        alerts = [a for a in alerts if a.get("date") != date]
    _save_alerts(alerts)
