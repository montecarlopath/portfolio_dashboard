from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, date
from typing import Optional

from app.services.finnhub_market_data import get_latest_price

_OPTION_RE = re.compile(r"^([A-Z]+)(\d{6})([CP])(\d{8})$")


@dataclass
class ParsedOptionSymbol:
    underlying: str
    expiry: date
    option_type: str
    strike: float


@dataclass
class OptionPositionMetrics:
    symbol: str
    quantity: float
    current_price: float
    current_market_value: float
    avg_cost_basis: float
    total_cost_basis: float
    delta_dollars: float


def parse_occ_option_symbol(symbol: str) -> Optional[ParsedOptionSymbol]:
    if not symbol:
        return None

    m = _OPTION_RE.fullmatch(symbol)
    if not m:
        return None

    underlying = m.group(1)
    expiry_raw = m.group(2)
    option_type = m.group(3)
    strike_raw = m.group(4)

    try:
        expiry = datetime.strptime(expiry_raw, "%y%m%d").date()
        strike = int(strike_raw) / 1000.0
    except Exception:
        return None

    return ParsedOptionSymbol(
        underlying=underlying,
        expiry=expiry,
        option_type=option_type,
        strike=strike,
    )


def is_option_symbol(symbol: str) -> bool:
    return parse_occ_option_symbol(symbol) is not None


def estimate_option_market_value(
    symbol: str,
    quantity: float,
    as_of_date: Optional[date] = None,
) -> float:
    """
    Fallback only. Used when no real live option value is available.
    """
    parsed = parse_occ_option_symbol(symbol)
    if parsed is None:
        return 0.0

    underlying_price = get_latest_price(parsed.underlying)
    if underlying_price is None or underlying_price <= 0:
        return 0.0

    today = as_of_date or date.today()
    days_to_expiry = max((parsed.expiry - today).days, 0)

    if parsed.option_type == "C":
        intrinsic = max(underlying_price - parsed.strike, 0.0)
        otm_amount = max(parsed.strike - underlying_price, 0.0)
    else:
        intrinsic = max(parsed.strike - underlying_price, 0.0)
        otm_amount = max(underlying_price - parsed.strike, 0.0)

    contract_multiplier = 100
    intrinsic_value = max(quantity, 0.0) * intrinsic * contract_multiplier

    if intrinsic > 0:
        return intrinsic_value

    if days_to_expiry <= 0:
        return 0.0

    otm_pct = otm_amount / underlying_price if underlying_price > 0 else 1.0

    if days_to_expiry >= 120:
        if otm_pct <= 0.05:
            time_value_per_share = underlying_price * 0.025
        elif otm_pct <= 0.10:
            time_value_per_share = underlying_price * 0.012
        else:
            time_value_per_share = underlying_price * 0.005
    elif days_to_expiry >= 60:
        if otm_pct <= 0.05:
            time_value_per_share = underlying_price * 0.015
        elif otm_pct <= 0.10:
            time_value_per_share = underlying_price * 0.007
        else:
            time_value_per_share = underlying_price * 0.003
    else:
        if otm_pct <= 0.05:
            time_value_per_share = underlying_price * 0.008
        elif otm_pct <= 0.10:
            time_value_per_share = underlying_price * 0.003
        else:
            time_value_per_share = underlying_price * 0.001

    estimated_time_value = max(quantity, 0.0) * time_value_per_share * contract_multiplier
    return max(estimated_time_value, 0.0)


def get_option_position_metrics_from_holding(
    holding,
    as_of_date: Optional[date] = None,
) -> OptionPositionMetrics:
    """
    Use values already present on the holding if available.
    Otherwise fall back to estimated market value.

    Expected optional fields if broker eventually provides them:
    - option_price / current_price / mark_price / last_price
    - avg_cost_basis
    - total_cost_basis / cost_basis
    """
    symbol = holding.get("symbol", "") if isinstance(holding, dict) else getattr(holding, "symbol", "")
    quantity = float(
        (holding.get("quantity", 0.0) if isinstance(holding, dict) else getattr(holding, "quantity", 0.0)) or 0.0
    )

    market_value = float(
        (
            holding.get("market_value", 0.0)
            if isinstance(holding, dict)
            else getattr(holding, "market_value", 0.0)
        )
        or 0.0
    )

    current_price = float(
        (
            holding.get("option_price")
            or holding.get("current_price")
            or holding.get("mark_price")
            or holding.get("last_price")
            or 0.0
        )
        if isinstance(holding, dict)
        else (
            getattr(holding, "option_price", None)
            or getattr(holding, "current_price", None)
            or getattr(holding, "mark_price", None)
            or getattr(holding, "last_price", None)
            or 0.0
        )
    )

    avg_cost_basis = float(
        (
            holding.get("avg_cost_basis")
            or holding.get("average_cost_basis")
            or 0.0
        )
        if isinstance(holding, dict)
        else (
            getattr(holding, "avg_cost_basis", None)
            or getattr(holding, "average_cost_basis", None)
            or 0.0
        )
    )

    delta_dollars = float(
        (
            holding.get("delta_dollars")
            if isinstance(holding, dict)
            else getattr(holding, "delta_dollars", 0.0)
        ) or 0.0
    )
    total_cost_basis = float(
        (
            holding.get("total_cost_basis")
            or holding.get("cost_basis")
            or 0.0
        )
        if isinstance(holding, dict)
        else (
            getattr(holding, "total_cost_basis", None)
            or getattr(holding, "cost_basis", None)
            or 0.0
        )
    )

    if current_price <= 0 and market_value > 0 and quantity > 0:
        current_price = market_value / (quantity * 100.0)

    if market_value <= 0 and current_price > 0 and quantity > 0:
        market_value = current_price * quantity * 100.0

    if market_value <= 0 and quantity > 0:
        market_value = estimate_option_market_value(
            symbol=symbol,
            quantity=quantity,
            as_of_date=as_of_date,
        )
        if current_price <= 0 and quantity > 0 and market_value > 0:
            current_price = market_value / (quantity * 100.0)

    if total_cost_basis <= 0 and avg_cost_basis > 0 and quantity > 0:
        total_cost_basis = avg_cost_basis * quantity * 100.0

    if avg_cost_basis <= 0 and total_cost_basis > 0 and quantity > 0:
        avg_cost_basis = total_cost_basis / (quantity * 100.0)

    return OptionPositionMetrics(
        symbol=symbol,
        quantity=quantity,
        current_price=max(current_price, 0.0),
        current_market_value=max(market_value, 0.0),
        avg_cost_basis=max(avg_cost_basis, 0.0),
        total_cost_basis=max(total_cost_basis, 0.0),
        delta_dollars=delta_dollars,
    )