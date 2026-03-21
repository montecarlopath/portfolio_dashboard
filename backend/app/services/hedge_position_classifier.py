from __future__ import annotations

from datetime import date
from typing import Optional


PRIMARY_DELTA_MIN = 0.18
PRIMARY_DELTA_MAX = 0.40

TAIL_DELTA_MIN = 0.05
TAIL_DELTA_MAX = 0.18

PRIMARY_STRIKE_RATIO_MIN = 0.90
TAIL_STRIKE_RATIO_MAX = 0.82


def _safe_date_diff(expiry_str: str, as_of_date: str) -> Optional[int]:
    try:
        expiry = date.fromisoformat(expiry_str)
        asof = date.fromisoformat(as_of_date)
        return (expiry - asof).days
    except Exception:
        return None


def _estimate_abs_delta_from_delta_dollars(
    *,
    delta_dollars: Optional[float],
    quantity: Optional[float],
    spot_price: Optional[float],
) -> Optional[float]:
    try:
        if delta_dollars is None or quantity is None or spot_price is None:
            return None
        qty = abs(float(quantity))
        spot = float(spot_price)
        dd = abs(float(delta_dollars))
        if qty <= 0 or spot <= 0:
            return None
        return dd / (qty * 100.0 * spot)
    except Exception:
        return None


def classify_option_bucket(
    *,
    expiry: str,
    strike: float,
    option_type: str,
    underlying: str,
    as_of_date: str,
    spot_price: Optional[float],
    quantity: Optional[float] = None,
    delta_dollars: Optional[float] = None,
) -> str:
    """
    Shared bucket logic for current hedge positions.

    Priority:
    1) delta-based sleeve classification
    2) fallback to strike ratio + DTE
    """

    if str(option_type or "").upper() != "P":
        return "other"

    dte = _safe_date_diff(expiry, as_of_date)
    if dte is None or dte <= 0:
        return "other"

    abs_delta = _estimate_abs_delta_from_delta_dollars(
        delta_dollars=delta_dollars,
        quantity=quantity,
        spot_price=spot_price,
    )

    if abs_delta is not None:
        if TAIL_DELTA_MIN <= abs_delta < TAIL_DELTA_MAX:
            return "tail"
        if PRIMARY_DELTA_MIN <= abs_delta <= PRIMARY_DELTA_MAX:
            return "primary"

    # Fallback only if delta is unavailable or ambiguous
    if spot_price is None or spot_price <= 0:
        if dte >= 75:
            return "tail"
        return "primary"

    strike_ratio = float(strike) / float(spot_price)

    if strike_ratio <= TAIL_STRIKE_RATIO_MAX:
        return "tail"

    if strike_ratio >= PRIMARY_STRIKE_RATIO_MIN:
        return "primary"

    if dte >= 75:
        return "tail"

    return "primary"



def classify_structure_type(
    *,
    bucket: str,
    option_type: str,
    quantity: Optional[float] = None,
) -> str:
    if str(option_type or "").upper() != "P":
        return "other"

    qty = float(quantity or 0.0)

    if bucket == "primary":
        # current primarys are mostly naked longs
        return "primary_spread" if qty < 0 else "naked_put"

    if bucket == "tail":
        return "tail_spread"

    return "other"