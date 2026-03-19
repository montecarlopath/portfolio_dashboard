from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Any, Dict, List, Optional

import requests

from app.services.finnhub_market_data import get_latest_price

from app.config import (
    load_alpaca_key,
    load_alpaca_secret,
    load_alpaca_base_url,
    load_alpaca_data_url,
)

logger = logging.getLogger(__name__)

# Alpaca multi-snapshot endpoint accepts a comma-separated symbol list.
# Keep batches modest to avoid URL size issues.
_SNAPSHOT_BATCH_SIZE = 25


def normalize_option_chain_records(raw_records: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    normalized = []

    for row in raw_records:
        strike = row.get("strike")
        delta = row.get("delta")
        bid = row.get("bid")
        ask = row.get("ask")
        mark = row.get("mark")
        open_interest = row.get("open_interest")
        volume = row.get("volume")

        try:
            strike_val = float(strike or 0.0)
        except Exception:
            strike_val = 0.0

        try:
            delta_val = float(delta) if delta is not None else None
        except Exception:
            delta_val = None

        try:
            bid_val = float(bid) if bid is not None else None
        except Exception:
            bid_val = None

        try:
            ask_val = float(ask) if ask is not None else None
        except Exception:
            ask_val = None

        try:
            mark_val = float(mark) if mark is not None else None
        except Exception:
            mark_val = None

        try:
            oi_val = int(open_interest) if open_interest is not None else None
        except Exception:
            oi_val = None

        try:
            vol_val = int(volume) if volume is not None else None
        except Exception:
            vol_val = None

        normalized.append(
            {
                "symbol": row.get("symbol"),
                "underlying": row.get("underlying", "QQQ"),
                "expiry": row.get("expiry"),
                "strike": strike_val,
                "option_type": str(row.get("option_type", "")).upper(),
                "delta": delta_val,
                "bid": bid_val,
                "ask": ask_val,
                "mark": mark_val,
                "open_interest": oi_val,
                "volume": vol_val,
            }
        )

    return normalized


def _alpaca_headers() -> Dict[str, str]:
    key = load_alpaca_key()
    secret = load_alpaca_secret()

    if not key or not secret:
        raise ValueError("Alpaca API credentials are not configured.")

    return {
        "accept": "application/json",
        "APCA-API-KEY-ID": key,
        "APCA-API-SECRET-KEY": secret,
    }


def _safe_get(d: Dict[str, Any], *keys: str, default=None):
    cur: Any = d
    for key in keys:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(key)
        if cur is None:
            return default
    return cur


def _chunked(items: List[str], size: int) -> List[List[str]]:
    return [items[i:i + size] for i in range(0, len(items), size)]


def _request_json(url: str, headers: Dict[str, str], params: Optional[Dict[str, Any]] = None) -> Any:
    resp = requests.get(url, headers=headers, params=params or {}, timeout=30)
    resp.raise_for_status()
    return resp.json()


def _normalize_contract_row(contract: Dict[str, Any], underlying: str) -> Dict[str, Any]:
    """
    Normalize a row from Alpaca /v2/options/contracts.
    """
    symbol = (
        contract.get("symbol")
        or contract.get("option_symbol")
        or contract.get("id")
        or ""
    )

    expiry = (
        contract.get("expiration_date")
        or contract.get("expiry")
        or contract.get("expiration")
    )

    strike = (
        contract.get("strike_price")
        or contract.get("strike")
        or 0.0
    )

    option_type = (
        contract.get("type")
        or contract.get("option_type")
        or contract.get("contract_type")
        or ""
    )

    open_interest = (
        contract.get("open_interest")
        or contract.get("openInterest")
    )

    try:
        strike = float(strike or 0.0)
    except Exception:
        strike = 0.0

    try:
        open_interest = int(open_interest) if open_interest is not None else None
    except Exception:
        open_interest = None

    return {
        "symbol": str(symbol),
        "underlying": underlying,
        "expiry": str(expiry) if expiry else None,
        "strike": strike,
        "option_type": str(option_type).upper(),
        "delta": None,
        "bid": None,
        "ask": None,
        "mark": None,
        "open_interest": open_interest,
        "volume": None,
    }


def _normalize_snapshot_row(symbol: str, snap: Dict[str, Any]) -> Dict[str, Any]:
    """
    Normalize one row from Alpaca /v1beta1/options/snapshots.
    """
    bid = (
        _safe_get(snap, "latestQuote", "bp")
        or _safe_get(snap, "quote", "bp")
        or snap.get("bid")
    )
    ask = (
        _safe_get(snap, "latestQuote", "ap")
        or _safe_get(snap, "quote", "ap")
        or snap.get("ask")
    )

    mark = snap.get("mark")
    if mark is None:
        try:
            if bid is not None and ask is not None:
                mark = (float(bid) + float(ask)) / 2.0
        except Exception:
            mark = None

    delta = (
        _safe_get(snap, "greeks", "delta")
        or _safe_get(snap, "latestGreeks", "delta")
        or snap.get("delta")
    )

    volume = (
        _safe_get(snap, "dailyBar", "volume")
        or _safe_get(snap, "bar", "volume")
        or snap.get("volume")
    )

    open_interest = (
        snap.get("open_interest")
        or _safe_get(snap, "openInterest")
        or _safe_get(snap, "details", "open_interest")
    )

    try:
        bid = float(bid) if bid is not None else None
    except Exception:
        bid = None

    try:
        ask = float(ask) if ask is not None else None
    except Exception:
        ask = None

    try:
        mark = float(mark) if mark is not None else None
    except Exception:
        mark = None

    try:
        delta = float(delta) if delta is not None else None
    except Exception:
        delta = None

    try:
        volume = int(volume) if volume is not None else None
    except Exception:
        volume = None

    try:
        open_interest = int(open_interest) if open_interest is not None else None
    except Exception:
        open_interest = None

    return {
        "symbol": symbol,
        "delta": delta,
        "bid": bid,
        "ask": ask,
        "mark": mark,
        "open_interest": open_interest,
        "volume": volume,
    }


def get_option_contracts_alpaca(
    underlying: str,
    expiry_gte: str,
    expiry_lte: str,
    option_type: str = "PUT",
) -> List[Dict[str, Any]]:
    """
    Fetch a filtered contract universe from Alpaca trading API.
    """
    underlying = (underlying or "").strip().upper()
    option_type = (option_type or "").strip().lower()

    if not underlying:
        return []

    base_url = load_alpaca_base_url().rstrip("/")
    headers = _alpaca_headers()
    url = f"{base_url}/options/contracts"

    all_rows: List[Dict[str, Any]] = []
    page_token: Optional[str] = None
    seen_tokens: set[str] = set()

    while True:
        params: Dict[str, Any] = {
            "underlying_symbols": underlying,
            "expiration_date_gte": expiry_gte,
            "expiration_date_lte": expiry_lte,
            "type": option_type.lower(),
            "status": "active",
            "limit": 1000,
        }
        if page_token:
            params["page_token"] = page_token

        payload = _request_json(url, headers, params)

        rows = []
        if isinstance(payload, dict):
            rows = payload.get("option_contracts") or payload.get("contracts") or payload.get("data") or []
        if not isinstance(rows, list):
            rows = []

        for row in rows:
            if isinstance(row, dict):
                all_rows.append(_normalize_contract_row(row, underlying))

        next_page_token = payload.get("next_page_token") if isinstance(payload, dict) else None
        if not next_page_token:
            break

        if next_page_token in seen_tokens:
            logger.warning("ALPACA contracts pagination loop detected for %s", underlying)
            break

        seen_tokens.add(next_page_token)
        page_token = next_page_token

    logger.info(
        "ALPACA contracts loaded underlying=%s expiry_gte=%s expiry_lte=%s count=%s",
        underlying,
        expiry_gte,
        expiry_lte,
        len(all_rows),
    )
    return all_rows




def get_option_snapshots_alpaca(symbols: List[str]) -> Dict[str, Dict[str, Any]]:
    """
    Fetch latest quote/greeks snapshots for specific option symbols.

    Uses small batches to avoid very long query strings.
    Falls back to single-symbol requests if a batch returns HTTP 400.
    """
    if not symbols:
        return {}

    data_url = load_alpaca_data_url().rstrip("/")
    headers = _alpaca_headers()
    url = f"{data_url}/v1beta1/options/snapshots"

    snapshots_by_symbol: Dict[str, Dict[str, Any]] = {}

    for chunk in _chunked(symbols, _SNAPSHOT_BATCH_SIZE):
        params = {"symbols": ",".join(chunk)}

        try:
            payload = _request_json(url, headers, params)
            snapshots = payload.get("snapshots") if isinstance(payload, dict) else None
            if isinstance(snapshots, dict):
                for symbol, snap in snapshots.items():
                    if isinstance(snap, dict):
                        snapshots_by_symbol[symbol] = _normalize_snapshot_row(symbol, snap)
            continue

        except requests.HTTPError as e:
            status_code = e.response.status_code if e.response is not None else None

            # Fallback for too-large / bad multi-symbol requests
            if status_code != 400:
                raise

            logger.warning(
                "ALPACA multi-snapshot batch failed with 400; falling back to single-symbol requests for batch size=%s",
                len(chunk),
            )

        # Single-symbol fallback
        for symbol in chunk:
            try:
                single_url = f"{data_url}/v1beta1/options/snapshots/{symbol}"
                payload = _request_json(single_url, headers, None)

                # Some endpoints return the snapshot directly; some may wrap it
                snap = payload
                if isinstance(payload, dict) and "snapshot" in payload:
                    snap = payload["snapshot"]

                if isinstance(snap, dict):
                    snapshots_by_symbol[symbol] = _normalize_snapshot_row(symbol, snap)

            except Exception as single_err:
                logger.warning(
                    "ALPACA single-symbol snapshot failed for %s: %s",
                    symbol,
                    single_err,
                )

    logger.info("ALPACA snapshots loaded symbols=%s", len(snapshots_by_symbol))
    return snapshots_by_symbol


def get_live_option_chain(
    underlying: str,
    expiry_gte: Optional[str] = None,
    expiry_lte: Optional[str] = None,
    option_type: str = "PUT",
) -> List[Dict[str, Any]]:
    """
    Phase 5A normalized option chain interface:

    1) fetch contracts in desired expiry window
    2) trim contracts by strike band around spot
    3) enrich remaining symbols with snapshots
    4) merge into normalized records
    """

    underlying = (underlying or "").strip().upper()
    if not underlying:
        return []

    today = date.today()

    if not expiry_gte:
        expiry_gte = today.isoformat()

    if not expiry_lte:
        expiry_lte = (today + timedelta(days=120)).isoformat()

    contracts = get_option_contracts_alpaca(
        underlying=underlying,
        expiry_gte=expiry_gte,
        expiry_lte=expiry_lte,
        option_type=option_type,
    )

    if not contracts:
        return []

    # ----------------------------------------------------
    # NEW: reduce universe using strike band around spot
    # ----------------------------------------------------

    spot = None
    try:
        spot = get_latest_price(underlying)
    except Exception:
        pass

    if spot:
        lower = spot * 0.50
        upper = spot * 1.05

        contracts = [
            row for row in contracts
            if lower <= float(row.get("strike", 0.0) or 0.0) <= upper
        ]

    logger.info(
        "ALPACA contracts after strike filter underlying=%s remaining=%s",
        underlying,
        len(contracts),
    )

    # ----------------------------------------------------
    # snapshot enrichment
    # ----------------------------------------------------

    symbols = [row["symbol"] for row in contracts if row.get("symbol")]

    snapshots = get_option_snapshots_alpaca(symbols)

    merged: List[Dict[str, Any]] = []

    for row in contracts:
        symbol = row.get("symbol")
        snap = snapshots.get(symbol, {})

        merged.append(
            {
                "symbol": symbol,
                "underlying": row.get("underlying", underlying),
                "expiry": row.get("expiry"),
                "strike": row.get("strike"),
                "option_type": row.get("option_type"),
                "delta": snap.get("delta"),
                "bid": snap.get("bid"),
                "ask": snap.get("ask"),
                "mark": snap.get("mark"),
                "open_interest": snap.get("open_interest", row.get("open_interest")),
                "volume": snap.get("volume"),
            }
        )

    normalized = normalize_option_chain_records(merged)

    logger.info(
        "ALPACA live chain merged underlying=%s expiry_gte=%s expiry_lte=%s contracts=%s",
        underlying,
        expiry_gte,
        expiry_lte,
        len(normalized),
    )

    return normalized