from __future__ import annotations

from typing import Dict, List, Optional

from app.schemas import HedgePositionSnapshot
from app.services.broker_positions_engine import get_broker_positions
from app.services.finnhub_market_data import get_latest_price
from app.services.hedge_position_classifier import (
    classify_option_bucket,
    classify_structure_type,
)
from app.services.option_chain_read import get_option_snapshots_alpaca
from app.services.option_valuation import parse_occ_option_symbol


def _safe_float(value, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except Exception:
        return default


def _get_position_qty(position) -> float:
    """
    Returns the raw qty from Alpaca — may be negative for short positions.
    Alpaca returns qty as a signed float: positive for long, negative for short.
    """
    return _safe_float(getattr(position, "qty", 0.0), 0.0)


def _get_position_side(position) -> str:
    return str(getattr(position, "side", "") or "").lower()


def _get_snapshot_mark(snapshot: Dict) -> float:
    mark = _safe_float(snapshot.get("mark"), 0.0)
    if mark > 0:
        return mark

    bid = _safe_float(snapshot.get("bid"), 0.0)
    ask = _safe_float(snapshot.get("ask"), 0.0)

    if bid > 0 and ask > 0 and ask >= bid:
        return (bid + ask) / 2.0

    if ask > 0:
        return ask

    if bid > 0:
        return bid

    return 0.0


def _get_snapshot_delta(snapshot: Dict) -> Optional[float]:
    raw = snapshot.get("delta")
    if raw is None:
        return None
    try:
        return float(raw)
    except Exception:
        return None


def _signed_qty_for_option_position(position) -> float:
    """
    Returns signed quantity:
      Long position  → positive (e.g. +17)
      Short position → negative (e.g. -17)

    Alpaca qty field is already signed (negative for short positions).
    We normalise using abs() then re-apply sign from the side field
    to handle any broker that returns unsigned qty with a separate side field.
    """
    qty = _get_position_qty(position)
    side = _get_position_side(position)

    # qty == 0 means no position at all
    if qty == 0:
        return 0.0

    # Normalise to positive magnitude — direction comes from side field
    abs_qty = abs(qty)

    if side == "short":
        return -abs_qty

    return abs_qty


def _compute_signed_delta_dollars(
    *,
    signed_qty: float,
    delta: Optional[float],
    spot_price: Optional[float],
) -> float:
    """
    Compute delta-dollar exposure preserving sign:
      Long put  (signed_qty > 0, delta < 0) → negative delta dollars (hedge)
      Short put (signed_qty < 0, delta < 0) → positive delta dollars (offsets hedge)

    For a put spread:
      Net delta = long_delta_dollars + short_delta_dollars
                = (negative) + (positive)
                = smaller negative number = true spread delta
    """
    if signed_qty == 0 or delta is None or spot_price is None or spot_price <= 0:
        return 0.0

    return signed_qty * 100.0 * float(delta) * float(spot_price)


def _compute_market_value_from_mark(
    *,
    qty: float,
    mark: float,
) -> float:
    """
    Compute market value from mark price.
    Always use abs(qty) — market_value magnitude is the same for long/short.
    The sign of market_value (liability vs asset) is handled by Alpaca's
    reported market_value field directly.
    """
    abs_qty = abs(qty)
    if abs_qty <= 0 or mark <= 0:
        return 0.0
    return abs_qty * 100.0 * mark


def _filter_relevant_alpaca_option_positions(
    *,
    positions: List,
    underlying: str,
) -> List:
    """
    Filter to QQQ put positions only — both long and short legs.

    IMPORTANT: short legs (qty < 0) are included. They represent the sold
    leg of a put spread. Excluding them would cause the system to treat
    spreads as naked long puts — overstating delta and understating cost.
    """
    out: List = []

    for p in positions:
        symbol = str(getattr(p, "symbol", "") or "")
        parsed = parse_occ_option_symbol(symbol)

        if parsed is None:
            continue

        if parsed.underlying != underlying:
            continue

        # Alpaca is used for put hedge spreads only
        if parsed.option_type != "P":
            continue

        qty = _get_position_qty(p)

        # qty == 0 means no position — skip.
        # qty != 0 (positive = long, negative = short) — include both.
        if qty == 0:
            continue

        out.append(p)

    return out


def load_alpaca_hedge_positions(
    *,
    as_of_date: str,
    underlying: str = "QQQ",
    spot_price: Optional[float] = None,
) -> List[HedgePositionSnapshot]:
    """
    Load Alpaca hedge option positions and normalize them into HedgePositionSnapshot.

    Returns BOTH long and short legs of spread positions so that:
      - Delta is correctly netted (spread delta < naked long delta)
      - Cost basis reflects net debit paid (long premium - short premium received)
      - Close orders can be built correctly using both leg symbols

    Signed conventions:
      quantity:      positive for long legs, negative for short legs
      delta_dollars: negative for net put hedge exposure (as expected by intelligence engine)
      market_value:  positive for long legs, negative (liability) for short legs
    """
    if not underlying:
        return []

    underlying = underlying.upper().strip()

    if spot_price is None or spot_price <= 0:
        live_spot = get_latest_price(underlying)
        if live_spot is not None and live_spot > 0:
            spot_price = float(live_spot)

    broker_resp = get_broker_positions(broker="alpaca")
    all_positions = list(getattr(broker_resp, "positions", []) or [])

    relevant_positions = _filter_relevant_alpaca_option_positions(
        positions=all_positions,
        underlying=underlying,
    )

    if not relevant_positions:
        return []

    # Collect all symbols (long and short legs) for snapshot fetch
    symbols = [
        str(getattr(p, "symbol", "") or "")
        for p in relevant_positions
        if getattr(p, "symbol", None)
    ]

    snapshots = get_option_snapshots_alpaca(symbols)

    out: List[HedgePositionSnapshot] = []

    for p in relevant_positions:
        symbol = str(getattr(p, "symbol", "") or "")
        parsed = parse_occ_option_symbol(symbol)
        if parsed is None:
            continue

        raw_qty = _get_position_qty(p)
        signed_qty = _signed_qty_for_option_position(p)

        # Skip zero-quantity positions (shouldn't happen after filter, but guard)
        if raw_qty == 0:
            continue

        snap = snapshots.get(symbol, {}) or {}
        mark = _get_snapshot_mark(snap)
        delta = _get_snapshot_delta(snap)

        # Market value: use Alpaca's reported value if available.
        # For short legs Alpaca reports a negative market_value (liability).
        # Fall back to mark-based calculation using abs quantity.
        market_value = _safe_float(getattr(p, "market_value", 0.0), 0.0)
        if market_value == 0 and mark > 0:
            market_value = _compute_market_value_from_mark(qty=raw_qty, mark=mark)
            # For short legs, market_value should be negative (it's a liability)
            if signed_qty < 0:
                market_value = -abs(market_value)

        # Cost basis: Alpaca reports the original premium per leg.
        # For short legs this is the premium received (stored as negative by Alpaca).
        total_cost_basis = _safe_float(getattr(p, "cost_basis", 0.0), 0.0)

        # Delta dollars: signed — long put = negative, short put = positive
        delta_dollars = _compute_signed_delta_dollars(
            signed_qty=signed_qty,
            delta=delta,
            spot_price=spot_price,
        )

        # Classify bucket using the long leg's properties
        # Short leg shares the same expiry/strike family — bucket is the same
        bucket = classify_option_bucket(
            expiry=parsed.expiry.isoformat(),
            strike=parsed.strike,
            option_type=parsed.option_type,
            underlying=parsed.underlying,
            as_of_date=as_of_date,
            spot_price=spot_price,
        )

        structure_type = classify_structure_type(
            bucket=bucket,
            option_type=parsed.option_type,
        )

        out.append(
            HedgePositionSnapshot(
                symbol=symbol,
                quantity=signed_qty,   # signed: +17 long, -17 short
                expiry=parsed.expiry.isoformat(),
                strike=parsed.strike,
                option_type=parsed.option_type,
                market_value=market_value,
                total_cost_basis=total_cost_basis,
                delta_dollars=delta_dollars,
                hedge_bucket=bucket,
                structure_type=structure_type,
            )
        )

    return out