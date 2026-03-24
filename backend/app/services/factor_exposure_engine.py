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
    routing_action: str = "dedicated"
    routing_reason: str = ""


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


def _get_cash_like_symbols() -> set[str]:
    """
    Symbols that should be ignored for factor budgeting and unmapped warnings.

    Typical examples:
    - treasury / cash parking ETFs
    - box-spread style carry sleeves
    """
    cash_like = FACTOR_ALLOCATION_SETTINGS.get("cash_like_symbols", []) or []
    return {str(x).upper() for x in cash_like}


def _build_residual_beta_row(
    *,
    source_rows: list[FactorExposureRow],
    portfolio_value: float,
    residual_factor_name: str,
    residual_hedge_proxy: str,
) -> FactorExposureRow | None:
    residual_gross = sum(r.gross_exposure_dollars for r in source_rows)
    if residual_gross <= 0:
        return None

    residual_exposure_pct = residual_gross / portfolio_value if portfolio_value > 0 else 0.0

    return FactorExposureRow(
        factor=residual_factor_name,
        gross_exposure_dollars=residual_gross,
        exposure_pct=residual_exposure_pct,
        threshold_pct=0.0,
        excess_pct=residual_exposure_pct,
        hedge_proxy=residual_hedge_proxy,
        routing_action="aggregated_to_beta",
        routing_reason="aggregated sub-threshold factor exposures",
    )


def compute_factor_exposures(
    *,
    positions: list[Any],
    portfolio_value: float,
) -> list[FactorExposureRow]:
    """
    Scan holdings and compute gross factor exposures.

    Routing logic:
    1) cash-like symbols are ignored entirely
    2) mapped factors above threshold remain dedicated hedge factors
    3) mapped factors below threshold are aggregated into residual_beta
    4) only positive long exposure counts toward hedge budgeting

    This prevents many small factors from being dropped and silently left unhedged.
    """
    symbol_to_factor = _build_symbol_to_factor_map()
    cash_like_symbols = _get_cash_like_symbols()

    factor_totals: dict[str, float] = {}

    for p in positions:
        symbol = _extract_symbol(p)
        if not symbol:
            continue

        if symbol in cash_like_symbols:
            continue

        factor = symbol_to_factor.get(symbol)
        if not factor:
            continue

        market_value = _extract_market_value(p)

        # Only positive exposure should drive hedge budget.
        if market_value <= 0:
            continue

        factor_totals[factor] = factor_totals.get(factor, 0.0) + market_value

    dedicated_rows: list[FactorExposureRow] = []
    residual_source_rows: list[FactorExposureRow] = []

    residual_factor_name = str(
        FACTOR_ALLOCATION_SETTINGS.get("residual_beta_factor_name", "residual_beta")
    )
    residual_hedge_proxy = str(
        FACTOR_ALLOCATION_SETTINGS.get("residual_beta_hedge_proxy", "QQQ")
    )

    for factor, gross_dollars in factor_totals.items():
        exposure_pct = gross_dollars / portfolio_value if portfolio_value > 0 else 0.0
        threshold_pct = float(FACTOR_MIN_EXPOSURE_PCT.get(factor, 1.0) or 1.0)
        hedge_proxy = FACTOR_HEDGE_PROXIES.get(factor)

        row = FactorExposureRow(
            factor=factor,
            gross_exposure_dollars=gross_dollars,
            exposure_pct=exposure_pct,
            threshold_pct=threshold_pct,
            excess_pct=max(exposure_pct - threshold_pct, 0.0),
            hedge_proxy=hedge_proxy,
        )

        if exposure_pct >= threshold_pct:
            row.routing_action = "dedicated"
            row.routing_reason = "factor exposure exceeds dedicated hedge threshold"
            dedicated_rows.append(row)
        else:
            row.routing_action = "aggregated_to_beta"
            row.routing_reason = "factor exposure below dedicated hedge threshold"
            residual_source_rows.append(row)

    residual_row = _build_residual_beta_row(
        source_rows=residual_source_rows,
        portfolio_value=portfolio_value,
        residual_factor_name=residual_factor_name,
        residual_hedge_proxy=residual_hedge_proxy,
    )

    final_rows = sorted(
        dedicated_rows + ([residual_row] if residual_row else []),
        key=lambda r: r.gross_exposure_dollars,
        reverse=True,
    )

    return final_rows


def allocate_factor_hedge_budget(
    *,
    factor_rows: list[FactorExposureRow],
    total_budget_dollars: float,
    regime: str,
) -> list[dict]:
    """
    Allocate hedge budget dynamically across factors that are ACTUALLY present.

    Logic:
    1) dedicated factors above threshold receive direct budget
    2) residual_beta bucket receives budget for aggregated small-factor risk
    3) weighting uses:
         excess exposure
         x factor priority
         x regime multiplier
    4) if nothing qualifies and config says so, route all budget to core factor
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
        # dedicated factors use excess_pct
        # residual_beta uses full exposure_pct because it intentionally captures
        # aggregated sub-threshold exposures
        base_exposure = row.excess_pct if row.factor != "residual_beta" else row.exposure_pct

        if base_exposure <= 0:
            continue

        priority = float(FACTOR_BUDGET_PRIORITY.get(row.factor, 1.0) or 1.0)
        regime_mult = float(regime_multiplier_map.get(row.factor, 1.0) or 1.0)

        weight = base_exposure * priority * regime_mult

        if weight > 0:
            weighted_rows.append((row, weight))

    total_weight = sum(weight for _, weight in weighted_rows)

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
                        "routing_action": core_row.routing_action,
                        "routing_reason": core_row.routing_reason,
                        "priority_weight": float(
                            FACTOR_BUDGET_PRIORITY.get(core_row.factor, 1.0) or 1.0
                        ),
                        "regime_multiplier": float(
                            regime_multiplier_map.get(core_row.factor, 1.0) or 1.0
                        ),
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
                "routing_action": row.routing_action,
                "routing_reason": row.routing_reason,
                "priority_weight": float(
                    FACTOR_BUDGET_PRIORITY.get(row.factor, 1.0) or 1.0
                ),
                "regime_multiplier": float(
                    regime_multiplier_map.get(row.factor, 1.0) or 1.0
                ),
                "allocated_budget_dollars": budget,
            }
        )

    return sorted(
        allocations,
        key=lambda x: x["allocated_budget_dollars"],
        reverse=True,
    )


def compute_unmapped_exposures(
    *,
    positions: list[Any],
    portfolio_value: float,
) -> list[dict]:
    """
    Surface long positions that:
    - are not cash-like
    - are not mapped to any factor

    This is for config maintenance / explainability, not direct hedge budgeting.
    """
    symbol_to_factor = _build_symbol_to_factor_map()
    cash_like_symbols = _get_cash_like_symbols()

    rows: list[dict] = []

    for p in positions:
        symbol = _extract_symbol(p)
        if not symbol:
            continue

        if symbol in cash_like_symbols:
            continue

        if symbol in symbol_to_factor:
            continue

        market_value = _extract_market_value(p)
        if market_value <= 0:
            continue

        rows.append(
            {
                "symbol": symbol,
                "gross_exposure_dollars": market_value,
                "exposure_pct": market_value / portfolio_value if portfolio_value > 0 else 0.0,
                "suggested_action": "add_factor_mapping_or_route_to_residual_beta",
            }
        )

    rows.sort(key=lambda x: x["gross_exposure_dollars"], reverse=True)
    return rows