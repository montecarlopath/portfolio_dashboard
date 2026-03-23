from __future__ import annotations

from app.schemas import (
    HedgeExecutionPlanV2Response,
    HedgeFactorStructurePlan,
)
from app.services.option_selector import select_hedge_spreads
from app.services.hedge_execution_planner import _plan_one_spread


def build_factor_aware_hedge_execution_plan(
    *,
    as_of_date: str,
    market_regime: str,
    hedge_style: str,
    portfolio_value: float,
    factor_structure_allocations: list[dict],
    vix_level: float = 20.0,
) -> HedgeExecutionPlanV2Response:
    factor_plans: list[HedgeFactorStructurePlan] = []

    total_estimated_cost_dollars = 0.0
    total_estimated_hedge_dollars = 0.0

    for row in factor_structure_allocations:
        factor = str(row.get("factor", "unknown"))
        underlying = str(row.get("hedge_proxy") or "")
        structure_budgets = row.get("structure_budgets", {}) or {}

        if not underlying:
            continue

        primary_budget = float(structure_budgets.get("primary", 0.0) or 0.0)
        tail_budget = float(structure_budgets.get("tail", 0.0) or 0.0)
        convex_budget = float(structure_budgets.get("convex", 0.0) or 0.0)

        spread_selection = select_hedge_spreads(
            as_of_date=as_of_date,
            underlying=underlying,
            market_regime=market_regime,
            hedge_style=hedge_style,
            underlying_price=None,
        )

        primary_plan = _plan_one_spread(
            spread=spread_selection.primary_spread,
            target_hedge_dollars=primary_budget,
            budget_dollars=primary_budget,
        )

        tail_plan = _plan_one_spread(
            spread=spread_selection.tail_spread,
            target_hedge_dollars=tail_budget,
            budget_dollars=tail_budget,
        )

        factor_plans.append(
            HedgeFactorStructurePlan(
                factor=factor,
                underlying=underlying,
                primary_spread=primary_plan,
                tail_spread=tail_plan,
                convex_budget_dollars=convex_budget,
            )
        )

        total_estimated_cost_dollars += (
            primary_plan.estimated_cost_dollars + tail_plan.estimated_cost_dollars
        )
        total_estimated_hedge_dollars += (
            primary_plan.estimated_coverage_dollars + tail_plan.estimated_coverage_dollars
        )

    return HedgeExecutionPlanV2Response(
        as_of_date=as_of_date,
        benchmark="SPY",
        hedge_style=hedge_style,
        market_regime=market_regime,
        factor_plans=factor_plans,
        total_estimated_cost_dollars=total_estimated_cost_dollars,
        total_estimated_cost_pct=(
            total_estimated_cost_dollars / portfolio_value if portfolio_value > 0 else 0.0
        ),
        total_estimated_hedge_dollars=total_estimated_hedge_dollars,
        total_estimated_hedge_pct=(
            total_estimated_hedge_dollars / portfolio_value if portfolio_value > 0 else 0.0
        ),
    )