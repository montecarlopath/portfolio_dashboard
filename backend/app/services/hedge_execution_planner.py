from __future__ import annotations

import math

from app.schemas import (
    HedgeExecutionPlanResponse,
    HedgeStructurePlan,
    OptionContractCandidate,
)
from app.services.option_selector import select_hedge_spreads


from app.config import HEDGE_STYLE_STRUCTURE_SPLIT_MAP


def _get_mark(leg: OptionContractCandidate | None) -> float:
    if leg is None:
        return 0.0

    if leg.mark is not None and leg.mark > 0:
        return float(leg.mark)

    if leg.bid is not None and leg.ask is not None and leg.ask >= leg.bid:
        return (float(leg.bid) + float(leg.ask)) / 2.0

    if leg.ask is not None:
        return float(leg.ask)

    if leg.bid is not None:
        return float(leg.bid)

    return 0.0


def _choose_contract_count(
    *,
    target_hedge_dollars: float,
    budget_dollars: float,
    debit_per_contract: float,
    max_payoff_per_contract: float,
) -> tuple[int, list[str]]:
    notes: list[str] = []

    if debit_per_contract <= 0:
        return 0, ["Spread debit is non-positive; cannot size contracts."]

    if max_payoff_per_contract <= 0:
        return 0, ["Spread max payoff is non-positive; cannot size contracts."]

    contracts_by_budget = int(budget_dollars // debit_per_contract)
    if contracts_by_budget <= 0:
        return 0, ["Budget too small for even one spread."]

    if target_hedge_dollars <= 0:
        return 0, ["Target hedge dollars are zero."]

    contracts_by_target = max(
        int(math.ceil(target_hedge_dollars / max_payoff_per_contract)),
        0,
    )

    contracts = min(contracts_by_budget, contracts_by_target)

    if contracts <= 0 and contracts_by_budget > 0:
        contracts = 1

    if contracts_by_budget < contracts_by_target:
        notes.append("Budget does not fully cover target hedge; sized to budget-feasible contract count.")
    elif contracts * max_payoff_per_contract > target_hedge_dollars * 1.10:
        notes.append("Discrete contract sizing slightly overfills target hedge.")

    return contracts, notes


def _plan_one_spread(
    *,
    spread,
    target_hedge_dollars: float,
    budget_dollars: float,
) -> HedgeStructurePlan:
    if spread.long_leg is None or spread.short_leg is None:
        return HedgeStructurePlan(
            structure_name=spread.structure_name,
            selected_expiry=spread.selected_expiry,
            long_leg=spread.long_leg,
            short_leg=spread.short_leg,
            contracts=0,
            spread_width=0.0,
            debit_per_contract=0.0,
            max_payoff_per_contract=0.0,
            target_hedge_dollars=target_hedge_dollars,
            estimated_coverage_dollars=0.0,
            estimated_cost_dollars=0.0,
            coverage_to_cost_ratio=0.0,
            target_fill_pct=0.0,
            budget_used_pct=0.0,
            notes=["No valid spread selected."],
        )

    long_mark = _get_mark(spread.long_leg)
    short_mark = _get_mark(spread.short_leg)

    width = max(float(spread.long_leg.strike) - float(spread.short_leg.strike), 0.0)
    debit = max(long_mark - short_mark, 0.0)

    debit_per_contract = debit * 100.0
    max_payoff_per_contract = max(width - debit, 0.0) * 100.0

    contracts, notes = _choose_contract_count(
        target_hedge_dollars=target_hedge_dollars,
        budget_dollars=budget_dollars,
        debit_per_contract=debit_per_contract,
        max_payoff_per_contract=max_payoff_per_contract,
    )

    estimated_cost_dollars = contracts * debit_per_contract
    estimated_coverage_dollars = contracts * max_payoff_per_contract

    coverage_to_cost_ratio = (
        estimated_coverage_dollars / estimated_cost_dollars
        if estimated_cost_dollars > 0
        else 0.0
    )
    target_fill_pct = (
        estimated_coverage_dollars / target_hedge_dollars
        if target_hedge_dollars > 0
        else 0.0
    )
    budget_used_pct = (
        estimated_cost_dollars / budget_dollars
        if budget_dollars > 0
        else 0.0
    )

    return HedgeStructurePlan(
        structure_name=spread.structure_name,
        selected_expiry=spread.selected_expiry,
        long_leg=spread.long_leg,
        short_leg=spread.short_leg,
        contracts=contracts,
        spread_width=width,
        debit_per_contract=debit_per_contract,
        max_payoff_per_contract=max_payoff_per_contract,
        target_hedge_dollars=target_hedge_dollars,
        estimated_coverage_dollars=estimated_coverage_dollars,
        estimated_cost_dollars=estimated_cost_dollars,
        coverage_to_cost_ratio=coverage_to_cost_ratio,
        target_fill_pct=target_fill_pct,
        budget_used_pct=budget_used_pct,
        notes=notes,
    )


def build_hedge_execution_plan(
    *,
    as_of_date: str,
    underlying: str,
    market_regime: str,
    hedge_style: str,
    portfolio_value: float,
    recommended_hedge_pct: float,
    additional_hedge_pct: float,
    remaining_hedge_budget_pct: float,
    vix_level: float = 20.0,
    underlying_price: float | None = None,
) -> HedgeExecutionPlanResponse:
    spread_selection = select_hedge_spreads(
        as_of_date=as_of_date,
        underlying=underlying,
        market_regime=market_regime,
        hedge_style=hedge_style,
        underlying_price=underlying_price,
    )

    style_split = HEDGE_STYLE_SPLIT_MAP.get(
        hedge_style,
        {"primary": 0.70, "tail": 0.30},
    )

    total_target_hedge_dollars = portfolio_value * recommended_hedge_pct
    total_budget_dollars = portfolio_value * remaining_hedge_budget_pct

    primary_target = total_target_hedge_dollars * style_split["primary"]
    tail_target = total_target_hedge_dollars * style_split["tail"]

    primary_budget = total_budget_dollars * style_split["primary"]
    tail_budget = total_budget_dollars * style_split["tail"]

    primary_plan = _plan_one_spread(
        spread=spread_selection.primary_spread,
        target_hedge_dollars=primary_target,
        budget_dollars=primary_budget,
    )

    tail_plan = _plan_one_spread(
        spread=spread_selection.tail_spread,
        target_hedge_dollars=tail_target,
        budget_dollars=tail_budget,
    )

    total_estimated_cost_dollars = (
        primary_plan.estimated_cost_dollars + tail_plan.estimated_cost_dollars
    )
    total_estimated_hedge_dollars = (
        primary_plan.estimated_coverage_dollars + tail_plan.estimated_coverage_dollars
    )

    return HedgeExecutionPlanResponse(
        as_of_date=as_of_date,
        benchmark="SPY",
        hedge_style=hedge_style,
        hedge_asset=underlying,
        market_regime=market_regime,
        primary_spread=primary_plan,
        tail_spread=tail_plan,
        total_estimated_cost_dollars=total_estimated_cost_dollars,
        total_estimated_cost_pct=(
            total_estimated_cost_dollars / portfolio_value if portfolio_value > 0 else 0.0
        ),
        total_estimated_hedge_dollars=total_estimated_hedge_dollars,
        total_estimated_hedge_pct=(
            total_estimated_hedge_dollars / portfolio_value if portfolio_value > 0 else 0.0
        ),
    )