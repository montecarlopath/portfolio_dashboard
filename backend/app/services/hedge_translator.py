from __future__ import annotations

from datetime import date

from app.schemas import HedgeExecutionPlanResponse, HedgeStructurePlan

PRIMARY_WIDTH_PCT = 0.10
TAIL_WIDTH_PCT = 0.20

PRIMARY_BASE_COVERAGE_EFFICIENCY = 0.85
TAIL_BASE_COVERAGE_EFFICIENCY = 0.45

PRIMARY_BASE_COST_PCT = 0.012
TAIL_BASE_COST_PCT = 0.006

MIN_CONTRACTS_IF_ACTIVE = 1

REGIME_SPLIT_MAP = {
    "strong_bull": {"primary": 0.40, "tail": 0.60},
    "extended_bull": {"primary": 0.60, "tail": 0.40},
    "early_breakdown": {"primary": 0.75, "tail": 0.25},
    "high_crash_risk": {"primary": 0.85, "tail": 0.15},
    "localized_bubble": {"primary": 0.80, "tail": 0.20},
    "neutral": {"primary": 0.60, "tail": 0.40},
}

STYLE_ADJUSTMENTS = {
    "balanced": {
        "primary_split_shift": 0.00,
        "primary_cost_mult": 1.00,
        "tail_cost_mult": 1.00,
        "primary_eff_mult": 1.00,
        "tail_eff_mult": 1.00,
    },
    "cost_sensitive": {
        "primary_split_shift": -0.05,
        "primary_cost_mult": 0.90,
        "tail_cost_mult": 0.90,
        "primary_eff_mult": 0.95,
        "tail_eff_mult": 0.95,
    },
    "crash_paranoid": {
        "primary_split_shift": -0.10,
        "primary_cost_mult": 1.10,
        "tail_cost_mult": 1.20,
        "primary_eff_mult": 1.00,
        "tail_eff_mult": 1.10,
    },
    "correction_focused": {
        "primary_split_shift": 0.10,
        "primary_cost_mult": 1.10,
        "tail_cost_mult": 0.95,
        "primary_eff_mult": 1.05,
        "tail_eff_mult": 0.90,
    },
}


def _round_contracts(value: float) -> int:
    if value <= 0:
        return 0
    rounded = int(round(value))
    return max(rounded, MIN_CONTRACTS_IF_ACTIVE)


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _get_regime_split(market_regime: str) -> tuple[float, float]:
    split = REGIME_SPLIT_MAP.get(market_regime, REGIME_SPLIT_MAP["neutral"])
    return float(split["primary"]), float(split["tail"])


def _get_style_adjustments(hedge_style: str) -> dict:
    return STYLE_ADJUSTMENTS.get(hedge_style, STYLE_ADJUSTMENTS["balanced"])


def _asset_display_name(hedge_asset: str) -> str:
    if hedge_asset == "QQQ":
        return "QQQ"
    return "SPY"


def _structure_name(underlying: str, primary: bool) -> str:
    if primary:
        return f"{underlying} 40D put / 20D put"
    return f"{underlying} 10D put / 5D put"


def _translate_single_asset_plan(
    underlying: str,
    asset_weight: float,
    portfolio_value: float,
    additional_hedge_pct: float,
    hedge_budget_pct: float,
    asset_price: float,
    market_regime: str,
    hedge_style: str,
):
    regime_primary, regime_tail = _get_regime_split(market_regime)
    style_adj = _get_style_adjustments(hedge_style)

    primary_split = _clamp(regime_primary + style_adj["primary_split_shift"], 0.20, 0.90)
    tail_split = 1.0 - primary_split

    asset_target_hedge_pct = additional_hedge_pct * asset_weight

    primary_pct = asset_target_hedge_pct * primary_split
    tail_pct = asset_target_hedge_pct * tail_split

    primary_target_dollars = portfolio_value * primary_pct
    tail_target_dollars = portfolio_value * tail_pct

    primary_width = asset_price * PRIMARY_WIDTH_PCT
    tail_width = asset_price * TAIL_WIDTH_PCT

    primary_nominal_payoff = primary_width * 100.0
    tail_nominal_payoff = tail_width * 100.0

    primary_efficiency = PRIMARY_BASE_COVERAGE_EFFICIENCY * style_adj["primary_eff_mult"]
    tail_efficiency = TAIL_BASE_COVERAGE_EFFICIENCY * style_adj["tail_eff_mult"]

    primary_effective_payoff = primary_nominal_payoff * primary_efficiency
    tail_effective_payoff = tail_nominal_payoff * tail_efficiency

    primary_contracts = (
        _round_contracts(primary_target_dollars / primary_effective_payoff)
        if primary_effective_payoff > 0 else 0
    )
    tail_contracts = (
        _round_contracts(tail_target_dollars / tail_effective_payoff)
        if tail_effective_payoff > 0 else 0
    )

    primary_coverage_dollars = primary_contracts * primary_effective_payoff
    tail_coverage_dollars = tail_contracts * tail_effective_payoff

    primary_cost_pct = PRIMARY_BASE_COST_PCT
    tail_cost_pct = TAIL_BASE_COST_PCT

    if market_regime == "strong_bull":
        tail_cost_pct *= 1.10
    elif market_regime == "extended_bull":
        tail_cost_pct *= 1.05
    elif market_regime == "early_breakdown":
        primary_cost_pct *= 1.05
    elif market_regime == "high_crash_risk":
        primary_cost_pct *= 1.15
        tail_cost_pct *= 1.15

    primary_cost_pct *= style_adj["primary_cost_mult"] * asset_weight
    tail_cost_pct *= style_adj["tail_cost_mult"] * asset_weight

    raw_total_cost_pct = primary_cost_pct + tail_cost_pct
    if hedge_budget_pct > 0 and raw_total_cost_pct > hedge_budget_pct:
        scale = hedge_budget_pct / raw_total_cost_pct
        primary_cost_pct *= scale
        tail_cost_pct *= scale

    primary_cost_dollars = portfolio_value * primary_cost_pct
    tail_cost_dollars = portfolio_value * tail_cost_pct

    return {
        "primary": HedgeStructurePlan(
            structure=_structure_name(underlying, primary=True),
            underlying=underlying,
            target_hedge_pct=primary_pct,
            contracts=primary_contracts,
            estimated_cost_pct=primary_cost_pct,
            estimated_cost_dollars=primary_cost_dollars,
            estimated_coverage_dollars=primary_coverage_dollars,
        ),
        "tail": HedgeStructurePlan(
            structure=_structure_name(underlying, primary=False),
            underlying=underlying,
            target_hedge_pct=tail_pct,
            contracts=tail_contracts,
            estimated_cost_pct=tail_cost_pct,
            estimated_cost_dollars=tail_cost_dollars,
            estimated_coverage_dollars=tail_coverage_dollars,
        ),
    }


def translate_hedge_plan(
    portfolio_value: float,
    additional_hedge_pct: float,
    hedge_budget_pct: float,
    spy_price: float,
    qqq_price: float,
    market_regime: str,
    hedge_style: str = "balanced",
    hedge_asset: str = "SPY",
) -> HedgeExecutionPlanResponse:
    if hedge_asset == "QQQ":
        plan = _translate_single_asset_plan(
            underlying="QQQ",
            asset_weight=1.0,
            portfolio_value=portfolio_value,
            additional_hedge_pct=additional_hedge_pct,
            hedge_budget_pct=hedge_budget_pct,
            asset_price=qqq_price,
            market_regime=market_regime,
            hedge_style=hedge_style,
        )
        primary_plan = plan["primary"]
        tail_plan = plan["tail"]

    elif hedge_asset == "hybrid":
        spy_weight = 0.50
        qqq_weight = 0.50

        spy_plan = _translate_single_asset_plan(
            underlying="SPY",
            asset_weight=spy_weight,
            portfolio_value=portfolio_value,
            additional_hedge_pct=additional_hedge_pct,
            hedge_budget_pct=hedge_budget_pct * spy_weight,
            asset_price=spy_price,
            market_regime=market_regime,
            hedge_style=hedge_style,
        )

        qqq_plan = _translate_single_asset_plan(
            underlying="QQQ",
            asset_weight=qqq_weight,
            portfolio_value=portfolio_value,
            additional_hedge_pct=additional_hedge_pct,
            hedge_budget_pct=hedge_budget_pct * qqq_weight,
            asset_price=qqq_price,
            market_regime=market_regime,
            hedge_style=hedge_style,
        )

        primary_plan = HedgeStructurePlan(
            structure=f"Hybrid primary: {spy_plan['primary'].structure} + {qqq_plan['primary'].structure}",
            underlying="hybrid",
            target_hedge_pct=spy_plan["primary"].target_hedge_pct + qqq_plan["primary"].target_hedge_pct,
            contracts=spy_plan["primary"].contracts + qqq_plan["primary"].contracts,
            estimated_cost_pct=spy_plan["primary"].estimated_cost_pct + qqq_plan["primary"].estimated_cost_pct,
            estimated_cost_dollars=spy_plan["primary"].estimated_cost_dollars + qqq_plan["primary"].estimated_cost_dollars,
            estimated_coverage_dollars=spy_plan["primary"].estimated_coverage_dollars + qqq_plan["primary"].estimated_coverage_dollars,
        )

        tail_plan = HedgeStructurePlan(
            structure=f"Hybrid tail: {spy_plan['tail'].structure} + {qqq_plan['tail'].structure}",
            underlying="hybrid",
            target_hedge_pct=spy_plan["tail"].target_hedge_pct + qqq_plan["tail"].target_hedge_pct,
            contracts=spy_plan["tail"].contracts + qqq_plan["tail"].contracts,
            estimated_cost_pct=spy_plan["tail"].estimated_cost_pct + qqq_plan["tail"].estimated_cost_pct,
            estimated_cost_dollars=spy_plan["tail"].estimated_cost_dollars + qqq_plan["tail"].estimated_cost_dollars,
            estimated_coverage_dollars=spy_plan["tail"].estimated_coverage_dollars + qqq_plan["tail"].estimated_coverage_dollars,
        )

    else:
        plan = _translate_single_asset_plan(
            underlying="SPY",
            asset_weight=1.0,
            portfolio_value=portfolio_value,
            additional_hedge_pct=additional_hedge_pct,
            hedge_budget_pct=hedge_budget_pct,
            asset_price=spy_price,
            market_regime=market_regime,
            hedge_style=hedge_style,
        )
        primary_plan = plan["primary"]
        tail_plan = plan["tail"]

    total_estimated_cost_pct = (
        primary_plan.estimated_cost_pct + tail_plan.estimated_cost_pct
    )
    total_estimated_cost_dollars = (
        primary_plan.estimated_cost_dollars + tail_plan.estimated_cost_dollars
    )
    total_estimated_hedge_dollars = (
        primary_plan.estimated_coverage_dollars + tail_plan.estimated_coverage_dollars
    )
    total_estimated_hedge_pct = (
        total_estimated_hedge_dollars / portfolio_value if portfolio_value > 0 else 0.0
    )

    return HedgeExecutionPlanResponse(
        as_of_date=str(date.today()),
        benchmark="SPY",
        hedge_style=hedge_style,
        hedge_asset=hedge_asset,
        market_regime=market_regime,
        primary_spread=primary_plan,
        tail_spread=tail_plan,
        total_estimated_cost_pct=total_estimated_cost_pct,
        total_estimated_cost_dollars=total_estimated_cost_dollars,
        total_estimated_hedge_pct=total_estimated_hedge_pct,
        total_estimated_hedge_dollars=total_estimated_hedge_dollars,
    )