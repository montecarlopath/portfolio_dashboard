from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.services.hedge_config import (
    FACTOR_ALLOCATION_SETTINGS,
    FACTOR_BUDGET_PRIORITY,
    FACTOR_HEDGE_PROXIES,
    FACTOR_MIN_EXPOSURE_PCT,
    FACTOR_REGIME_MULTIPLIERS,
    FACTOR_SYMBOL_MAP,
)


@dataclass
class FactorExposureRow:
    factor: str
    gross_exposure_dollars: float
    exposure_pct: float
    threshold_pct: float
    excess_pct: float
    hedge_proxy: str | None


def _extract_symbol(position: Any) -> str | None:
    """
    Extract the most relevant symbol-like field from a position object.

    Supports both dicts and typed objects.
    """
    if isinstance(position, dict):
        for key in ("symbol", "ticker", "underlying"):
            value = position.get(key)
            if value:
                return str(value).upper()
        return None

    for key in ("symbol", "ticker", "underlying"):
        value = getattr(position, key, None)
        if value:
            return str(value).upper()

    return None


def _extract_market_value(position: Any) -> float:
    """
    Extract position market value / notional from a position object.

    Only positive long exposure is counted for factor budgeting.
    Short / hedge legs should not receive hedge budget.
    """
    if isinstance(position, dict):
        for key in ("market_value", "notional_value", "position_value"):
            value = position.get(key)
            if value is not None:
                return float(value)
        return 0.0

    for key in ("market_value", "notional_value", "position_value"):
        value = getattr(position, key, None)
        if value is not None:
            return float(value)

    return 0.0


def _build_symbol_to_factor_map() -> dict[str, str]:
    """
    Flatten FACTOR_SYMBOL_MAP into a symbol -> factor lookup.

    If a symbol appears in multiple buckets, first-seen wins.
    Keep your config clean so symbols ideally live in one primary factor bucket.
    """
    out: dict[str, str] = {}

    for factor, symbols in FACTOR_SYMBOL_MAP.items():
        for symbol in symbols:
            out.setdefault(str(symbol).upper(), factor)

    return out


def compute_factor_exposures(
    *,
    positions: list[Any],
    portfolio_value: float,
) -> list[FactorExposureRow]:
    """
    Scan holdings and compute gross factor exposures.

    Only factors actually present in holdings will appear.
    Factors below their configured minimum threshold are still shown here,
    but their excess_pct will be zero and they will not receive budget.
    """
    symbol_to_factor = _build_symbol_to_factor_map()
    factor_totals: dict[str, float] = {}

    for p in positions:
        symbol = _extract_symbol(p)
        if not symbol:
            continue

        factor = symbol_to_factor.get(symbol)
        if not factor:
            continue

        market_value = _extract_market_value(p)

        # Only positive exposure should drive hedge budget.
        if market_value <= 0:
            continue

        factor_totals[factor] = factor_totals.get(factor, 0.0) + market_value

    rows: list[FactorExposureRow] = []

    for factor, gross_dollars in factor_totals.items():
        exposure_pct = gross_dollars / portfolio_value if portfolio_value > 0 else 0.0
        threshold_pct = FACTOR_MIN_EXPOSURE_PCT.get(factor, 1.0)
        excess_pct = max(exposure_pct - threshold_pct, 0.0)
        hedge_proxy = FACTOR_HEDGE_PROXIES.get(factor)

        rows.append(
            FactorExposureRow(
                factor=factor,
                gross_exposure_dollars=gross_dollars,
                exposure_pct=exposure_pct,
                threshold_pct=threshold_pct,
                excess_pct=excess_pct,
                hedge_proxy=hedge_proxy,
            )
        )

    return sorted(rows, key=lambda r: r.gross_exposure_dollars, reverse=True)


def allocate_factor_hedge_budget(
    *,
    factor_rows: list[FactorExposureRow],
    total_budget_dollars: float,
    regime: str,
) -> list[dict]:
    """
    Allocate hedge budget dynamically across factors that are ACTUALLY present.

    Logic:
    1) Ignore factors below min exposure threshold (excess_pct <= 0)
    2) Weight eligible factors by:
         excess exposure
         x factor priority
         x regime multiplier
    3) Allocate total budget across those eligible factors
    4) If no factor qualifies and config says so, route all budget to core factor
    """
    if total_budget_dollars <= 0:
        return []

    min_total_budget = float(
        FACTOR_ALLOCATION_SETTINGS.get("min_total_factor_budget_dollars", 0.0) or 0.0
    )
    if total_budget_dollars < min_total_budget:
        return []

    regime_multiplier_map = FACTOR_REGIME_MULTIPLIERS.get(regime, {})
    reserve_unassigned_to_core = bool(
        FACTOR_ALLOCATION_SETTINGS.get("reserve_unassigned_budget_to_core", True)
    )
    core_factor = str(FACTOR_ALLOCATION_SETTINGS.get("core_factor", "tech"))

    weighted_rows: list[tuple[FactorExposureRow, float]] = []

    for row in factor_rows:
        if row.excess_pct <= 0:
            continue

        priority = float(FACTOR_BUDGET_PRIORITY.get(row.factor, 1.0) or 1.0)
        regime_mult = float(regime_multiplier_map.get(row.factor, 1.0) or 1.0)

        # Weighted score determines budget share.
        weight = row.excess_pct * priority * regime_mult

        if weight > 0:
            weighted_rows.append((row, weight))

    total_weight = sum(weight for _, weight in weighted_rows)

    # If no factor qualified, optionally route all budget to core factor IF core exists.
    if total_weight <= 0:
        if reserve_unassigned_to_core:
            core_row = next((r for r in factor_rows if r.factor == core_factor), None)
            if core_row and core_row.hedge_proxy:
                return [
                    {
                        "factor": core_row.factor,
                        "hedge_proxy": core_row.hedge_proxy,
                        "gross_exposure_dollars": core_row.gross_exposure_dollars,
                        "exposure_pct": core_row.exposure_pct,
                        "threshold_pct": core_row.threshold_pct,
                        "excess_pct": core_row.excess_pct,
                        "priority_weight": float(FACTOR_BUDGET_PRIORITY.get(core_row.factor, 1.0) or 1.0),
                        "regime_multiplier": float(regime_multiplier_map.get(core_row.factor, 1.0) or 1.0),
                        "allocated_budget_dollars": total_budget_dollars,
                    }
                ]
        return []

    allocations: list[dict] = []

    for row, weight in weighted_rows:
        budget = total_budget_dollars * (weight / total_weight)

        allocations.append(
            {
                "factor": row.factor,
                "hedge_proxy": row.hedge_proxy,
                "gross_exposure_dollars": row.gross_exposure_dollars,
                "exposure_pct": row.exposure_pct,
                "threshold_pct": row.threshold_pct,
                "excess_pct": row.excess_pct,
                "priority_weight": float(FACTOR_BUDGET_PRIORITY.get(row.factor, 1.0) or 1.0),
                "regime_multiplier": float(regime_multiplier_map.get(row.factor, 1.0) or 1.0),
                "allocated_budget_dollars": budget,
            }
        )

    return sorted(allocations, key=lambda x: x["allocated_budget_dollars"], reverse=True)