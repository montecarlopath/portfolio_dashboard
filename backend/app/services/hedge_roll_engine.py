from __future__ import annotations

from datetime import date
from typing import Literal

from app.schemas import (
    HedgeRollDecision,
    HedgeRollEngineResponse,
)
from app.services.hedge_execution_planner import build_hedge_execution_plan


def _safe_dte(as_of_date: str, expiry: str | None) -> int | None:
    if not expiry:
        return None
    try:
        return (date.fromisoformat(expiry) - date.fromisoformat(as_of_date)).days
    except Exception:
        return None


def _decide_primary(
    *,
    as_of_date: str,
    market_regime: str,
    current_hedge_pct: float,
    recommended_hedge_pct: float,
    additional_hedge_pct: float,
    selected_expiry: str | None,
    target_fill_pct: float,
) -> HedgeRollDecision:
    dte = _safe_dte(as_of_date, selected_expiry)

    if market_regime in {"strong_bull"} and current_hedge_pct > 0.10:
        return HedgeRollDecision(
            action="trim",
            structure_name="primary",
            reason="Market regime improved materially; primary hedge can be reduced.",
        )

    if market_regime in {"extended_bull"} and current_hedge_pct > recommended_hedge_pct + 0.05:
        return HedgeRollDecision(
            action="trim",
            structure_name="primary",
            reason="Current hedge is materially above recommended level for this regime.",
        )

    if dte is not None and dte < 35:
        return HedgeRollDecision(
            action="roll",
            structure_name="primary",
            reason="Primary hedge is nearing expiration; roll to maintain correction protection.",
        )

    if additional_hedge_pct > 0.03 and target_fill_pct < 0.90:
        return HedgeRollDecision(
            action="add",
            structure_name="primary",
            reason="Primary hedge does not fully meet current target protection needs.",
        )

    if market_regime in {"early_breakdown", "high_crash_risk"} and target_fill_pct > 1.20:
        return HedgeRollDecision(
            action="trim",
            structure_name="primary",
            reason="Primary hedge is materially oversized versus target.",
        )

    return HedgeRollDecision(
        action="hold",
        structure_name="primary",
        reason="Primary hedge is appropriately sized for the current regime.",
    )


def _decide_tail(
    *,
    as_of_date: str,
    market_regime: str,
    additional_hedge_pct: float,
    selected_expiry: str | None,
    target_fill_pct: float,
) -> HedgeRollDecision:
    dte = _safe_dte(as_of_date, selected_expiry)

    if market_regime == "strong_bull" and additional_hedge_pct <= 0.0:
        return HedgeRollDecision(
            action="close",
            structure_name="tail",
            reason="Crash hedge is no longer needed in the current regime.",
        )

    if dte is not None and dte < 45:
        return HedgeRollDecision(
            action="roll",
            structure_name="tail",
            reason="Tail hedge is nearing expiration; roll to preserve crash convexity.",
        )

    if additional_hedge_pct > 0.02 and target_fill_pct < 0.85:
        return HedgeRollDecision(
            action="add",
            structure_name="tail",
            reason="Tail hedge is underfilled relative to current crash protection needs.",
        )

    if target_fill_pct > 1.30:
        return HedgeRollDecision(
            action="trim",
            structure_name="tail",
            reason="Tail hedge is oversized relative to its sleeve target.",
        )

    return HedgeRollDecision(
        action="hold",
        structure_name="tail",
        reason="Tail hedge remains a reasonable convex sleeve.",
    )


def _summarize_action(primary_action: str, tail_action: str) -> Literal["hold", "add", "roll", "trim", "close"]:
    priority = ["roll", "add", "trim", "close", "hold"]
    for action in priority:
        if primary_action == action or tail_action == action:
            return action  # type: ignore[return-value]
    return "hold"


def build_hedge_roll_engine(
    *,
    as_of_date: str,
    underlying: str,
    market_regime: str,
    hedge_style: str,
    portfolio_value: float,
    current_hedge_pct: float,
    recommended_hedge_pct: float,
    additional_hedge_pct: float,
    remaining_hedge_budget_pct: float,
    vix_level: float = 20.0,
    underlying_price: float | None = None,
) -> HedgeRollEngineResponse:
    plan = build_hedge_execution_plan(
        as_of_date=as_of_date,
        underlying_price=underlying_price,
        market_regime=market_regime,
        hedge_style=hedge_style,
        recommended_hedge_pct=recommended_hedge_pct,
        portfolio_value=portfolio_value,
        additional_hedge_pct=additional_hedge_pct,
        remaining_hedge_budget_pct=remaining_hedge_budget_pct,
    )

    primary_decision = _decide_primary(
        as_of_date=as_of_date,
        market_regime=market_regime,
        current_hedge_pct=current_hedge_pct,
        recommended_hedge_pct=recommended_hedge_pct,
        additional_hedge_pct=additional_hedge_pct,
        selected_expiry=plan.primary_spread.selected_expiry,
        target_fill_pct=plan.primary_spread.target_fill_pct,
    )

    tail_decision = _decide_tail(
        as_of_date=as_of_date,
        market_regime=market_regime,
        additional_hedge_pct=additional_hedge_pct,
        selected_expiry=plan.tail_spread.selected_expiry,
        target_fill_pct=plan.tail_spread.target_fill_pct,
    )

    notes: list[str] = []

    if additional_hedge_pct <= 0:
        notes.append("Current hedge already meets or exceeds recommended hedge level.")

    if plan.total_estimated_cost_pct < 0.005:
        notes.append("Planned hedge cost is modest relative to portfolio size.")

    if plan.tail_spread.coverage_to_cost_ratio > 8:
        notes.append("Tail hedge is highly convex relative to premium spent.")

    return HedgeRollEngineResponse(
        as_of_date=as_of_date,
        benchmark="SPY",
        hedge_style=hedge_style,
        hedge_asset=underlying,
        market_regime=market_regime,
        current_hedge_pct=current_hedge_pct,
        recommended_hedge_pct=recommended_hedge_pct,
        additional_hedge_pct=additional_hedge_pct,
        primary_decision=primary_decision,
        tail_decision=tail_decision,
        summary_action=_summarize_action(primary_decision.action, tail_decision.action),
        notes=notes,
    )