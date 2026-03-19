from __future__ import annotations

from datetime import date
from typing import List

from app.schemas import (
    HedgeTradeLeg,
    HedgeTradeTicket,
    HedgeTradeTicketResponse,
)
from app.services.finnhub_market_data import get_latest_price
from app.services.hedge_efficiency_optimizer import evaluate_hedge_efficiency
from app.services.hedge_execution_planner import build_hedge_execution_plan
from app.services.hedge_reconciliation_engine import build_hedge_reconciliation_engine


def _safe_float(value, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except Exception:
        return default


def _safe_dte(as_of_date: str, expiry: str | None) -> int | None:
    if not expiry:
        return None
    try:
        return (date.fromisoformat(expiry) - date.fromisoformat(as_of_date)).days
    except Exception:
        return None


def _max_contracts_by_budget(*, budget_remaining: float, spread_plan) -> int:
    debit_per_contract = _safe_float(
        getattr(spread_plan, "debit_per_contract", 0.0),
        0.0,
    )
    if debit_per_contract <= 0 or budget_remaining <= 0:
        return 0
    return int(budget_remaining // debit_per_contract)


def _ticket_contracts_from_gap(
    *,
    exposure_gap_dollars: float,
    spread_plan,
    budget_remaining: float,
):
    max_payoff_per_contract = _safe_float(
        getattr(spread_plan, "max_payoff_per_contract", 0.0),
        0.0,
    )
    plan_contracts = int(_safe_float(getattr(spread_plan, "contracts", 0), 0))
    budget_contracts = _max_contracts_by_budget(
        budget_remaining=budget_remaining,
        spread_plan=spread_plan,
    )

    if exposure_gap_dollars <= 0 or max_payoff_per_contract <= 0:
        return 0, "none"

    gap_contracts = max(
        1,
        int(round(exposure_gap_dollars / max_payoff_per_contract)),
    )

    contracts = min(gap_contracts, budget_contracts, plan_contracts)

    if contracts <= 0:
        return 0, "budget_constrained" if budget_contracts == 0 else "none"

    if contracts == gap_contracts:
        driver = "gap_constrained"
    elif contracts == budget_contracts:
        driver = "budget_constrained"
    else:
        driver = "plan_capped"

    return max(0, contracts), driver


def _build_hold_existing_ticket(
    *,
    priority: int,
    phase: str,
    bucket: str,
    description: str,
    current_positions: list[str],
    current_exposure_dollars: float,
) -> HedgeTradeTicket:
    return HedgeTradeTicket(
        priority=priority,
        phase=phase,
        bucket=bucket,
        action="hold_existing",
        description=description,
        contracts=0,
        estimated_debit_dollars=0.0,
        estimated_max_payoff_dollars=0.0,
        estimated_coverage_added_dollars=0.0,
        budget_consumption_pct=0.0,
        coverage_to_cost_ratio=0.0,
        fits_remaining_budget=True,
        sizing_driver="none",
        unfilled_gap_dollars=0.0,
        post_ticket_gap_dollars=0.0,
        long_leg_symbol=None,
        short_leg_symbol=None,
        legs=[],
        notes=[
            f"Existing {bucket} exposure retained: ${current_exposure_dollars:,.2f}",
            *current_positions,
        ],
    )


def _build_migration_ticket(
    *,
    priority: int,
    bucket: str,
    from_symbol: str,
    from_expiry: str | None,
    target_spread,
    optimizer_score: float | None = None,
    optimizer_reasons: list[str] | None = None,
) -> HedgeTradeTicket:
    notes: List[str] = [f"Close existing position: {from_symbol}"]
    if from_expiry:
        notes.append(f"Current expiry: {from_expiry}")
    if optimizer_score is not None:
        notes.append(f"Optimizer score: {optimizer_score:.2f}")
    if optimizer_reasons:
        notes.extend(optimizer_reasons)

    if target_spread.long_leg is not None:
        notes.append(f"Buy replacement long leg: {target_spread.long_leg.symbol}")
    if target_spread.short_leg is not None:
        notes.append(f"Sell replacement short leg: {target_spread.short_leg.symbol}")

    return HedgeTradeTicket(
        priority=priority,
        phase="roll",
        bucket=bucket,
        action="migrate_position",
        description="Roll existing hedge into target spread structure at roll window.",
        contracts=1,
        estimated_debit_dollars=0.0,
        estimated_max_payoff_dollars=0.0,
        estimated_coverage_added_dollars=0.0,
        budget_consumption_pct=0.0,
        coverage_to_cost_ratio=0.0,
        fits_remaining_budget=True,
        sizing_driver="none",
        unfilled_gap_dollars=0.0,
        post_ticket_gap_dollars=0.0,
        long_leg_symbol=target_spread.long_leg.symbol if target_spread.long_leg else None,
        short_leg_symbol=target_spread.short_leg.symbol if target_spread.short_leg else None,
        legs=[],
        notes=notes,
    )


def _build_replace_ticket(
    *,
    priority: int,
    bucket: str,
    from_symbol: str,
    from_expiry: str | None,
    target_spread,
    optimizer_score: float | None = None,
    optimizer_reasons: list[str] | None = None,
) -> HedgeTradeTicket:
    notes: List[str] = [f"Replace existing position: {from_symbol}"]
    if from_expiry:
        notes.append(f"Current expiry: {from_expiry}")
    if optimizer_score is not None:
        notes.append(f"Optimizer score: {optimizer_score:.2f}")
    if optimizer_reasons:
        notes.extend(optimizer_reasons)

    if target_spread.long_leg is not None:
        notes.append(f"Buy replacement long leg: {target_spread.long_leg.symbol}")
    if target_spread.short_leg is not None:
        notes.append(f"Sell replacement short leg: {target_spread.short_leg.symbol}")

    return HedgeTradeTicket(
        priority=priority,
        phase="roll",
        bucket=bucket,
        action="migrate_position",
        description="Replace inefficient existing hedge with target spread structure.",
        contracts=1,
        estimated_debit_dollars=0.0,
        estimated_max_payoff_dollars=0.0,
        estimated_coverage_added_dollars=0.0,
        budget_consumption_pct=0.0,
        coverage_to_cost_ratio=0.0,
        fits_remaining_budget=True,
        sizing_driver="none",
        unfilled_gap_dollars=0.0,
        post_ticket_gap_dollars=0.0,
        long_leg_symbol=target_spread.long_leg.symbol if target_spread.long_leg else None,
        short_leg_symbol=target_spread.short_leg.symbol if target_spread.short_leg else None,
        legs=[],
        notes=notes,
    )


def _build_spread_ticket(
    *,
    priority,
    phase,
    bucket,
    description,
    contracts,
    spread_plan,
    budget_before_ticket,
    exposure_gap_dollars,
    sizing_driver,
) -> HedgeTradeTicket:
    if (
        contracts <= 0
        or spread_plan.long_leg is None
        or spread_plan.short_leg is None
    ):
        notes: List[str] = ["No executable spread ticket generated."]
        if sizing_driver == "budget_constrained":
            notes.append("Ticket could not be funded within remaining budget.")
        elif sizing_driver == "plan_capped":
            notes.append("Ticket size was capped by the spread plan.")
        elif sizing_driver == "gap_constrained":
            notes.append("No additional contracts were required to address the hedge gap.")

        return HedgeTradeTicket(
            priority=priority,
            phase=phase,
            bucket=bucket,
            action="hold",
            description=description,
            contracts=0,
            estimated_debit_dollars=0.0,
            estimated_max_payoff_dollars=0.0,
            estimated_coverage_added_dollars=0.0,
            budget_consumption_pct=0.0,
            coverage_to_cost_ratio=0.0,
            fits_remaining_budget=True,
            sizing_driver=sizing_driver,
            unfilled_gap_dollars=max(exposure_gap_dollars, 0.0),
            post_ticket_gap_dollars=max(exposure_gap_dollars, 0.0),
            long_leg_symbol=None,
            short_leg_symbol=None,
            legs=[],
            notes=notes,
        )

    debit_per_contract = _safe_float(
        getattr(spread_plan, "debit_per_contract", 0.0),
        0.0,
    )
    max_payoff_per_contract = _safe_float(
        getattr(spread_plan, "max_payoff_per_contract", 0.0),
        0.0,
    )
    plan_contracts = int(_safe_float(getattr(spread_plan, "contracts", 0), 0))

    estimated_debit_dollars = contracts * debit_per_contract
    estimated_max_payoff_dollars = contracts * max_payoff_per_contract
    estimated_coverage_added_dollars = estimated_max_payoff_dollars

    budget_consumption_pct = (
        estimated_debit_dollars / budget_before_ticket
        if budget_before_ticket > 0
        else 0.0
    )
    coverage_to_cost_ratio = (
        estimated_coverage_added_dollars / estimated_debit_dollars
        if estimated_debit_dollars > 0
        else 0.0
    )
    fits_remaining_budget = estimated_debit_dollars <= budget_before_ticket + 1e-9

    coverage_added = contracts * max_payoff_per_contract
    post_ticket_gap = max(exposure_gap_dollars - coverage_added, 0.0)
    unfilled_gap = post_ticket_gap

    notes: List[str] = []
    if not fits_remaining_budget:
        notes.append("Ticket exceeds remaining budget.")
    if sizing_driver == "budget_constrained":
        notes.append("Ticket size is constrained by remaining budget.")
    elif sizing_driver == "plan_capped":
        notes.append("Ticket size is capped by the spread plan.")
    elif sizing_driver == "gap_constrained":
        notes.append("Ticket size is driven by the remaining hedge gap.")
    if contracts < plan_contracts and sizing_driver != "gap_constrained":
        notes.append("Ticket is smaller than the full spread plan size.")

    return HedgeTradeTicket(
        priority=priority,
        phase=phase,
        bucket=bucket,
        action="buy_spread",
        description=description,
        contracts=contracts,
        estimated_debit_dollars=estimated_debit_dollars,
        estimated_max_payoff_dollars=estimated_max_payoff_dollars,
        estimated_coverage_added_dollars=estimated_coverage_added_dollars,
        budget_consumption_pct=budget_consumption_pct,
        coverage_to_cost_ratio=coverage_to_cost_ratio,
        fits_remaining_budget=fits_remaining_budget,
        sizing_driver=sizing_driver,
        unfilled_gap_dollars=unfilled_gap,
        post_ticket_gap_dollars=post_ticket_gap,
        long_leg_symbol=spread_plan.long_leg.symbol,
        short_leg_symbol=spread_plan.short_leg.symbol,
        legs=[
            HedgeTradeLeg(
                symbol=spread_plan.long_leg.symbol,
                side="buy",
                quantity=contracts,
                option_type=spread_plan.long_leg.option_type,
                strike=spread_plan.long_leg.strike,
                expiry=spread_plan.long_leg.expiry,
            ),
            HedgeTradeLeg(
                symbol=spread_plan.short_leg.symbol,
                side="sell",
                quantity=contracts,
                option_type=spread_plan.short_leg.option_type,
                strike=spread_plan.short_leg.strike,
                expiry=spread_plan.short_leg.expiry,
            ),
        ],
        notes=notes,
    )


def build_hedge_trade_tickets(
    *,
    db,
    account_ids: list[str],
    as_of_date: str,
    underlying: str,
    market_regime: str,
    hedge_style: str,
    portfolio_value: float,
    current_hedge_pct: float,
    recommended_hedge_pct: float,
    additional_hedge_pct: float,
    remaining_hedge_budget_pct: float,
    vix_level: float | None = None,
) -> HedgeTradeTicketResponse:
    reconcile = build_hedge_reconciliation_engine(
        db=db,
        account_ids=account_ids,
        as_of_date=as_of_date,
        underlying=underlying,
        market_regime=market_regime,
        hedge_style=hedge_style,
        portfolio_value=portfolio_value,
        current_hedge_pct=current_hedge_pct,
        recommended_hedge_pct=recommended_hedge_pct,
        additional_hedge_pct=additional_hedge_pct,
        remaining_hedge_budget_pct=remaining_hedge_budget_pct,
    )

    plan = build_hedge_execution_plan(
        as_of_date=as_of_date,
        underlying=underlying,
        market_regime=market_regime,
        hedge_style=hedge_style,
        portfolio_value=portfolio_value,
        recommended_hedge_pct=recommended_hedge_pct,
        additional_hedge_pct=additional_hedge_pct,
        remaining_hedge_budget_pct=remaining_hedge_budget_pct,
    )

    tickets: List[HedgeTradeTicket] = []

    total_budget_dollars = portfolio_value * remaining_hedge_budget_pct
    budget_remaining = total_budget_dollars

    # Current market inputs for Optimizer V1
    underlying_price = get_latest_price(underlying) if underlying == "QQQ" else None
    vix_level = _safe_float(vix_level, 20.0)
    
    # 0) Existing primary hedge positions: optimizer decides keep / roll / replace
    primary_positions = [
        p for p in reconcile.current_positions
        if p.hedge_bucket == "primary"
    ]

    hold_positions = []
    migration_candidates = []
    replace_candidates = []
    exit_candidates = [] 

    for p in primary_positions:
        result = evaluate_hedge_efficiency(
            as_of_date=as_of_date,
            expiry=p.expiry,
            strike=p.strike,
            option_type=p.option_type,
            underlying_price=underlying_price,
            vix_level=vix_level,
            current_market_value=_safe_float(getattr(p, "market_value", None)),
            total_cost_basis=_safe_float(getattr(p, "total_cost_basis", None)),
            current_regime=market_regime,
        )

        if result.decision in ("close_profit_take", "close_regime_exit", "close_decay"):
            exit_candidates.append((p, result))
        elif result.decision == "keep":
            hold_positions.append((p, result))
        elif result.decision == "roll":
            migration_candidates.append((p, result))
        else:
            replace_candidates.append((p, result))

    held_primary_exposure = sum(
        max(-_safe_float(p.delta_dollars, 0.0), 0.0)
        for p, _ in hold_positions
    )

    if hold_positions:
        tickets.append(
            _build_hold_existing_ticket(
                priority=0,
                phase="immediate",
                bucket="primary",
                description="Keep existing primary naked puts in place.",
                current_positions=[p.symbol for p, _ in hold_positions],
                current_exposure_dollars=held_primary_exposure,
            )
        )
    # Build a lookup: expiry → short leg symbol for matching spread legs
    # Short legs have negative quantity (signed_qty < 0)
    short_leg_by_expiry: dict[str, str] = {}
    for pos in reconcile.current_positions:
        qty = _safe_float(getattr(pos, "quantity", 0), 0.0)
        if qty < 0:  # short leg
            expiry = getattr(pos, "expiry", "") or ""
            symbol = getattr(pos, "symbol", "") or ""
            if expiry and symbol:
                short_leg_by_expiry[expiry] = symbol
 
    for p, result in exit_candidates:
        action = result.decision
        close_frac = getattr(result, "close_fraction", 1.0)
        pnl = getattr(result, "pnl_dollars", None)
        pnl_str = f"  P&L: ${pnl:,.0f}" if pnl is not None else ""
 
        # Find matching short leg (same expiry = same spread)
        p_expiry = getattr(p, "expiry", "") or ""
        short_symbol = short_leg_by_expiry.get(p_expiry)
 
        qty = _safe_float(getattr(p, "quantity", 0), 0.0)
        close_contracts = max(1, int(round(abs(qty) * close_frac)))
 
        tickets.append(HedgeTradeTicket(
            priority=0,
            phase="immediate",
            bucket=getattr(p, "hedge_bucket", "primary"),
            action=action,
            description=(
                f"Exit {int(close_frac*100)}% of {p.symbol}"
                + (f"/{short_symbol}" if short_symbol else "")
                + f": {result.reasons[0] if result.reasons else action}{pnl_str}"
            ),
            contracts=close_contracts,
            estimated_debit_dollars=0.0,
            estimated_max_payoff_dollars=0.0,
            estimated_coverage_added_dollars=0.0,
            budget_consumption_pct=0.0,
            coverage_to_cost_ratio=0.0,
            fits_remaining_budget=True,
            sizing_driver="exit_trigger",
            unfilled_gap_dollars=0.0,
            post_ticket_gap_dollars=0.0,
            long_leg_symbol=p.symbol,
            short_leg_symbol=short_symbol,   # ← now correctly populated for spreads
            legs=[],
            notes=result.reasons,
        ))

    # 1) Tail first
    if reconcile.tail_action.action == "add_tail_spreads_now":
        tail_contracts, tail_driver = _ticket_contracts_from_gap(
            exposure_gap_dollars=_safe_float(
                reconcile.tail_action.exposure_gap_dollars,
                0.0,
            ),
            spread_plan=plan.tail_spread,
            budget_remaining=budget_remaining,
        )

        tail_ticket = _build_spread_ticket(
            priority=1,
            phase="immediate",
            bucket="tail",
            description="Add QQQ tail put spreads now.",
            contracts=tail_contracts,
            spread_plan=plan.tail_spread,
            budget_before_ticket=budget_remaining,
            exposure_gap_dollars=_safe_float(
                reconcile.tail_action.exposure_gap_dollars,
                0.0,
            ),
            sizing_driver=tail_driver,
        )
        tickets.append(tail_ticket)
        budget_remaining = max(
            budget_remaining - tail_ticket.estimated_debit_dollars,
            0.0,
        )

    # 2) Primary second
    if reconcile.primary_action.action == "add_primary_spreads":
        primary_contracts, primary_driver = _ticket_contracts_from_gap(
            exposure_gap_dollars=_safe_float(
                reconcile.primary_action.exposure_gap_dollars,
                0.0,
            ),
            spread_plan=plan.primary_spread,
            budget_remaining=budget_remaining,
        )

        primary_ticket = _build_spread_ticket(
            priority=2,
            phase="immediate",
            bucket="primary",
            description="Add QQQ primary put spreads to close remaining primary hedge gap.",
            contracts=primary_contracts,
            spread_plan=plan.primary_spread,
            budget_before_ticket=budget_remaining,
            exposure_gap_dollars=_safe_float(
                reconcile.primary_action.exposure_gap_dollars,
                0.0,
            ),
            sizing_driver=primary_driver,
        )
        tickets.append(primary_ticket)
        budget_remaining = max(
            budget_remaining - primary_ticket.estimated_debit_dollars,
            0.0,
        )

    # 3) Roll-phase migration tickets for roll candidates
    migration_priority = 90
    for p, result in migration_candidates:
        tickets.append(
            _build_migration_ticket(
                priority=migration_priority,
                bucket="primary",
                from_symbol=p.symbol,
                from_expiry=p.expiry,
                target_spread=plan.primary_spread,
                optimizer_score=result.score,
                optimizer_reasons=result.reasons,
            )
        )
        migration_priority += 1

    # 4) Roll-phase replacement tickets for clearly inefficient hedges
    replace_priority = 100
    for p, result in replace_candidates:
        tickets.append(
            _build_replace_ticket(
                priority=replace_priority,
                bucket="primary",
                from_symbol=p.symbol,
                from_expiry=p.expiry,
                target_spread=plan.primary_spread,
                optimizer_score=result.score,
                optimizer_reasons=result.reasons,
            )
        )
        replace_priority += 1

    if not tickets:
        tickets.append(
            HedgeTradeTicket(
                priority=99,
                phase="deferred",
                bucket="primary",
                action="hold",
                description="No immediate hedge trade tickets generated.",
                contracts=0,
                estimated_debit_dollars=0.0,
                estimated_max_payoff_dollars=0.0,
                estimated_coverage_added_dollars=0.0,
                budget_consumption_pct=0.0,
                coverage_to_cost_ratio=0.0,
                fits_remaining_budget=True,
                sizing_driver="none",
                unfilled_gap_dollars=0.0,
                post_ticket_gap_dollars=0.0,
                long_leg_symbol=None,
                short_leg_symbol=None,
                legs=[],
                notes=["Current hedge setup does not require immediate action."],
            )
        )

    tickets.sort(key=lambda x: x.priority)

    total_estimated_debit_dollars = sum(
        _safe_float(t.estimated_debit_dollars, 0.0) for t in tickets
    )
    total_estimated_max_payoff_dollars = sum(
        _safe_float(t.estimated_max_payoff_dollars, 0.0) for t in tickets
    )
    total_estimated_coverage_added_dollars = sum(
        _safe_float(t.estimated_coverage_added_dollars, 0.0) for t in tickets
    )

    notes = []
    if reconcile.immediate_actions:
        notes.append("Immediate hedge tickets are generated in execution priority order.")
    if migration_candidates or replace_candidates:
        notes.append("Roll-phase items are informational and not immediate trade tickets.")
    if hold_positions:
        notes.append("Existing primary hedge positions are explicitly retained in the ticket set.")
    if migration_candidates:
        notes.append("Some primary hedges are efficient enough to keep for now, but should be rolled later.")
    if replace_candidates:
        notes.append("Some primary hedges are inefficient and should be replaced with the target spread structure.")
    if budget_remaining <= 1.0:
        notes.append("Remaining hedge premium budget is effectively fully utilized.")
    elif total_estimated_debit_dollars < total_budget_dollars:
        notes.append("Not all remaining hedge premium budget is required for the current ticket set.")

    return HedgeTradeTicketResponse(
        as_of_date=as_of_date,
        benchmark="SPY",
        hedge_style=hedge_style,
        hedge_asset=underlying,
        market_regime=market_regime,
        tickets=tickets,
        total_estimated_debit_dollars=total_estimated_debit_dollars,
        total_estimated_max_payoff_dollars=total_estimated_max_payoff_dollars,
        total_estimated_coverage_added_dollars=total_estimated_coverage_added_dollars,
        remaining_budget_before_tickets=total_budget_dollars,
        remaining_budget_after_tickets=budget_remaining,
        budget_fully_utilized=budget_remaining <= 1.0,
        notes=notes,
    )