from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional

from app.services.hedge_intelligence_read import get_hedge_intelligence_data
from app.services.option_selector import select_hedge_spreads
from app.services.hedge_execution_planner import build_hedge_execution_plan
from app.services.hedge_roll_engine import build_hedge_roll_engine
from app.services.hedge_reconciliation_engine import build_hedge_reconciliation_engine
from app.services.hedge_trade_ticket_engine import build_hedge_trade_tickets
from app.services.hedge_history_read import get_hedge_history_data
from app.services.finnhub_market_data import get_latest_price
from app.services.crash_simulation_engine import run_crash_simulation


def _auto_hedge_style(market_regime: str) -> str:
    if market_regime == "strong_bull":
        return "cost_sensitive"
    if market_regime == "extended_bull":
        return "balanced"
    if market_regime == "early_breakdown":
        return "correction_focused"
    if market_regime == "high_crash_risk":
        return "crash_paranoid"
    if market_regime == "localized_bubble":
        return "balanced"
    return "balanced"


def _parse_scenarios_pct(scenarios: Optional[str]) -> Optional[list[float]]:
    if not scenarios:
        return None
    try:
        return [float(s.strip()) / 100.0 for s in scenarios.split(",") if s.strip()]
    except ValueError:
        return None


def _to_dict(obj):
    if obj is None:
        return None
    if hasattr(obj, "model_dump"):
        return obj.model_dump()
    if hasattr(obj, "dict"):
        return obj.dict()
    return obj


def _get_attr_or_key(obj, key, default=None):
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _build_crash_sim_from_hedge_intel(
    hedge_intel,
    scenarios_pct: Optional[list[float]] = None,
):
    current_pct = max(
        float(_get_attr_or_key(hedge_intel, "current_hedge_pct", 0.01) or 0.01),
        0.01,
    )
    recommended_pct = float(
        _get_attr_or_key(hedge_intel, "recommended_hedge_pct", current_pct) or current_pct
    )

    structural = float(
        _get_attr_or_key(hedge_intel, "structural_hedge_exposure_dollars", 0.0) or 0.0
    )
    options = float(
        _get_attr_or_key(hedge_intel, "option_hedge_exposure_dollars", 0.0) or 0.0
    )

    portfolio_value = float(_get_attr_or_key(hedge_intel, "portfolio_value", 0.0) or 0.0)
    portfolio_beta = float(_get_attr_or_key(hedge_intel, "portfolio_beta", 0.0) or 0.0)
    portfolio_crash_beta = float(
        _get_attr_or_key(hedge_intel, "portfolio_crash_beta", 0.0) or 0.0
    )
    as_of_date = _get_attr_or_key(hedge_intel, "as_of_date", "")
    market_regime = _get_attr_or_key(hedge_intel, "market_regime", "")

    scale = recommended_pct / current_pct
    fully_hedged_structural = structural * scale
    fully_hedged_options = options * scale

    sim_current = run_crash_simulation(
        portfolio_value=portfolio_value,
        portfolio_beta=portfolio_beta,
        portfolio_crash_beta=portfolio_crash_beta,
        structural_hedge_exposure_dollars=structural,
        option_hedge_exposure_dollars=options,
        scenarios_pct=scenarios_pct,
    )

    sim_full = run_crash_simulation(
        portfolio_value=portfolio_value,
        portfolio_beta=portfolio_beta,
        portfolio_crash_beta=portfolio_crash_beta,
        structural_hedge_exposure_dollars=fully_hedged_structural,
        option_hedge_exposure_dollars=fully_hedged_options,
        scenarios_pct=scenarios_pct,
    )

    def _rows(sim):
        return [
            {
                "drop_pct": s.drop_pct,
                "drop_label": s.drop_label,
                "portfolio_loss_dollars": s.portfolio_loss_dollars,
                "structural_gain_dollars": s.structural_gain_dollars,
                "option_gain_dollars": s.option_gain_dollars,
                "total_hedge_gain_dollars": s.total_hedge_gain_dollars,
                "net_dollars": s.net_dollars,
                "hedge_offset_pct": s.hedge_offset_pct,
                "structural_decay_factor": s.structural_decay_factor,
                "option_convexity_factor": s.option_convexity_factor,
            }
            for s in sim.scenarios
        ]

    return {
        "as_of_date": as_of_date,
        "market_regime": market_regime,
        "portfolio_value": sim_current.portfolio_value,
        "portfolio_beta": sim_current.portfolio_beta,
        "portfolio_crash_beta": sim_current.portfolio_crash_beta,
        "portfolio_crash_beta_dollars": sim_current.portfolio_crash_beta_dollars,
        "structural_hedge_exposure_dollars": structural,
        "option_hedge_exposure_dollars": options,
        "total_hedge_exposure_dollars": structural + options,
        "current_hedge_pct": current_pct,
        "recommended_hedge_pct": recommended_pct,
        "fully_hedged_structural_dollars": fully_hedged_structural,
        "fully_hedged_option_dollars": fully_hedged_options,
        "scenarios": _rows(sim_current),
        "scenarios_fully_hedged": _rows(sim_full),
        "notes": list(getattr(sim_current, "notes", []) or []) + [
            f"scenarios_fully_hedged assumes hedge scaled from {current_pct*100:.1f}% to {recommended_pct*100:.1f}%"
        ],
    }


def build_hedge_dashboard_bundle(
    *,
    db,
    account_ids: list[str],
    target_date: Optional[str] = None,
    hedge_style: Optional[str] = None,
    scenarios: Optional[str] = None,
):
    snapshot_id = datetime.utcnow().isoformat(timespec="seconds") + "Z"

    hedge = get_hedge_intelligence_data(
        db=db,
        account_ids=account_ids,
        target_date=target_date,
    )

    resolved_hedge_style = hedge_style or _auto_hedge_style(hedge.market_regime)
    qqq_spot = get_latest_price("QQQ")
    scenarios_pct = _parse_scenarios_pct(scenarios)

    select = select_hedge_spreads(
        as_of_date=hedge.as_of_date,
        underlying="QQQ",
        market_regime=hedge.market_regime,
        hedge_style=resolved_hedge_style,
        underlying_price=qqq_spot,
    )

    plan = build_hedge_execution_plan(
        as_of_date=hedge.as_of_date,
        underlying="QQQ",
        market_regime=hedge.market_regime,
        hedge_style=resolved_hedge_style,
        portfolio_value=hedge.portfolio_value,
        recommended_hedge_pct=hedge.recommended_hedge_pct,
        additional_hedge_pct=hedge.additional_hedge_pct,
        remaining_hedge_budget_pct=hedge.remaining_hedge_budget_pct,
        vix_level=float(getattr(hedge, "vix_level", 20.0) or 20.0),
        underlying_price=qqq_spot,
    )

    hedge_dict = _to_dict(hedge)
    plan_dict = _to_dict(plan)

    theoretical_recommended_hedge_exposure_dollars = float(
        hedge_dict.get("recommended_hedge_exposure_dollars", 0.0) or 0.0
    )
    theoretical_recommended_hedge_pct = float(
        hedge_dict.get("recommended_hedge_pct", 0.0) or 0.0
    )
    theoretical_additional_hedge_exposure_dollars = float(
        hedge_dict.get("additional_hedge_exposure_dollars", 0.0) or 0.0
    )
    theoretical_additional_hedge_pct = float(
        hedge_dict.get("additional_hedge_pct", 0.0) or 0.0
    )

    practical_recommended_hedge_exposure_dollars = float(
        plan_dict.get("total_estimated_hedge_dollars", 0.0) or 0.0
    )
    practical_recommended_hedge_pct = (
        practical_recommended_hedge_exposure_dollars / float(hedge.portfolio_value)
        if float(hedge.portfolio_value or 0.0) > 0
        else 0.0
    )

    practical_additional_hedge_exposure_dollars = max(
        practical_recommended_hedge_exposure_dollars
        - float(hedge.current_hedge_exposure_dollars or 0.0),
        0.0,
    )
    practical_additional_hedge_pct = (
        practical_additional_hedge_exposure_dollars / float(hedge.portfolio_value)
        if float(hedge.portfolio_value or 0.0) > 0
        else 0.0
    )

    hedge_dict["theoretical_recommended_hedge_exposure_dollars"] = (
        theoretical_recommended_hedge_exposure_dollars
    )
    hedge_dict["theoretical_recommended_hedge_pct"] = theoretical_recommended_hedge_pct
    hedge_dict["theoretical_additional_hedge_exposure_dollars"] = (
        theoretical_additional_hedge_exposure_dollars
    )
    hedge_dict["theoretical_additional_hedge_pct"] = theoretical_additional_hedge_pct

    hedge_dict["practical_recommended_hedge_exposure_dollars"] = (
        practical_recommended_hedge_exposure_dollars
    )
    hedge_dict["practical_recommended_hedge_pct"] = practical_recommended_hedge_pct
    hedge_dict["practical_additional_hedge_exposure_dollars"] = (
        practical_additional_hedge_exposure_dollars
    )
    hedge_dict["practical_additional_hedge_pct"] = practical_additional_hedge_pct

    # compatibility aliases
    hedge_dict["recommended_hedge_exposure_dollars"] = (
        practical_recommended_hedge_exposure_dollars
    )
    hedge_dict["recommended_hedge_pct"] = practical_recommended_hedge_pct
    hedge_dict["additional_hedge_exposure_dollars"] = (
        practical_additional_hedge_exposure_dollars
    )
    hedge_dict["additional_hedge_pct"] = practical_additional_hedge_pct

    reconcile = build_hedge_reconciliation_engine(
        db=db,
        account_ids=account_ids,
        as_of_date=hedge.as_of_date,
        underlying="QQQ",
        market_regime=hedge.market_regime,
        hedge_style=resolved_hedge_style,
        portfolio_value=hedge.portfolio_value,
        current_hedge_pct=hedge.current_hedge_pct,
        recommended_hedge_pct=practical_recommended_hedge_pct,
        additional_hedge_pct=practical_additional_hedge_pct,
        remaining_hedge_budget_pct=hedge.remaining_hedge_budget_pct,
        vix_level=float(getattr(hedge, "vix_level", 20.0) or 20.0),
        spot_price=qqq_spot,
    )

    roll = build_hedge_roll_engine(
        as_of_date=hedge.as_of_date,
        underlying="QQQ",
        market_regime=hedge.market_regime,
        hedge_style=resolved_hedge_style,
        portfolio_value=hedge.portfolio_value,
        current_hedge_pct=hedge.current_hedge_pct,
        recommended_hedge_pct=practical_recommended_hedge_pct,
        additional_hedge_pct=practical_additional_hedge_pct,
        remaining_hedge_budget_pct=hedge.remaining_hedge_budget_pct,
        vix_level=float(getattr(hedge, "vix_level", 20.0) or 20.0),
        underlying_price=qqq_spot,
        prebuilt_plan=plan,
    )

    tickets_preview = build_hedge_trade_tickets(
        db=db,
        account_ids=account_ids,
        as_of_date=hedge.as_of_date,
        underlying="QQQ",
        market_regime=hedge.market_regime,
        hedge_style=resolved_hedge_style,
        portfolio_value=hedge.portfolio_value,
        current_hedge_pct=hedge.current_hedge_pct,
        recommended_hedge_pct=practical_recommended_hedge_pct,
        additional_hedge_pct=practical_additional_hedge_pct,
        remaining_hedge_budget_pct=hedge.remaining_hedge_budget_pct,
        vix_level=float(getattr(hedge, "vix_level", 20.0) or 20.0),
        underlying_price=qqq_spot,
        prebuilt_plan=plan,
        prereconciled=reconcile,
    )

    crash_sim = _build_crash_sim_from_hedge_intel(
        hedge_dict,
        scenarios_pct=scenarios_pct,
    )

    end_date = hedge.as_of_date
    start_date = (
        datetime.fromisoformat(end_date) - timedelta(days=30)
    ).date().isoformat()

    history_30d = get_hedge_history_data(
        db=db,
        account_ids=account_ids,
        start_date=start_date,
        end_date=end_date,
    )

    return {
        "snapshot_id": snapshot_id,
        "as_of_date": hedge.as_of_date,
        "context": {
            "account_ids": account_ids,
            "underlying": "QQQ",
            "hedge_style": resolved_hedge_style,
            "qqq_spot": qqq_spot,
            "market_regime": hedge.market_regime,
            "market_risk_score": hedge.market_risk_score,
            "vix_level": getattr(hedge, "vix_level", None),
        },
        "hedge_intelligence": hedge_dict,
        "crash_sim": crash_sim,
        "select": _to_dict(select),
        "plan": plan_dict,
        "reconcile": _to_dict(reconcile),
        "roll": _to_dict(roll),
        "tickets_preview": _to_dict(tickets_preview),
        "history_30d": _to_dict(history_30d),
    }