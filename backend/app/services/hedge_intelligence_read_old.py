from __future__ import annotations

from typing import Any, List, Optional

from sqlalchemy.orm import Session

from app.schemas import HedgeIntelligenceResponse, HedgeSourceBreakdown, HedgeSourceContribution
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

import logging

logger = logging.getLogger(__name__)


BASE_HEDGE_PCT_MAP = {
    "strong_bull": 0.07,
    "extended_bull": 0.18,
    "early_breakdown": 0.30,
    "high_crash_risk": 0.45,
    "localized_bubble": 0.12,
    "neutral": 0.15,
}

HEDGE_BUDGET_MAP = {
    "strong_bull": 0.0125,
    "extended_bull": 0.0250,
    "early_breakdown": 0.0350,
    "high_crash_risk": 0.0500,
    "localized_bubble": 0.0100,
    "neutral": 0.0150,
}

STRUCTURAL_HEDGE_SYMBOLS = {
    "PSQ",
    "SQQQ",
    "SOXS",
    "UVXY",
    "VIXM",
    "TMV",
    "EUM",
}


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


def compute_recommended_hedge_pct(
    regime: str,
    portfolio_crash_beta: float,
    portfolio_volatility_beta: float,
    portfolio_liquidity_beta: float,
    vix_level: float,
) -> float:
    rec = BASE_HEDGE_PCT_MAP.get(regime, 0.20)

    if portfolio_crash_beta > 1.5:
        rec += 0.10

    if portfolio_volatility_beta < -1.0:
        rec += 0.05

    if portfolio_liquidity_beta > 1.2:
        rec += 0.05

    rec = max(0.0, min(rec, 1.0))

    if vix_level >= 30:
        rec = min(rec, BASE_HEDGE_PCT_MAP.get(regime, 0.20) + 0.10)

    return max(0.0, min(rec, 1.0))


def compute_additional_hedge_pct(
    current_hedge_pct: float,
    recommended_hedge_pct: float,
) -> float:
    return max(recommended_hedge_pct - current_hedge_pct, 0.0)


def compute_exposure_metrics_from_beta_rows(rows, portfolio_value: float):
    if portfolio_value <= 0:
        return 0.0, 0.0, 0.0, 0.0

    long_exposure = sum(
        float(_get_field(row, "dollar_beta_exposure", 0.0) or 0.0)
        for row in rows
        if float(_get_field(row, "dollar_beta_exposure", 0.0) or 0.0) > 0
    )

    short_exposure = sum(
        abs(float(_get_field(row, "dollar_beta_exposure", 0.0) or 0.0))
        for row in rows
        if float(_get_field(row, "dollar_beta_exposure", 0.0) or 0.0) < 0
    )

    gross_long_exposure_pct = long_exposure / portfolio_value
    gross_short_exposure_pct = short_exposure / portfolio_value
    net_exposure_pct = (long_exposure - short_exposure) / portfolio_value
    net_beta_exposure_pct = net_exposure_pct

    return (
        gross_long_exposure_pct,
        gross_short_exposure_pct,
        net_exposure_pct,
        net_beta_exposure_pct,
    )


def compute_hedge_effectiveness_metrics(
    portfolio_value: float,
    current_hedge_exposure_dollars: float,
    current_hedge_premium_market_value: float,
    current_hedge_premium_cost_basis: float,
    portfolio_beta: float,
    gross_long_exposure_pct: float,
):
    hedge_unrealized_pnl = (
        current_hedge_premium_market_value - current_hedge_premium_cost_basis
    )

    hedge_cost_drag_dollars = max(
        current_hedge_premium_cost_basis - current_hedge_premium_market_value,
        0.0,
    )
    hedge_cost_drag_pct = (
        hedge_cost_drag_dollars / portfolio_value if portfolio_value > 0 else 0.0
    )

    hedge_protection_capacity_dollars = current_hedge_exposure_dollars
    hedge_protection_capacity_pct = (
        hedge_protection_capacity_dollars / portfolio_value if portfolio_value > 0 else 0.0
    )

    hedge_marked_benefit_dollars = hedge_unrealized_pnl
    hedge_marked_benefit_pct = (
        hedge_marked_benefit_dollars / portfolio_value if portfolio_value > 0 else 0.0
    )

    hedge_capacity_ratio = (
        hedge_protection_capacity_dollars / hedge_cost_drag_dollars
        if hedge_cost_drag_dollars > 0
        else 0.0
    )

    hedged_beta_estimate = portfolio_beta
    unhedged_beta_estimate = gross_long_exposure_pct

    return {
        "hedge_unrealized_pnl": hedge_unrealized_pnl,
        "hedge_cost_drag_dollars": hedge_cost_drag_dollars,
        "hedge_cost_drag_pct": hedge_cost_drag_pct,
        "hedge_protection_capacity_dollars": hedge_protection_capacity_dollars,
        "hedge_protection_capacity_pct": hedge_protection_capacity_pct,
        "hedge_marked_benefit_dollars": hedge_marked_benefit_dollars,
        "hedge_marked_benefit_pct": hedge_marked_benefit_pct,
        "hedge_capacity_ratio": hedge_capacity_ratio,
        "hedged_beta_estimate": hedged_beta_estimate,
        "unhedged_beta_estimate": unhedged_beta_estimate,
    }


def decompose_current_hedges(
    holdings: list,
    beta_rows: list,
    portfolio_value: float,
) -> dict:
    beta_by_symbol = {}
    for row in beta_rows:
        symbol = _get_field(row, "symbol", None)
        if symbol:
            beta_by_symbol[symbol] = row

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
                    "OPTION HEDGE symbol=%s qty=%s current_price=%s market_value=%s total_cost_basis=%s delta_dollars=%s option_negative_exposure=%s",
                    metrics.symbol,
                    metrics.quantity,
                    metrics.current_price,
                    metrics.current_market_value,
                    metrics.total_cost_basis,
                    metrics.delta_dollars,
                    option_negative_exposure,
                )

                option_hedge_exposure_dollars += option_negative_exposure
                current_hedge_premium_market_value += metrics.current_market_value

                if metrics.total_cost_basis > 0:
                    current_hedge_premium_cost_basis += metrics.total_cost_basis
                else:
                    current_hedge_premium_cost_basis += metrics.current_market_value

            except Exception as e:
                logger.warning("OPTION HEDGE metrics failed for %s: %s", symbol, e)

    current_hedge_premium_cost = current_hedge_premium_cost_basis

    total_current_hedge_exposure_dollars = (
        structural_hedge_exposure_dollars + option_hedge_exposure_dollars
    )

    structural_hedge_exposure_pct = (
        structural_hedge_exposure_dollars / portfolio_value if portfolio_value > 0 else 0.0
    )
    option_hedge_exposure_pct = (
        option_hedge_exposure_dollars / portfolio_value if portfolio_value > 0 else 0.0
    )
    current_hedge_pct = (
        total_current_hedge_exposure_dollars / portfolio_value if portfolio_value > 0 else 0.0
    )

    current_hedge_premium_cost_pct = (
        current_hedge_premium_cost / portfolio_value if portfolio_value > 0 else 0.0
    )

    structural_hedge_efficiency = (
        structural_hedge_exposure_dollars / structural_hedge_capital_dollars
        if structural_hedge_capital_dollars > 0
        else 0.0
    )

    premium_hedge_efficiency = (
        option_hedge_exposure_dollars / current_hedge_premium_cost
        if current_hedge_premium_cost > 0
        else 0.0
    )

    return {
        "current_hedge_pct": current_hedge_pct,
        "current_hedge_exposure_dollars": total_current_hedge_exposure_dollars,
        "structural_hedge_exposure_dollars": structural_hedge_exposure_dollars,
        "structural_hedge_exposure_pct": structural_hedge_exposure_pct,
        "structural_hedge_capital_dollars": structural_hedge_capital_dollars,
        "structural_hedge_efficiency": structural_hedge_efficiency,
        "option_hedge_exposure_dollars": option_hedge_exposure_dollars,
        "option_hedge_exposure_pct": option_hedge_exposure_pct,
        "current_hedge_premium_cost": current_hedge_premium_cost,
        "current_hedge_premium_cost_pct": current_hedge_premium_cost_pct,
        "current_hedge_premium_market_value": current_hedge_premium_market_value,
        "current_hedge_premium_cost_basis": current_hedge_premium_cost_basis,
        "premium_hedge_efficiency": premium_hedge_efficiency,
    }


def build_hedge_insights(
    regime: str,
    current_hedge_pct: float,
    recommended_hedge_pct: float,
    current_hedge_premium_cost_pct: float,
    remaining_hedge_budget_pct: float,
    portfolio_crash_beta: float,
    portfolio_volatility_beta: float,
    vix_level: float,
) -> list[str]:
    insights = []

    if regime == "strong_bull":
        insights.append("Market trend is strong; only modest crash protection is justified.")
    elif regime == "extended_bull":
        insights.append("Market is extended; partial protection is justified before volatility becomes expensive.")
    elif regime == "early_breakdown":
        insights.append("Market momentum is weakening; directional downside protection is warranted.")
    elif regime == "high_crash_risk":
        insights.append("Crash risk is elevated; focus on maintaining meaningful protection.")
    elif regime == "localized_bubble":
        insights.append("A localized bubble regime suggests selective, targeted hedging rather than broad portfolio protection.")

    if portfolio_crash_beta > 1.5:
        insights.append("Portfolio crash beta is elevated relative to normal beta.")
    if portfolio_volatility_beta < -1.0:
        insights.append("Portfolio is vulnerable to volatility expansion.")
    if vix_level >= 30:
        insights.append("Protection is expensive here; prioritize spreads and selective additions.")

    gap = recommended_hedge_pct - current_hedge_pct
    if gap > 0.15:
        insights.append("Current protection is materially below the recommended hedge level.")
    elif gap <= 0.05:
        insights.append("Current protection is near the recommended hedge level.")

    if current_hedge_premium_cost_pct < 0.01:
        insights.append("Most current hedge appears structural rather than premium-based.")
    if remaining_hedge_budget_pct > 0.01:
        insights.append("There is still meaningful option premium budget available.")

    return insights


def _summarize_alpaca_hedge_positions(alpaca_positions):
    structural_exposure = 0.0
    option_exposure = 0.0
    premium_market_value = 0.0
    premium_cost_basis = 0.0
    premium_cost = 0.0

    symbols = []
    option_positions_count = 0

    for p in alpaca_positions:
        symbol = str(getattr(p, "symbol", "") or "")
        if symbol:
            symbols.append(symbol)

        option_positions_count += 1

        dd = float(getattr(p, "delta_dollars", 0.0) or 0.0)
        hedge_exposure = max(-dd, 0.0)
        option_exposure += hedge_exposure

        market_value = float(getattr(p, "market_value", 0.0) or 0.0)
        total_cost_basis = float(getattr(p, "total_cost_basis", 0.0) or 0.0)

        premium_market_value += market_value
        premium_cost_basis += total_cost_basis
        premium_cost += total_cost_basis

    return {
        "source": "alpaca",
        "positions_count": option_positions_count,
        "option_positions_count": option_positions_count,
        "symbols": symbols,
        "structural_hedge_exposure_dollars": structural_exposure,
        "option_hedge_exposure_dollars": option_exposure,
        "current_hedge_exposure_dollars": structural_exposure + option_exposure,
        "current_hedge_premium_cost": premium_cost,
        "current_hedge_premium_market_value": premium_market_value,
        "current_hedge_premium_cost_basis": premium_cost_basis,
    }

def _build_composer_hedge_source_breakdown(hedge_breakdown: dict, holdings: list):
    option_symbols = []
    structural_symbols = []

    for h in holdings:
        symbol = str(_get_field(h, "symbol", "") or "")
        if not symbol:
            continue
        if _is_structural_hedge(symbol):
            structural_symbols.append(symbol)
        elif is_option_symbol(symbol):
            option_symbols.append(symbol)

    all_symbols = structural_symbols + option_symbols

    return {
        "source": "composer",
        "positions_count": len(all_symbols),
        "option_positions_count": len(option_symbols),
        "symbols": all_symbols,
        "structural_hedge_exposure_dollars": float(
            hedge_breakdown["structural_hedge_exposure_dollars"]
        ),
        "option_hedge_exposure_dollars": float(
            hedge_breakdown["option_hedge_exposure_dollars"]
        ),
        "current_hedge_exposure_dollars": float(
            hedge_breakdown["current_hedge_exposure_dollars"]
        ),
        "current_hedge_premium_cost": float(
            hedge_breakdown["current_hedge_premium_cost"]
        ),
        "current_hedge_premium_market_value": float(
            hedge_breakdown.get("current_hedge_premium_market_value", 0.0)
        ),
        "current_hedge_premium_cost_basis": float(
            hedge_breakdown.get(
                "current_hedge_premium_cost_basis",
                hedge_breakdown["current_hedge_premium_cost"],
            )
        ),
    }


def get_hedge_intelligence_data(
    db: Session,
    account_ids: List[str],
    target_date: Optional[str] = None,
) -> HedgeIntelligenceResponse:
    signals = get_market_regime_signals(db=db, target_date=target_date)
    regime_resp = classify_market_regime(signals)

    beta_resp = get_portfolio_beta_data(
        db=db,
        account_ids=account_ids,
        target_date=target_date,
    )

    holdings_resp = get_portfolio_holdings_data(
        db=db,
        account_ids=account_ids,
        target_date=target_date,
        get_client_for_account_fn=get_client_for_account,
    )

    holdings = _get_field(holdings_resp, "holdings", [])
    holdings_date = _get_field(holdings_resp, "date", None)

    portfolio_value = float(_get_field(beta_resp, "portfolio_value", 0.0) or 0.0)
    portfolio_beta = float(_get_field(beta_resp, "portfolio_beta", 0.0) or 0.0)
    portfolio_dollar_beta = float(_get_field(beta_resp, "portfolio_dollar_beta", 0.0) or 0.0)
    beta_rows = _get_field(beta_resp, "rows", []) or []

    portfolio_crash_beta = max(portfolio_beta * 1.35, portfolio_beta)
    portfolio_volatility_beta = -abs(portfolio_beta * 0.9)
    portfolio_liquidity_beta = max(portfolio_beta * 1.1, portfolio_beta)

    gross_long_exposure_pct, gross_short_exposure_pct, net_exposure_pct, net_beta_exposure_pct = (
        compute_exposure_metrics_from_beta_rows(beta_rows, portfolio_value)
    )

    hedge_breakdown = decompose_current_hedges(
        holdings=holdings,
        beta_rows=beta_rows,
        portfolio_value=portfolio_value,
    )
    composer_source_breakdown = _build_composer_hedge_source_breakdown(
        hedge_breakdown=hedge_breakdown,
        holdings=holdings,
    )

    current_hedge_pct = float(hedge_breakdown["current_hedge_pct"])
    current_hedge_exposure_dollars = float(hedge_breakdown["current_hedge_exposure_dollars"])

    structural_hedge_exposure_dollars = float(hedge_breakdown["structural_hedge_exposure_dollars"])
    structural_hedge_exposure_pct = float(hedge_breakdown["structural_hedge_exposure_pct"])
    structural_hedge_capital_dollars = float(hedge_breakdown["structural_hedge_capital_dollars"])
    structural_hedge_efficiency = float(hedge_breakdown["structural_hedge_efficiency"])

    option_hedge_exposure_dollars = float(hedge_breakdown["option_hedge_exposure_dollars"])
    option_hedge_exposure_pct = float(hedge_breakdown["option_hedge_exposure_pct"])
    current_hedge_premium_cost = float(hedge_breakdown["current_hedge_premium_cost"])
    current_hedge_premium_cost_pct = float(hedge_breakdown["current_hedge_premium_cost_pct"])
    current_hedge_premium_market_value = float(
        hedge_breakdown.get("current_hedge_premium_market_value", 0.0)
    )
    current_hedge_premium_cost_basis = float(
        hedge_breakdown.get("current_hedge_premium_cost_basis", current_hedge_premium_cost)
    )
    premium_hedge_efficiency = float(hedge_breakdown["premium_hedge_efficiency"])

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

    structural_hedge_exposure_dollars += float(
        alpaca_summary["structural_hedge_exposure_dollars"]
    )
    option_hedge_exposure_dollars += float(
        alpaca_summary["option_hedge_exposure_dollars"]
    )
    current_hedge_premium_market_value += float(
        alpaca_summary["current_hedge_premium_market_value"]
    )
    current_hedge_premium_cost_basis += float(
        alpaca_summary["current_hedge_premium_cost_basis"]
    )
    current_hedge_premium_cost += float(
        alpaca_summary["current_hedge_premium_cost"]
    )

    current_hedge_exposure_dollars = (
        structural_hedge_exposure_dollars + option_hedge_exposure_dollars
    )

    if portfolio_value > 0:
        current_hedge_pct = current_hedge_exposure_dollars / portfolio_value
        structural_hedge_exposure_pct = structural_hedge_exposure_dollars / portfolio_value
        option_hedge_exposure_pct = option_hedge_exposure_dollars / portfolio_value
        current_hedge_premium_cost_pct = current_hedge_premium_cost / portfolio_value
        premium_hedge_efficiency = (
            option_hedge_exposure_dollars / current_hedge_premium_cost
            if current_hedge_premium_cost > 0
            else 0.0
        )
    else:
        current_hedge_pct = 0.0
        structural_hedge_exposure_pct = 0.0
        option_hedge_exposure_pct = 0.0
        current_hedge_premium_cost_pct = 0.0
        premium_hedge_efficiency = 0.0

    effectiveness = compute_hedge_effectiveness_metrics(
        portfolio_value=portfolio_value,
        current_hedge_exposure_dollars=current_hedge_exposure_dollars,
        current_hedge_premium_market_value=current_hedge_premium_market_value,
        current_hedge_premium_cost_basis=current_hedge_premium_cost_basis,
        portfolio_beta=portfolio_beta,
        gross_long_exposure_pct=gross_long_exposure_pct,
    )

    hedge_unrealized_pnl = float(effectiveness["hedge_unrealized_pnl"])
    hedge_cost_drag_dollars = float(effectiveness["hedge_cost_drag_dollars"])
    hedge_cost_drag_pct = float(effectiveness["hedge_cost_drag_pct"])
    hedge_protection_capacity_dollars = float(effectiveness["hedge_protection_capacity_dollars"])
    hedge_protection_capacity_pct = float(effectiveness["hedge_protection_capacity_pct"])
    hedge_marked_benefit_dollars = float(effectiveness["hedge_marked_benefit_dollars"])
    hedge_marked_benefit_pct = float(effectiveness["hedge_marked_benefit_pct"])
    hedge_capacity_ratio = float(effectiveness["hedge_capacity_ratio"])
    hedged_beta_estimate = float(effectiveness["hedged_beta_estimate"])
    unhedged_beta_estimate = float(effectiveness["unhedged_beta_estimate"])

    recommended_hedge_pct = compute_recommended_hedge_pct(
        regime=regime_resp.regime,
        portfolio_crash_beta=portfolio_crash_beta,
        portfolio_volatility_beta=portfolio_volatility_beta,
        portfolio_liquidity_beta=portfolio_liquidity_beta,
        vix_level=float(signals.get("vix_level", 20.0) or 20.0),
    )

    additional_hedge_pct = compute_additional_hedge_pct(
        current_hedge_pct=current_hedge_pct,
        recommended_hedge_pct=recommended_hedge_pct,
    )

    recommended_hedge_exposure_dollars = portfolio_value * recommended_hedge_pct
    additional_hedge_exposure_dollars = portfolio_value * additional_hedge_pct

    hedge_budget_pct = HEDGE_BUDGET_MAP.get(regime_resp.regime, 0.015)
    hedge_budget_dollars = portfolio_value * hedge_budget_pct

    remaining_hedge_budget_dollars = max(
        hedge_budget_dollars - current_hedge_premium_cost,
        0.0,
    )
    remaining_hedge_budget_pct = max(
        hedge_budget_pct - current_hedge_premium_cost_pct,
        0.0,
    )

    insights = build_hedge_insights(
        regime=regime_resp.regime,
        current_hedge_pct=current_hedge_pct,
        recommended_hedge_pct=recommended_hedge_pct,
        current_hedge_premium_cost_pct=current_hedge_premium_cost_pct,
        remaining_hedge_budget_pct=remaining_hedge_budget_pct,
        portfolio_crash_beta=portfolio_crash_beta,
        portfolio_volatility_beta=portfolio_volatility_beta,
        vix_level=float(signals.get("vix_level", 20.0) or 20.0),
    )

    as_of_date = (
        signals.get("as_of_date")
        or _get_field(beta_resp, "date", None)
        or holdings_date
        or str(target_date or "")
    )

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
        gross_long_exposure_pct=gross_long_exposure_pct,
        gross_short_exposure_pct=gross_short_exposure_pct,
        net_exposure_pct=net_exposure_pct,
        net_beta_exposure_pct=net_beta_exposure_pct,
        current_hedge_pct=current_hedge_pct,
        recommended_hedge_pct=recommended_hedge_pct,
        additional_hedge_pct=additional_hedge_pct,
        current_hedge_exposure_dollars=current_hedge_exposure_dollars,
        recommended_hedge_exposure_dollars=recommended_hedge_exposure_dollars,
        additional_hedge_exposure_dollars=additional_hedge_exposure_dollars,
        structural_hedge_exposure_dollars=structural_hedge_exposure_dollars,
        structural_hedge_exposure_pct=structural_hedge_exposure_pct,
        structural_hedge_capital_dollars=structural_hedge_capital_dollars,
        structural_hedge_efficiency=structural_hedge_efficiency,
        option_hedge_exposure_dollars=option_hedge_exposure_dollars,
        option_hedge_exposure_pct=option_hedge_exposure_pct,
        current_hedge_premium_cost=current_hedge_premium_cost,
        current_hedge_premium_cost_pct=current_hedge_premium_cost_pct,
        current_hedge_premium_market_value=current_hedge_premium_market_value,
        current_hedge_premium_cost_basis=current_hedge_premium_cost_basis,
        premium_hedge_efficiency=premium_hedge_efficiency,
        hedge_unrealized_pnl=hedge_unrealized_pnl,
        hedge_cost_drag_dollars=hedge_cost_drag_dollars,
        hedge_cost_drag_pct=hedge_cost_drag_pct,
        hedge_protection_capacity_dollars=hedge_protection_capacity_dollars,
        hedge_protection_capacity_pct=hedge_protection_capacity_pct,
        hedge_marked_benefit_dollars=hedge_marked_benefit_dollars,
        hedge_marked_benefit_pct=hedge_marked_benefit_pct,
        hedge_capacity_ratio=hedge_capacity_ratio,
        hedged_beta_estimate=hedged_beta_estimate,
        unhedged_beta_estimate=unhedged_beta_estimate,
        hedge_budget_pct=hedge_budget_pct,
        hedge_budget_dollars=hedge_budget_dollars,
        remaining_hedge_budget_dollars=remaining_hedge_budget_dollars,
        remaining_hedge_budget_pct=remaining_hedge_budget_pct,
        reasons=regime_resp.reasons,
        insights=insights,
        hedge_source_breakdown={
            "composer": composer_source_breakdown,
            "alpaca": alpaca_summary,
        },
        vix_level=float(signals.get("vix_level", 20.0) or 20.0),
    )