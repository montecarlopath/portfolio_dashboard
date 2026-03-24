from __future__ import annotations

"""
hedge_intelligence_read.py  (Phase 6 rewrite)

Key changes from previous version:
  1. Crash-loss anchored target — replaces arbitrary BASE_HEDGE_PCT_MAP %.
     recommended_hedge_pct = protection_needed / portfolio_value
     where protection_needed = max(crash_loss - max_tolerated_loss, 0)

  2. Double-count fix — current_hedge_pct = option puts ONLY.
     Structural positions (PSQ, SQQQ, UVXY...) are already in portfolio_beta.
     They are tracked for display but NOT counted toward closing the gap.

  3. Synthetic betas removed — crash_beta uses regime-calibrated multiplier
     from hedge_config, not the old blanket beta*1.35.

  4. Beta direction flag — detects when net beta goes negative (over-hedged
     or short-bias) and surfaces it for future call-spread phase.

  5. Dynamic factor-aware hedge budgeting:
     - compute factor exposures from actual holdings
     - reserve convex budget by regime
     - allocate available hedge premium budget now only across factors actually present
"""

from typing import Any, List, Optional

from sqlalchemy.orm import Session

from app.schemas import HedgeIntelligenceResponse
from app.services.market_regime_read import classify_market_regime
from app.services.market_signal_read import get_market_regime_signals
from app.services.portfolio_holdings_read import get_portfolio_holdings_data
from app.services.portfolio_risk_read import get_portfolio_beta_data
from app.services.account_clients import get_client_for_account
from app.services.option_valuation import (
    is_option_symbol,
    get_option_position_metrics_from_holding,
)
from app.services.alpaca_hedge_inventory import load_alpaca_hedge_positions
from app.services.finnhub_market_data import get_latest_price
from app.services.hedge_config import (
    MAX_TOLERATED_LOSS_PCT,
    REGIME_SCENARIO_DROPS,
    REGIME_CRASH_BETA_MULTIPLIER,
    HEDGE_BUDGET_PCT,
    BETA_LONG_THRESHOLD,
    BETA_SHORT_THRESHOLD,
    STRUCTURAL_HEDGE_SYMBOLS,
    CONVEX_ALLOCATION_BY_REGIME,
)
from app.services.factor_exposure_engine import (
    compute_factor_exposures,
    allocate_factor_hedge_budget,
)

from app.services.hedge_budget_allocator import allocate_structure_budgets

from app.services.factor_exposure_engine import (
    compute_factor_exposures,
    allocate_factor_hedge_budget,
    compute_unmapped_exposures,
)

import logging

logger = logging.getLogger(__name__)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _get_field(obj: Any, key: str, default=None):
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _get_holding_value(h: Any) -> float:
    for key in ("market_value", "value", "notional_value", "current_value"):
        v = _get_field(h, key, None)
        if v is not None:
            return float(v or 0.0)
    return 0.0


def _is_structural_hedge(symbol: str) -> bool:
    return symbol in STRUCTURAL_HEDGE_SYMBOLS


# ── Core crash-loss sizing ────────────────────────────────────────────────────

def compute_crash_loss(
    portfolio_value: float,
    portfolio_beta: float,
    regime: str,
    scenario: str,  # "primary" or "tail"
) -> float:
    """
    Compute expected portfolio loss at the regime's scenario drop.

    crash_beta = portfolio_beta × regime_multiplier
    crash_loss = portfolio_value × crash_beta × scenario_drop_pct
    """
    scenario_drops = REGIME_SCENARIO_DROPS.get(
        regime, {"primary": 0.20, "tail": 0.30}
    )
    drop_pct = scenario_drops.get(scenario, 0.20)
    crash_multiplier = REGIME_CRASH_BETA_MULTIPLIER.get(regime, 1.35)
    crash_beta = portfolio_beta * crash_multiplier
    return portfolio_value * crash_beta * drop_pct


def compute_protection_needed(
    portfolio_value: float,
    portfolio_beta: float,
    regime: str,
    max_tolerated_loss_pct: float = MAX_TOLERATED_LOSS_PCT,
) -> dict:
    """
    Compute how much crash protection is needed from option puts.
    """
    scenario_drops = REGIME_SCENARIO_DROPS.get(regime, {"primary": 0.20, "tail": 0.30})
    crash_multiplier = REGIME_CRASH_BETA_MULTIPLIER.get(regime, 1.35)
    crash_beta = portfolio_beta * crash_multiplier

    primary_crash_loss = portfolio_value * crash_beta * scenario_drops["primary"]
    tail_crash_loss = portfolio_value * crash_beta * scenario_drops["tail"]
    max_tolerated_loss = portfolio_value * max_tolerated_loss_pct

    primary_protection_needed = max(primary_crash_loss - max_tolerated_loss, 0.0)
    tail_protection_needed = max(tail_crash_loss - max_tolerated_loss, 0.0)

    return {
        "primary_crash_loss": primary_crash_loss,
        "tail_crash_loss": tail_crash_loss,
        "max_tolerated_loss": max_tolerated_loss,
        "primary_protection_needed": primary_protection_needed,
        "tail_protection_needed": tail_protection_needed,
        "total_protection_needed": tail_protection_needed,
        "crash_beta": crash_beta,
        "scenario_drop_primary": scenario_drops["primary"],
        "scenario_drop_tail": scenario_drops["tail"],
    }


# ── Option gap calculation ────────────────────────────────────────────────────

def compute_option_gap(
    protection_needed_dollars: float,
    option_hedge_exposure_dollars: float,
) -> float:
    """
    How much additional option protection is needed.
    """
    return max(protection_needed_dollars - option_hedge_exposure_dollars, 0.0)


# ── Hedge decomposition ───────────────────────────────────────────────────────

def decompose_current_hedges(
    holdings: list,
    beta_rows: list,
    portfolio_value: float,
) -> dict:
    """
    Separate structural positions (negative-beta ETFs) from option puts.

    IMPORTANT: current_hedge_pct is set to option_hedge_pct ONLY.
    Structural positions are tracked for display but the gap calculation
    uses only option exposure, since structurals are already in portfolio_beta.
    """
    beta_by_symbol = {
        _get_field(row, "symbol", ""): row
        for row in beta_rows
        if _get_field(row, "symbol", "")
    }

    structural_hedge_exposure_dollars = 0.0
    structural_hedge_capital_dollars = 0.0
    option_hedge_exposure_dollars = 0.0
    current_hedge_premium_cost = 0.0
    current_hedge_premium_market_value = 0.0
    current_hedge_premium_cost_basis = 0.0

    for h in holdings:
        symbol = _get_field(h, "symbol", "")
        value = _get_holding_value(h)
        if not symbol:
            continue

        row = beta_by_symbol.get(symbol)
        if row is None:
            continue

        dollar_beta_exposure = float(_get_field(row, "dollar_beta_exposure", 0.0) or 0.0)
        negative_exposure = max(-dollar_beta_exposure, 0.0)

        if _is_structural_hedge(symbol):
            structural_hedge_exposure_dollars += negative_exposure
            structural_hedge_capital_dollars += max(value, 0.0)

        elif is_option_symbol(symbol):
            try:
                metrics = get_option_position_metrics_from_holding(h)
                option_negative_exposure = max(-(metrics.delta_dollars or 0.0), 0.0)

                logger.info(
                    "OPTION HEDGE symbol=%s qty=%s market_value=%s cost_basis=%s "
                    "delta_dollars=%s exposure=%s",
                    metrics.symbol,
                    metrics.quantity,
                    metrics.current_market_value,
                    metrics.total_cost_basis,
                    metrics.delta_dollars,
                    option_negative_exposure,
                )

                option_hedge_exposure_dollars += option_negative_exposure
                current_hedge_premium_market_value += metrics.current_market_value

                cost = (
                    metrics.total_cost_basis
                    if metrics.total_cost_basis > 0
                    else metrics.current_market_value
                )
                current_hedge_premium_cost_basis += cost

            except Exception as e:
                logger.warning("OPTION HEDGE metrics failed for %s: %s", symbol, e)

    current_hedge_premium_cost = current_hedge_premium_cost_basis

    option_hedge_pct = option_hedge_exposure_dollars / portfolio_value if portfolio_value > 0 else 0.0
    structural_hedge_pct = structural_hedge_exposure_dollars / portfolio_value if portfolio_value > 0 else 0.0

    structural_efficiency = (
        structural_hedge_exposure_dollars / structural_hedge_capital_dollars
        if structural_hedge_capital_dollars > 0 else 0.0
    )
    premium_efficiency = (
        option_hedge_exposure_dollars / current_hedge_premium_cost
        if current_hedge_premium_cost > 0 else 0.0
    )

    return {
        "current_hedge_pct": option_hedge_pct,
        "current_hedge_exposure_dollars": option_hedge_exposure_dollars,
        "structural_hedge_exposure_dollars": structural_hedge_exposure_dollars,
        "structural_hedge_exposure_pct": structural_hedge_pct,
        "structural_hedge_capital_dollars": structural_hedge_capital_dollars,
        "structural_hedge_efficiency": structural_efficiency,
        "option_hedge_exposure_dollars": option_hedge_exposure_dollars,
        "option_hedge_exposure_pct": option_hedge_pct,
        "current_hedge_premium_cost": current_hedge_premium_cost,
        "current_hedge_premium_cost_pct": (
            current_hedge_premium_cost / portfolio_value if portfolio_value > 0 else 0.0
        ),
        "current_hedge_premium_market_value": current_hedge_premium_market_value,
        "current_hedge_premium_cost_basis": current_hedge_premium_cost_basis,
        "premium_hedge_efficiency": premium_efficiency,
    }


# ── Exposure metrics ──────────────────────────────────────────────────────────

def compute_exposure_metrics_from_beta_rows(rows, portfolio_value: float):
    if portfolio_value <= 0:
        return 0.0, 0.0, 0.0, 0.0

    long_exposure = sum(
        float(_get_field(r, "dollar_beta_exposure", 0.0) or 0.0)
        for r in rows
        if float(_get_field(r, "dollar_beta_exposure", 0.0) or 0.0) > 0
    )
    short_exposure = sum(
        abs(float(_get_field(r, "dollar_beta_exposure", 0.0) or 0.0))
        for r in rows
        if float(_get_field(r, "dollar_beta_exposure", 0.0) or 0.0) < 0
    )

    return (
        long_exposure / portfolio_value,
        short_exposure / portfolio_value,
        (long_exposure - short_exposure) / portfolio_value,
        (long_exposure - short_exposure) / portfolio_value,
    )


# ── Effectiveness metrics ─────────────────────────────────────────────────────

def compute_hedge_effectiveness_metrics(
    portfolio_value: float,
    current_hedge_exposure_dollars: float,
    current_hedge_premium_market_value: float,
    current_hedge_premium_cost_basis: float,
    portfolio_beta: float,
    gross_long_exposure_pct: float,
):
    hedge_unrealized_pnl = current_hedge_premium_market_value - current_hedge_premium_cost_basis
    hedge_cost_drag_dollars = max(current_hedge_premium_cost_basis - current_hedge_premium_market_value, 0.0)
    hedge_cost_drag_pct = hedge_cost_drag_dollars / portfolio_value if portfolio_value > 0 else 0.0

    hedge_protection_capacity_dollars = current_hedge_exposure_dollars
    hedge_protection_capacity_pct = hedge_protection_capacity_dollars / portfolio_value if portfolio_value > 0 else 0.0

    hedge_capacity_ratio = (
        hedge_protection_capacity_dollars / hedge_cost_drag_dollars
        if hedge_cost_drag_dollars > 0 else 0.0
    )

    return {
        "hedge_unrealized_pnl": hedge_unrealized_pnl,
        "hedge_cost_drag_dollars": hedge_cost_drag_dollars,
        "hedge_cost_drag_pct": hedge_cost_drag_pct,
        "hedge_protection_capacity_dollars": hedge_protection_capacity_dollars,
        "hedge_protection_capacity_pct": hedge_protection_capacity_pct,
        "hedge_marked_benefit_dollars": hedge_unrealized_pnl,
        "hedge_marked_benefit_pct": hedge_unrealized_pnl / portfolio_value if portfolio_value > 0 else 0.0,
        "hedge_capacity_ratio": hedge_capacity_ratio,
        "hedged_beta_estimate": portfolio_beta,
        "unhedged_beta_estimate": gross_long_exposure_pct,
    }


# ── Insights builder ──────────────────────────────────────────────────────────

def build_hedge_insights(
    regime: str,
    portfolio_beta: float,
    current_option_hedge_pct: float,
    recommended_hedge_pct: float,
    protection_calc: dict,
    vix_level: float,
    option_gap_dollars: float,
    portfolio_value: float,
    convex_budget_dollars: float,
    convex_budget_pct_of_hedge_budget: float,
) -> list[str]:
    insights = []

    if regime == "strong_bull":
        insights.append("Market trend is strong — only tail crash protection is justified.")
    elif regime == "extended_bull":
        insights.append("Market is extended — build hedges before vol rises and protection becomes expensive.")
    elif regime == "early_breakdown":
        insights.append("Market is losing momentum — directional put protection is warranted for both correction and crash.")
    elif regime == "high_crash_risk":
        insights.append("Crash risk is elevated — prioritize maintaining existing protection over adding new expensive hedges.")
    elif regime == "localized_bubble":
        insights.append("A localized bubble regime suggests targeted hedging rather than broad portfolio protection.")

    prot = protection_calc
    tail_loss_pct = prot["tail_crash_loss"] / portfolio_value if portfolio_value > 0 else 0
    max_tol = prot["max_tolerated_loss"] / portfolio_value if portfolio_value > 0 else MAX_TOLERATED_LOSS_PCT

    insights.append(
        f"At -{prot['scenario_drop_tail']:.0%} SPY, estimated crash loss is "
        f"${prot['tail_crash_loss']:,.0f} ({tail_loss_pct:.1%} of portfolio). "
        f"Tolerance is {max_tol:.0%}."
    )

    if prot["total_protection_needed"] <= 0:
        insights.append(
            "Option puts on are sufficient — portfolio is within loss tolerance at the tail scenario."
        )
    elif option_gap_dollars > 0:
        insights.append(
            f"Option gap: ${option_gap_dollars:,.0f} of additional put protection "
            f"needed to reach the {max_tol:.0%} loss limit."
        )

    if vix_level >= 30:
        insights.append("VIX is elevated — new hedges are expensive. Prioritize spreads over naked puts.")
    elif vix_level <= 18 and regime in ("strong_bull", "extended_bull"):
        insights.append("VIX is subdued — good time to add tail puts cheaply before vol expands.")

    if convex_budget_dollars > 0:
        insights.append(
            f"Convex budget allocation: ${convex_budget_dollars:,.0f} "
            f"({convex_budget_pct_of_hedge_budget:.0%} of remaining hedge premium budget)."
        )

    if portfolio_beta < 0.10:
        insights.append(
            f"Portfolio beta is very low ({portfolio_beta:.2f}) — cash/short positions dominate. "
            f"Structural positions may be providing more offset than needed."
        )
    elif portfolio_beta < 0:
        insights.append(
            f"Portfolio net beta is negative ({portfolio_beta:.2f}) — "
            f"upside hedge (calls) may be warranted if a rally is expected."
        )

    return insights


# ── Alpaca summarizer ─────────────────────────────────────────────────────────

def _summarize_alpaca_hedge_positions(alpaca_positions) -> dict:
    net_delta_dollars = 0.0
    premium_market_value = 0.0
    premium_cost_basis = 0.0
    symbols = []

    for p in alpaca_positions:
        symbol = str(getattr(p, "symbol", "") or "")
        if symbol:
            symbols.append(symbol)

        dd = float(getattr(p, "delta_dollars", 0.0) or 0.0)
        net_delta_dollars += dd

        premium_market_value += float(getattr(p, "market_value", 0.0) or 0.0)
        premium_cost_basis += float(getattr(p, "total_cost_basis", 0.0) or 0.0)

    option_exposure = max(-net_delta_dollars, 0.0)

    return {
        "source": "alpaca",
        "positions_count": len(symbols),
        "option_positions_count": len(symbols),
        "symbols": symbols,
        "structural_hedge_exposure_dollars": 0.0,
        "option_hedge_exposure_dollars": option_exposure,
        "current_hedge_exposure_dollars": option_exposure,
        "current_hedge_premium_cost": premium_cost_basis,
        "current_hedge_premium_market_value": premium_market_value,
        "current_hedge_premium_cost_basis": premium_cost_basis,
    }


def _build_composer_source_breakdown(hedge_breakdown: dict, holdings: list) -> dict:
    option_symbols = [
        str(_get_field(h, "symbol", "") or "")
        for h in holdings
        if is_option_symbol(str(_get_field(h, "symbol", "") or ""))
    ]
    structural_symbols = [
        str(_get_field(h, "symbol", "") or "")
        for h in holdings
        if _is_structural_hedge(str(_get_field(h, "symbol", "") or ""))
    ]
    return {
        "source": "composer",
        "positions_count": len(structural_symbols) + len(option_symbols),
        "option_positions_count": len(option_symbols),
        "symbols": structural_symbols + option_symbols,
        "structural_hedge_exposure_dollars": float(hedge_breakdown["structural_hedge_exposure_dollars"]),
        "option_hedge_exposure_dollars": float(hedge_breakdown["option_hedge_exposure_dollars"]),
        "current_hedge_exposure_dollars": float(hedge_breakdown["current_hedge_exposure_dollars"]),
        "current_hedge_premium_cost": float(hedge_breakdown["current_hedge_premium_cost"]),
        "current_hedge_premium_market_value": float(hedge_breakdown.get("current_hedge_premium_market_value", 0.0)),
        "current_hedge_premium_cost_basis": float(
            hedge_breakdown.get(
                "current_hedge_premium_cost_basis",
                hedge_breakdown["current_hedge_premium_cost"],
            )
        ),
    }


# ── Main entry point ──────────────────────────────────────────────────────────

def get_hedge_intelligence_data(
    db: Session,
    account_ids: List[str],
    target_date: Optional[str] = None,
) -> HedgeIntelligenceResponse:
    # ── Market signals + regime ───────────────────────────────────────────────
    signals = get_market_regime_signals(db=db, target_date=target_date)
    regime_resp = classify_market_regime(signals)
    regime = regime_resp.regime
    vix_level = float(signals.get("vix_level", 20.0) or 20.0)

    # ── Portfolio beta ────────────────────────────────────────────────────────
    beta_resp = get_portfolio_beta_data(
        db=db,
        account_ids=account_ids,
        target_date=target_date,
    )
    portfolio_value = float(_get_field(beta_resp, "portfolio_value", 0.0) or 0.0)
    portfolio_beta = float(_get_field(beta_resp, "portfolio_beta", 0.0) or 0.0)
    portfolio_dollar_beta = float(_get_field(beta_resp, "portfolio_dollar_beta", 0.0) or 0.0)
    beta_rows = _get_field(beta_resp, "rows", []) or []

    # ── Crash beta (regime-calibrated) ────────────────────────────────────────
    crash_multiplier = REGIME_CRASH_BETA_MULTIPLIER.get(regime, 1.35)
    portfolio_crash_beta = portfolio_beta * crash_multiplier
    portfolio_volatility_beta = portfolio_beta * -0.5
    portfolio_liquidity_beta = portfolio_beta * 1.1

    # ── Gross/net exposure ────────────────────────────────────────────────────
    gross_long_pct, gross_short_pct, net_exposure_pct, net_beta_pct = (
        compute_exposure_metrics_from_beta_rows(beta_rows, portfolio_value)
    )

    # ── Holdings + hedge decomposition ────────────────────────────────────────
    holdings_resp = get_portfolio_holdings_data(
        db=db,
        account_ids=account_ids,
        target_date=target_date,
        get_client_for_account_fn=get_client_for_account,
    )
    holdings = _get_field(holdings_resp, "holdings", [])
    holdings_date = _get_field(holdings_resp, "date", None)

    hedge_breakdown = decompose_current_hedges(
        holdings=holdings,
        beta_rows=beta_rows,
        portfolio_value=portfolio_value,
    )
    composer_source = _build_composer_source_breakdown(hedge_breakdown, holdings)

    # ── Alpaca option positions ───────────────────────────────────────────────
    as_of_date_for_alpaca = str(
        target_date
        or signals.get("as_of_date")
        or _get_field(beta_resp, "date", None)
        or holdings_date
        or ""
    )
    qqq_spot = get_latest_price("QQQ")
    alpaca_positions = load_alpaca_hedge_positions(
        as_of_date=as_of_date_for_alpaca,
        underlying="QQQ",
        spot_price=qqq_spot,
    )
    alpaca_summary = _summarize_alpaca_hedge_positions(alpaca_positions)

    # ── Merge Alpaca into totals ──────────────────────────────────────────────
    option_hedge_exposure_dollars = (
        float(hedge_breakdown["option_hedge_exposure_dollars"])
        + float(alpaca_summary["option_hedge_exposure_dollars"])
    )
    structural_hedge_exposure_dollars = float(
        hedge_breakdown["structural_hedge_exposure_dollars"]
    )
    current_hedge_premium_market_value = (
        float(hedge_breakdown.get("current_hedge_premium_market_value", 0.0))
        + float(alpaca_summary["current_hedge_premium_market_value"])
    )
    current_hedge_premium_cost_basis = (
        float(hedge_breakdown.get("current_hedge_premium_cost_basis", 0.0))
        + float(alpaca_summary["current_hedge_premium_cost_basis"])
    )
    current_hedge_premium_cost = (
        float(hedge_breakdown["current_hedge_premium_cost"])
        + float(alpaca_summary["current_hedge_premium_cost"])
    )

    current_hedge_pct = (
        option_hedge_exposure_dollars / portfolio_value if portfolio_value > 0 else 0.0
    )
    option_hedge_pct = current_hedge_pct
    structural_hedge_pct = (
        structural_hedge_exposure_dollars / portfolio_value if portfolio_value > 0 else 0.0
    )
    current_hedge_premium_cost_pct = (
        current_hedge_premium_cost / portfolio_value if portfolio_value > 0 else 0.0
    )
    premium_efficiency = (
        option_hedge_exposure_dollars / current_hedge_premium_cost
        if current_hedge_premium_cost > 0
        else 0.0
    )

    # ── Crash-loss anchored target ────────────────────────────────────────────
    protection_calc = compute_protection_needed(
        portfolio_value=portfolio_value,
        portfolio_beta=portfolio_beta,
        regime=regime,
        max_tolerated_loss_pct=MAX_TOLERATED_LOSS_PCT,
    )

    total_protection_needed = protection_calc["total_protection_needed"]
    recommended_hedge_pct = (
        total_protection_needed / portfolio_value if portfolio_value > 0 else 0.0
    )
    recommended_hedge_pct = max(0.0, min(recommended_hedge_pct, 1.0))

    option_gap_dollars = compute_option_gap(
        protection_needed_dollars=total_protection_needed,
        option_hedge_exposure_dollars=option_hedge_exposure_dollars,
    )
    additional_hedge_pct = (
        option_gap_dollars / portfolio_value if portfolio_value > 0 else 0.0
    )
    additional_hedge_pct = max(0.0, min(additional_hedge_pct, 1.0))

    recommended_hedge_exposure_dollars = total_protection_needed
    additional_hedge_exposure_dollars = option_gap_dollars

    logger.info(
        "HEDGE INTELLIGENCE regime=%s beta=%.3f crash_beta=%.3f "
        "primary_loss=$%.0f tail_loss=$%.0f max_tolerated=$%.0f "
        "protection_needed=$%.0f option_on=$%.0f gap=$%.0f "
        "current_pct=%.1f%% recommended_pct=%.1f%%",
        regime,
        portfolio_beta,
        protection_calc["crash_beta"],
        protection_calc["primary_crash_loss"],
        protection_calc["tail_crash_loss"],
        protection_calc["max_tolerated_loss"],
        total_protection_needed,
        option_hedge_exposure_dollars,
        option_gap_dollars,
        current_hedge_pct * 100,
        recommended_hedge_pct * 100,
    )

    # ── Beta direction flag ───────────────────────────────────────────────────
    if portfolio_beta > BETA_LONG_THRESHOLD:
        beta_direction = "long"
    elif portfolio_beta < -BETA_SHORT_THRESHOLD:
        beta_direction = "short"
    else:
        beta_direction = "neutral"

    if beta_direction == "short":
        logger.warning(
            "HEDGE: net beta is NEGATIVE (%.3f) — portfolio has short bias. "
            "Upside hedging (calls) may be warranted.",
            portfolio_beta,
        )

    # ── Budget guardrail ──────────────────────────────────────────────────────
    hedge_budget_pct = HEDGE_BUDGET_PCT.get(regime, 0.015)
    hedge_budget_dollars = portfolio_value * hedge_budget_pct
    remaining_hedge_budget_dollars = max(
        hedge_budget_dollars - current_hedge_premium_cost, 0.0
    )
    remaining_hedge_budget_pct = max(
        hedge_budget_pct - current_hedge_premium_cost_pct, 0.0
    )

    # Reserve convex budget first
    convex_budget_pct_of_hedge_budget = CONVEX_ALLOCATION_BY_REGIME.get(regime, 0.0)
    convex_budget_dollars = (
        remaining_hedge_budget_dollars * convex_budget_pct_of_hedge_budget
    )

    # Dynamic factor exposures + allocations
    factor_rows = compute_factor_exposures(
        positions=holdings,
        portfolio_value=portfolio_value,
    )
    unmapped_exposures = compute_unmapped_exposures(
        positions=holdings,
        portfolio_value=portfolio_value,
    )
    unmapped_exposure_dollars = sum(
        float(x.get("gross_exposure_dollars", 0.0) or 0.0)
        for x in unmapped_exposures
    )


    factor_budget_allocations = allocate_factor_hedge_budget(
        factor_rows=factor_rows,
        total_budget_dollars=max(
            remaining_hedge_budget_dollars - convex_budget_dollars, 0.0
        ),
        regime=regime,
    )
    factor_structure_allocations = allocate_structure_budgets(
        factor_budget_allocations=factor_budget_allocations,
        regime=regime,
    )

    # ── Effectiveness metrics ─────────────────────────────────────────────────
    effectiveness = compute_hedge_effectiveness_metrics(
        portfolio_value=portfolio_value,
        current_hedge_exposure_dollars=option_hedge_exposure_dollars,
        current_hedge_premium_market_value=current_hedge_premium_market_value,
        current_hedge_premium_cost_basis=current_hedge_premium_cost_basis,
        portfolio_beta=portfolio_beta,
        gross_long_exposure_pct=gross_long_pct,
    )

    # ── Insights ──────────────────────────────────────────────────────────────
    insights = build_hedge_insights(
        regime=regime,
        portfolio_beta=portfolio_beta,
        current_option_hedge_pct=current_hedge_pct,
        recommended_hedge_pct=recommended_hedge_pct,
        protection_calc=protection_calc,
        vix_level=vix_level,
        option_gap_dollars=option_gap_dollars,
        portfolio_value=portfolio_value,
        convex_budget_dollars=convex_budget_dollars,
        convex_budget_pct_of_hedge_budget=convex_budget_pct_of_hedge_budget,
    )

    as_of_date = (
        signals.get("as_of_date")
        or _get_field(beta_resp, "date", None)
        or holdings_date
        or str(target_date or "")
    )

    # ── Theoretical vs practical targets ──────────────────────────────────────
    # Temporary: keep them equal here. The bundle layer can later override
    # practical values with budget-constrained plan outputs.
    theoretical_recommended_hedge_exposure_dollars = (
        recommended_hedge_exposure_dollars
    )
    theoretical_recommended_hedge_pct = recommended_hedge_pct

    practical_recommended_hedge_exposure_dollars = (
        recommended_hedge_exposure_dollars
    )
    practical_recommended_hedge_pct = recommended_hedge_pct

    return HedgeIntelligenceResponse(
        as_of_date=str(as_of_date or ""),
        benchmark="SPY",
        market_regime=regime_resp.regime,
        market_risk_score=regime_resp.market_risk_score,
        new_hedge_aggressiveness=regime_resp.new_hedge_aggressiveness,

        portfolio_value=portfolio_value,
        portfolio_beta=portfolio_beta,
        portfolio_dollar_beta=portfolio_dollar_beta,
        portfolio_crash_beta=portfolio_crash_beta,
        portfolio_volatility_beta=portfolio_volatility_beta,
        portfolio_liquidity_beta=portfolio_liquidity_beta,

        gross_long_exposure_pct=gross_long_pct,
        gross_short_exposure_pct=gross_short_pct,
        net_exposure_pct=net_exposure_pct,
        net_beta_exposure_pct=net_beta_pct,

        current_hedge_pct=current_hedge_pct,
        recommended_hedge_pct=recommended_hedge_pct,
        additional_hedge_pct=additional_hedge_pct,

        current_hedge_exposure_dollars=option_hedge_exposure_dollars,
        recommended_hedge_exposure_dollars=recommended_hedge_exposure_dollars,
        additional_hedge_exposure_dollars=additional_hedge_exposure_dollars,

        theoretical_recommended_hedge_exposure_dollars=theoretical_recommended_hedge_exposure_dollars,
        theoretical_recommended_hedge_pct=theoretical_recommended_hedge_pct,
        practical_recommended_hedge_exposure_dollars=practical_recommended_hedge_exposure_dollars,
        practical_recommended_hedge_pct=practical_recommended_hedge_pct,

        structural_hedge_exposure_dollars=structural_hedge_exposure_dollars,
        structural_hedge_exposure_pct=structural_hedge_pct,
        structural_hedge_capital_dollars=float(
            hedge_breakdown["structural_hedge_capital_dollars"]
        ),
        structural_hedge_efficiency=float(
            hedge_breakdown["structural_hedge_efficiency"]
        ),

        option_hedge_exposure_dollars=option_hedge_exposure_dollars,
        option_hedge_exposure_pct=option_hedge_pct,
        current_hedge_premium_cost=current_hedge_premium_cost,
        current_hedge_premium_cost_pct=current_hedge_premium_cost_pct,
        premium_hedge_efficiency=premium_efficiency,
        current_hedge_premium_market_value=current_hedge_premium_market_value,
        current_hedge_premium_cost_basis=current_hedge_premium_cost_basis,

        hedge_unrealized_pnl=float(effectiveness["hedge_unrealized_pnl"]),
        hedge_cost_drag_dollars=float(effectiveness["hedge_cost_drag_dollars"]),
        hedge_cost_drag_pct=float(effectiveness["hedge_cost_drag_pct"]),
        hedge_protection_capacity_dollars=float(
            effectiveness["hedge_protection_capacity_dollars"]
        ),
        hedge_protection_capacity_pct=float(
            effectiveness["hedge_protection_capacity_pct"]
        ),
        hedge_marked_benefit_dollars=float(
            effectiveness["hedge_marked_benefit_dollars"]
        ),
        hedge_marked_benefit_pct=float(effectiveness["hedge_marked_benefit_pct"]),
        hedge_capacity_ratio=float(effectiveness["hedge_capacity_ratio"]),
        hedged_beta_estimate=float(effectiveness["hedged_beta_estimate"]),
        unhedged_beta_estimate=float(effectiveness["unhedged_beta_estimate"]),
        vix_level=vix_level,

        hedge_budget_pct=hedge_budget_pct,
        hedge_budget_dollars=hedge_budget_dollars,
        remaining_hedge_budget_dollars=remaining_hedge_budget_dollars,
        remaining_hedge_budget_pct=remaining_hedge_budget_pct,

        reasons=regime_resp.reasons,
        insights=insights,

        convex_budget_dollars=convex_budget_dollars,
        convex_budget_pct_of_hedge_budget=convex_budget_pct_of_hedge_budget,

        factor_exposures=[
            {
                "factor": r.factor,
                "gross_exposure_dollars": r.gross_exposure_dollars,
                "exposure_pct": r.exposure_pct,
                "threshold_pct": r.threshold_pct,
                "excess_pct": r.excess_pct,
                "hedge_proxy": r.hedge_proxy,
                "routing_action": r.routing_action,
                "routing_reason": r.routing_reason,
            }
            for r in factor_rows
        ],
        factor_budget_allocations=factor_budget_allocations,
        factor_structure_allocations=factor_structure_allocations,

        unmapped_exposures=unmapped_exposures,
        unmapped_exposure_dollars=unmapped_exposure_dollars,

        hedge_source_breakdown={
            "composer": composer_source,
            "alpaca": alpaca_summary,
        },
    )