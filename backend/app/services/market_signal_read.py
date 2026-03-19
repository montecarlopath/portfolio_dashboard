from __future__ import annotations

"""
market_signal_read.py

Computes market regime signals used by the hedge intelligence engine.

Price sourcing strategy:
  - SPY current price   → Alpaca stock snapshot (real-time during market hours,
                           latest trade after hours). Never uses Stooq for the
                           current price so we always get today's actual level.
  - SPY historical closes → Stooq daily CSV (420 days for SMA200 + RSI buffer).
                            Stooq is EOD only so it gives yesterday's close as
                            the most recent point. We override that last point
                            with the live Alpaca price so the SMA/RSI anchors
                            to the correct current level.
  - VIX                 → Alpaca snapshot first, Finnhub fallback.
"""

import logging
import os
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional

import requests
from sqlalchemy.orm import Session

from app.config import load_alpaca_key, load_alpaca_secret
from app.services.finnhub_market_data import (
    get_daily_closes_stooq,
    get_latest_price,
)

logger = logging.getLogger(__name__)

# ── Alpaca data base URL (always market data, not broker URL) ─────────────────
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


# ── Alpaca stock snapshot ─────────────────────────────────────────────────────

def _get_alpaca_stock_snapshot(symbol: str) -> Dict[str, Any]:
    """
    Fetch a single stock snapshot from Alpaca.

    Returns the raw snapshot dict or {} on failure.
    The snapshot contains:
      latestTrade.p   → latest trade price
      latestQuote.ap  → ask price
      latestQuote.bp  → bid price
      dailyBar.c      → today's daily close so far (or previous close)
      prevDailyBar.c  → previous session close
    """
    try:
        url = f"{_ALPACA_DATA_URL}/v2/stocks/snapshots"
        resp = requests.get(
            url,
            headers=_alpaca_headers(),
            params={"symbols": symbol.upper(), "feed": "iex"},
            timeout=10,
        )
        resp.raise_for_status()
        payload = resp.json()
        snap = payload.get(symbol.upper()) or {}
        return snap
    except Exception as e:
        logger.warning("Alpaca stock snapshot failed for %s: %s", symbol, e)
        return {}


def _get_alpaca_spy_price() -> Optional[float]:
    """
    Return the best available SPY price from Alpaca.

    Priority:
      1. latestTrade.p  (most recent tick — real-time during market hours)
      2. dailyBar.c     (today's bar close)
      3. prevDailyBar.c (previous session close — always available)
    """
    snap = _get_alpaca_stock_snapshot("SPY")
    if not snap:
        return None

    # Latest trade is most accurate during market hours
    latest_trade = snap.get("latestTrade") or {}
    price = latest_trade.get("p")
    if price and float(price) > 0:
        logger.info("Alpaca SPY price (latestTrade): %.2f", float(price))
        return float(price)

    # Daily bar close (intraday VWAP close)
    daily_bar = snap.get("dailyBar") or {}
    price = daily_bar.get("c")
    if price and float(price) > 0:
        logger.info("Alpaca SPY price (dailyBar.c): %.2f", float(price))
        return float(price)

    # Previous session close — always available
    prev_bar = snap.get("prevDailyBar") or {}
    price = prev_bar.get("c")
    if price and float(price) > 0:
        logger.info("Alpaca SPY price (prevDailyBar.c): %.2f", float(price))
        return float(price)

    return None


def _get_alpaca_vix() -> Optional[float]:
    """
    Fetch VIX from Alpaca. VIX trades as VIXY (ETF proxy) or as index data.
    We try the snapshot for VIXY as a proxy, then fall back to Finnhub.
    """
    # Try VIXY as a VIX proxy (liquid ETF)
    for symbol in ("VIXY", "VXX"):
        snap = _get_alpaca_stock_snapshot(symbol)
        if snap:
            latest_trade = snap.get("latestTrade") or {}
            price = latest_trade.get("p")
            if price and float(price) > 0:
                # VIXY is not VIX — don't use it as a VIX level directly
                # It tracks short-term VIX futures, not spot VIX
                break

    # VIX spot is not available via Alpaca stock data feed — use Finnhub
    return None


# ── Historical close series ───────────────────────────────────────────────────

def _parse_target_date(target_date: Optional[str]) -> date:
    if not target_date:
        return date.today()
    return datetime.strptime(target_date, "%Y-%m-%d").date()


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except Exception:
        return default


def _extract_close_series(raw_history: Any) -> List[float]:
    """Extract a list of float closes from various data formats."""
    if raw_history is None:
        return []

    # pandas DataFrame
    try:
        if hasattr(raw_history, "columns") and hasattr(raw_history, "__getitem__"):
            if "close" in raw_history.columns:
                return [_safe_float(x) for x in raw_history["close"].tolist() if x is not None]
            if "c" in raw_history.columns:
                return [_safe_float(x) for x in raw_history["c"].tolist() if x is not None]
    except Exception:
        pass

    if isinstance(raw_history, dict):
        if isinstance(raw_history.get("c"), list):
            return [_safe_float(x) for x in raw_history["c"] if x is not None]
        if isinstance(raw_history.get("closes"), list):
            return [_safe_float(x) for x in raw_history["closes"] if x is not None]
        if isinstance(raw_history.get("results"), list):
            out: List[float] = []
            for row in raw_history["results"]:
                if isinstance(row, dict):
                    v = row.get("close") or row.get("c")
                    if v is not None:
                        out.append(_safe_float(v))
            return out
        if isinstance(raw_history.get("data"), list):
            out = []
            for row in raw_history["data"]:
                if isinstance(row, dict):
                    v = row.get("close") or row.get("c")
                    if v is not None:
                        out.append(_safe_float(v))
            return out
        return []

    if isinstance(raw_history, list):
        out = []
        for row in raw_history:
            if isinstance(row, tuple) and len(row) >= 2:
                out.append(_safe_float(row[1]))
            elif isinstance(row, dict):
                v = row.get("close") or row.get("c") or row.get("adjClose")
                if v is not None:
                    out.append(_safe_float(v))
            elif isinstance(row, (int, float)):
                out.append(float(row))
        return out

    return []


def _fetch_historical_closes(ticker: str, start_date: str, end_date: str) -> List[float]:
    """Fetch EOD historical closes from Stooq (420 days for SMA200 + RSI)."""
    start_dt = datetime.strptime(start_date, "%Y-%m-%d").date()
    end_dt = datetime.strptime(end_date, "%Y-%m-%d").date()

    try:
        raw = get_daily_closes_stooq(ticker, start_dt, end_dt)
        closes = _extract_close_series(raw)
        if closes:
            logger.info(
                "Stooq historical closes for %s: %d rows (last=%.2f)",
                ticker, len(closes), closes[-1],
            )
            return closes
    except Exception as e:
        logger.warning("Stooq historical fetch failed for %s: %s", ticker, e)

    return []


# ── Technical indicators ──────────────────────────────────────────────────────

def _sma(values: List[float], window: int) -> Optional[float]:
    if len(values) < window:
        return None
    return sum(values[-window:]) / window


def _compute_rsi(values: List[float], period: int = 14) -> Optional[float]:
    if len(values) < period + 1:
        return None

    gains: List[float] = []
    losses: List[float] = []
    for i in range(1, len(values)):
        change = values[i] - values[i - 1]
        gains.append(max(change, 0.0))
        losses.append(abs(min(change, 0.0)))

    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period

    if avg_loss == 0:
        return 100.0
    if avg_gain == 0:
        return 0.0

    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


# ── Main entry point ──────────────────────────────────────────────────────────

def get_market_regime_signals(
    db: Session,
    target_date: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Compute all market regime signals.

    Current SPY price → Alpaca (real-time or latest trade).
    Historical series  → Stooq EOD, with the last data point replaced by the
                         Alpaca live price so SMA/RSI are anchored correctly.
    """
    as_of_date = _parse_target_date(target_date)
    end_date = as_of_date.strftime("%Y-%m-%d")
    start_date = (as_of_date - timedelta(days=420)).strftime("%Y-%m-%d")

    # ── Step 1: Get live SPY price from Alpaca ────────────────────────────────
    alpaca_spy_price = _get_alpaca_spy_price()

    # ── Step 2: Get historical close series from Stooq ────────────────────────
    spy_closes = _fetch_historical_closes("SPY", start_date, end_date)

    # ── Step 3: Splice — replace last Stooq close with live Alpaca price ──────
    # Stooq gives yesterday's close as the most recent point.
    # Replacing it ensures SMA/RSI compute against today's actual level.
    if alpaca_spy_price and alpaca_spy_price > 0:
        if spy_closes:
            spy_closes[-1] = alpaca_spy_price  # override yesterday's EOD with live
        else:
            spy_closes = [alpaca_spy_price]
        latest_spy_price = alpaca_spy_price
        price_source = "alpaca_live"
    elif spy_closes:
        latest_spy_price = spy_closes[-1]
        price_source = "stooq_eod"
    else:
        # Final fallback: Finnhub
        try:
            latest_spy_price = _safe_float(get_latest_price("SPY"), 0.0)
            price_source = "finnhub_fallback"
        except Exception:
            latest_spy_price = 0.0
            price_source = "none"

    # ── Step 4: Compute technical indicators ──────────────────────────────────
    sma50 = _sma(spy_closes, 50)
    sma200 = _sma(spy_closes, 200)
    rsi14 = _compute_rsi(spy_closes, 14)

    spy_above_50dma = bool(sma50 is not None and latest_spy_price > sma50)
    spy_above_200dma = bool(sma200 is not None and latest_spy_price > sma200)
    spy_distance_from_200dma_pct = (
        ((latest_spy_price - sma200) / sma200) * 100.0
        if sma200 not in (None, 0)
        else 0.0
    )

    # ── Step 5: VIX — Finnhub (Alpaca doesn't carry VIX spot) ────────────────
    vix_level = 20.0
    for symbol in ("VIX", "^VIX"):
        try:
            px = _safe_float(get_latest_price(symbol), 0.0)
            if px > 0:
                vix_level = px
                break
        except Exception:
            continue

    # ── Step 6: Breadth proxy ─────────────────────────────────────────────────
    if sma200 is None:
        breadth_pct_above_200dma = 55.0
    elif spy_above_200dma and spy_above_50dma:
        breadth_pct_above_200dma = 65.0
    elif spy_above_200dma:
        breadth_pct_above_200dma = 55.0
    else:
        breadth_pct_above_200dma = 40.0

    logger.info(
        "REGIME SIGNALS as_of=%s spy=%.2f [%s] closes=%d sma50=%s sma200=%s "
        "rsi14=%s vix=%.1f above50=%s above200=%s dist200=%.2f",
        end_date,
        latest_spy_price,
        price_source,
        len(spy_closes),
        f"{sma50:.2f}" if sma50 else "n/a",
        f"{sma200:.2f}" if sma200 else "n/a",
        f"{rsi14:.2f}" if rsi14 else "n/a",
        vix_level,
        spy_above_50dma,
        spy_above_200dma,
        spy_distance_from_200dma_pct,
    )

    return {
        "as_of_date": end_date,
        "spy_close": latest_spy_price,
        "spy_price_source": price_source,
        "spy_above_50dma": spy_above_50dma,
        "spy_above_200dma": spy_above_200dma,
        "spy_distance_from_200dma_pct": round(_safe_float(spy_distance_from_200dma_pct), 2),
        "spy_rsi_14": round(_safe_float(rsi14, 50.0), 2),
        "breadth_pct_above_200dma": round(_safe_float(breadth_pct_above_200dma), 2),
        "vix_level": round(_safe_float(vix_level, 20.0), 2),
        "vix_term_structure_ratio": None,
        "credit_stress_score": 0.0,
        "liquidity_stress_score": 0.0,
        "localized_bubble_score": 0.0,
    }
