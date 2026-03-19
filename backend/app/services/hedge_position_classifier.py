from __future__ import annotations

from datetime import date
from typing import Optional


PRIMARY_STRIKE_RATIO_MIN = 0.88
TAIL_STRIKE_RATIO_MAX = 0.82


def _safe_date_diff(expiry_str: str, as_of_date: str) -> Optional[int]:
    try:
        expiry = date.fromisoformat(expiry_str)
        asof = date.fromisoformat(as_of_date)
        return (expiry - asof).days
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
) -> str:
    """
    Shared bucket logic for current hedge positions.

    Keep this aligned with your existing reconciliation behavior:
    - only PUT hedges are classified into primary/tail buckets
    - primary = closer strike / shorter-medium hedge sleeve
    - tail = farther OTM crash sleeve
    """
    if str(option_type or "").upper() != "P":
        return "other"

    dte = _safe_date_diff(expiry, as_of_date)
    if dte is None or dte <= 0:
        return "other"

    if spot_price is None or spot_price <= 0:
        # fallback: use DTE only if spot is unavailable
        if dte >= 75:
            return "tail"
        return "primary"

    strike_ratio = float(strike) / float(spot_price)

    if strike_ratio <= TAIL_STRIKE_RATIO_MAX:
        return "tail"

    if strike_ratio >= PRIMARY_STRIKE_RATIO_MIN:
        return "primary"

    # middle zone: use DTE tie-break
    if dte >= 75:
        return "tail"

    return "primary"


def classify_structure_type(
    *,
    bucket: str,
    option_type: str,
) -> str:
    if str(option_type or "").upper() != "P":
        return "other"

    if bucket == "primary":
        return "naked_put"
    if bucket == "tail":
        return "tail_put"

    return "other"