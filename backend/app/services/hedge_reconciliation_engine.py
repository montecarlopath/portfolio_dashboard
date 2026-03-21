from __future__ import annotations

from datetime import date
from typing import Any, List, Literal, Optional


from app.services.account_clients import get_client_for_account
from app.services.finnhub_market_data import get_latest_price
from app.services.hedge_execution_planner import build_hedge_execution_plan
from app.services.option_valuation import parse_occ_option_symbol
from app.services.portfolio_holdings_read import get_portfolio_holdings_data
from app.services.alpaca_hedge_inventory import load_alpaca_hedge_positions

from app.services.hedge_position_classifier import (
    classify_option_bucket,
    classify_structure_type,
)


from app.schemas import (
    HedgeReconciliationResponse,
    HedgeReconciliationAction,
    HedgeExecutionPriorityItem,
    HedgePositionSnapshot,
)


def _get_field(obj: Any, key: str, default=None):
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except Exception:
        return default




def _extract_current_hedge_positions(
    *,
    holdings: list,
    as_of_date: str,
    spot_price: Optional[float],
) -> List[HedgePositionSnapshot]:
    out: List[HedgePositionSnapshot] = []

    for h in holdings:
        symbol = str(_get_field(h, "symbol", "") or "")
        quantity = _safe_float(_get_field(h, "quantity", 0.0), 0.0)
        market_value = _safe_float(_get_field(h, "market_value", 0.0), 0.0)
        total_cost_basis = _safe_float(_get_field(h, "total_cost_basis", 0.0), 0.0)
        delta_dollars = _safe_float(_get_field(h, "delta_dollars", 0.0), 0.0)

        parsed = parse_occ_option_symbol(symbol)
        if parsed is None:
            continue

        if parsed.underlying != "QQQ":
            continue

        bucket = classify_option_bucket(
            expiry=parsed.expiry.isoformat(),
            strike=parsed.strike,
            option_type=parsed.option_type,
            underlying=parsed.underlying,
            as_of_date=as_of_date,
            spot_price=spot_price,
            quantity=quantity,
            delta_dollars=delta_dollars,
)

        structure_type = classify_structure_type(
            bucket=bucket,
            option_type=parsed.option_type,
            quantity=quantity,   # or signed_qty in alpaca file
        )

        out.append(
            HedgePositionSnapshot(
                symbol=symbol,
                quantity=quantity,
                expiry=parsed.expiry.isoformat(),
                strike=parsed.strike,
                option_type=parsed.option_type,
                market_value=market_value,
                total_cost_basis=total_cost_basis,
                delta_dollars=delta_dollars,
                hedge_bucket=bucket,
                structure_type=structure_type,
            )
        )

    return out


def _estimate_bucket_contracts(positions: List[HedgePositionSnapshot], bucket: str) -> int:
    long_qty = 0.0
    short_qty = 0.0

    for p in positions:
        if p.hedge_bucket != bucket:
            continue

        qty = float(p.quantity or 0.0)
        if qty > 0:
            long_qty += qty
        elif qty < 0:
            short_qty += abs(qty)

    if long_qty > 0 and short_qty > 0:
        return int(round(min(long_qty, short_qty)))

    return int(round(max(long_qty, short_qty)))


def _bucket_symbols(positions: List[HedgePositionSnapshot], bucket: str) -> List[str]:
    return [p.symbol for p in positions if p.hedge_bucket == bucket]


def _bucket_exposure_dollars(positions: List[HedgePositionSnapshot], bucket: str) -> float:
    # Net all delta_dollars within the bucket (long puts negative, short puts positive)
    # then convert to positive exposure value.
    # Using max(-net, 0) per-leg (old approach) zeros out short legs — wrong for spreads.
    net_delta = 0.0
    for p in positions:
        if p.hedge_bucket == bucket:
            net_delta += (p.delta_dollars or 0.0)
    return max(-net_delta, 0.0)


def _safe_dte(as_of_date: str, expiry: Optional[str]) -> Optional[int]:
    if not expiry:
        return None
    try:
        return (date.fromisoformat(expiry) - date.fromisoformat(as_of_date)).days
    except Exception:
        return None


def _nearest_bucket_dte(
    positions: List[HedgePositionSnapshot],
    *,
    as_of_date: str,
    bucket: str,
) -> Optional[int]:
    dtes = []
    for p in positions:
        if p.hedge_bucket != bucket:
            continue
        dte = _safe_dte(as_of_date, p.expiry)
        if dte is not None:
            dtes.append(dte)
    return min(dtes) if dtes else None


def _decide_primary_action(
    *,
    as_of_date: str,
    current_positions: List[HedgePositionSnapshot],
    target_contracts: int,
    target_exposure_dollars: float,
    target_expiry: Optional[str],
    target_structure_name: str,
    target_long_symbol: Optional[str],
    target_short_symbol: Optional[str],
) -> HedgeReconciliationAction:
    current_contracts = _estimate_bucket_contracts(current_positions, "primary")
    current_symbols = _bucket_symbols(current_positions, "primary")
    current_exposure = _bucket_exposure_dollars(current_positions, "primary")
    gap = max(target_exposure_dollars - current_exposure, 0.0)
    nearest_dte = _nearest_bucket_dte(current_positions, as_of_date=as_of_date, bucket="primary")

    # crude structure detection for v3:
    # if current primary positions are naked puts and target is spread, structure differs
    has_naked_puts = any(
        p.hedge_bucket == "primary" and p.structure_type == "naked_put"
        for p in current_positions
    )

    if current_contracts == 0 and target_contracts > 0:
        return HedgeReconciliationAction(
            bucket="primary",
            action="add_primary_spreads",
            reason="No current primary hedge detected; add target primary spreads.",
            current_contracts_estimate=0,
            target_contracts=target_contracts,
            current_positions=current_symbols,
            current_exposure_dollars=current_exposure,
            target_exposure_dollars=target_exposure_dollars,
            exposure_gap_dollars=gap,
            target_structure_name=target_structure_name,
            target_expiry=target_expiry,
            target_long_symbol=target_long_symbol,
            target_short_symbol=target_short_symbol,
        )

    if nearest_dte is not None and nearest_dte < 35:
        return HedgeReconciliationAction(
            bucket="primary",
            action="replace_on_roll",
            reason="Current primary hedge is near roll window; migrate naked puts into the target spread structure.",
            current_contracts_estimate=current_contracts,
            target_contracts=target_contracts,
            current_positions=current_symbols,
            current_exposure_dollars=current_exposure,
            target_exposure_dollars=target_exposure_dollars,
            exposure_gap_dollars=gap,
            target_structure_name=target_structure_name,
            target_expiry=target_expiry,
            target_long_symbol=target_long_symbol,
            target_short_symbol=target_short_symbol,
        )

    # If exposure is materially below target, add more primary spreads
    if current_exposure < target_exposure_dollars * 0.85:
        return HedgeReconciliationAction(
            bucket="primary",
            action="add_primary_spreads",
            reason="Existing primary hedge exposure is below target; add primary spreads.",
            current_contracts_estimate=current_contracts,
            target_contracts=target_contracts,
            current_positions=current_symbols,
            current_exposure_dollars=current_exposure,
            target_exposure_dollars=target_exposure_dollars,
            exposure_gap_dollars=gap,
            target_structure_name=target_structure_name,
            target_expiry=target_expiry,
            target_long_symbol=target_long_symbol,
            target_short_symbol=target_short_symbol,
        )

    # If exposure is in the right zone but structure is not the eventual target, keep for now and replace later
    if target_exposure_dollars > 0 and 0.85 <= (current_exposure / target_exposure_dollars) <= 1.15:
        if has_naked_puts:
            return HedgeReconciliationAction(
                bucket="primary",
                action="replace_on_roll",
                reason="Primary exposure is adequate, but current hedge is naked-put based; replace with target spread on roll.",
                current_contracts_estimate=current_contracts,
                target_contracts=target_contracts,
                current_positions=current_symbols,
                current_exposure_dollars=current_exposure,
                target_exposure_dollars=target_exposure_dollars,
                exposure_gap_dollars=gap,
                target_structure_name=target_structure_name,
                target_expiry=target_expiry,
                target_long_symbol=target_long_symbol,
                target_short_symbol=target_short_symbol,
            )

        return HedgeReconciliationAction(
            bucket="primary",
            action="hold_existing",
            reason="Current primary hedge is already well aligned with target exposure.",
            current_contracts_estimate=current_contracts,
            target_contracts=target_contracts,
            current_positions=current_symbols,
            current_exposure_dollars=current_exposure,
            target_exposure_dollars=target_exposure_dollars,
            exposure_gap_dollars=gap,
            target_structure_name=target_structure_name,
            target_expiry=target_expiry,
            target_long_symbol=target_long_symbol,
            target_short_symbol=target_short_symbol,
        )

    # If exposure is above target, do not add more; usually just hold or migrate later
    if current_exposure > target_exposure_dollars * 1.15:
        if has_naked_puts:
            return HedgeReconciliationAction(
                bucket="primary",
                action="hold_existing",
                reason="Primary hedge exposure already exceeds target; hold existing puts and avoid adding more.",
                current_contracts_estimate=current_contracts,
                target_contracts=target_contracts,
                current_positions=current_symbols,
                current_exposure_dollars=current_exposure,
                target_exposure_dollars=target_exposure_dollars,
                exposure_gap_dollars=0.0,
                target_structure_name=target_structure_name,
                target_expiry=target_expiry,
                target_long_symbol=target_long_symbol,
                target_short_symbol=target_short_symbol,
            )

        return HedgeReconciliationAction(
            bucket="primary",
            action="hold_existing",
            reason="Primary hedge exposure exceeds target but remains acceptable; no addition needed.",
            current_contracts_estimate=current_contracts,
            target_contracts=target_contracts,
            current_positions=current_symbols,
            current_exposure_dollars=current_exposure,
            target_exposure_dollars=target_exposure_dollars,
            exposure_gap_dollars=0.0,
            target_structure_name=target_structure_name,
            target_expiry=target_expiry,
            target_long_symbol=target_long_symbol,
            target_short_symbol=target_short_symbol,
        )

    return HedgeReconciliationAction(
        bucket="primary",
        action="hold_existing",
        reason="Current primary hedge is acceptable for now.",
        current_contracts_estimate=current_contracts,
        target_contracts=target_contracts,
        current_positions=current_symbols,
        current_exposure_dollars=current_exposure,
        target_exposure_dollars=target_exposure_dollars,
        exposure_gap_dollars=gap,
        target_structure_name=target_structure_name,
        target_expiry=target_expiry,
        target_long_symbol=target_long_symbol,
        target_short_symbol=target_short_symbol,
    )


def _decide_tail_action(
    *,
    current_positions: List[HedgePositionSnapshot],
    target_contracts: int,
    target_exposure_dollars: float,
    target_expiry: Optional[str],
    target_structure_name: str,
    target_long_symbol: Optional[str],
    target_short_symbol: Optional[str],
) -> HedgeReconciliationAction:
    current_contracts = _estimate_bucket_contracts(current_positions, "tail")
    current_symbols = _bucket_symbols(current_positions, "tail")
    current_exposure = _bucket_exposure_dollars(current_positions, "tail")
    gap = max(target_exposure_dollars - current_exposure, 0.0)

    has_tail_positions = len(current_symbols) > 0
    has_tail_exposure = current_exposure > 0

    if not has_tail_positions and not has_tail_exposure and target_contracts > 0:
        return HedgeReconciliationAction(
            bucket="tail",
            action="add_tail_spreads_now",
            reason="No current tail hedge detected; add target tail spreads now.",
            current_contracts_estimate=0,
            target_contracts=target_contracts,
            current_positions=current_symbols,
            current_exposure_dollars=current_exposure,
            target_exposure_dollars=target_exposure_dollars,
            exposure_gap_dollars=gap,
            target_structure_name=target_structure_name,
            target_expiry=target_expiry,
            target_long_symbol=target_long_symbol,
            target_short_symbol=target_short_symbol,
        )

    if current_exposure >= target_exposure_dollars * 0.85:
        return HedgeReconciliationAction(
            bucket="tail",
            action="hold_existing",
            reason="Current tail hedge is already close to target exposure.",
            current_contracts_estimate=current_contracts,
            target_contracts=target_contracts,
            current_positions=current_symbols,
            current_exposure_dollars=current_exposure,
            target_exposure_dollars=target_exposure_dollars,
            exposure_gap_dollars=gap,
            target_structure_name=target_structure_name,
            target_expiry=target_expiry,
            target_long_symbol=target_long_symbol,
            target_short_symbol=target_short_symbol,
        )

    return HedgeReconciliationAction(
        bucket="tail",
        action="add_tail_spreads_now",
        reason="Tail hedge sleeve exists but is underbuilt relative to target exposure; add tail spreads now.",
        current_contracts_estimate=current_contracts,
        target_contracts=target_contracts,
        current_positions=current_symbols,
        current_exposure_dollars=current_exposure,
        target_exposure_dollars=target_exposure_dollars,
        exposure_gap_dollars=gap,
        target_structure_name=target_structure_name,
        target_expiry=target_expiry,
        target_long_symbol=target_long_symbol,
        target_short_symbol=target_short_symbol,
    )


def _summary_action(primary_action: str, tail_action: str) -> Literal[
    "hold_existing",
    "keep_partial",
    "add",
    "add_primary_spreads",
    "add_tail_spreads",
    "add_tail_spreads_now",
    "reduce",
    "replace",
    "replace_on_roll",
    "roll",
    "close",
]:
    priority = [
        "add_tail_spreads_now",
        "add_primary_spreads",
        "reduce",
        "close",
        "replace",
        "replace_on_roll",
        "roll",
        "add_tail_spreads",
        "add",
        "hold_existing",
        "keep_partial",
    ]
    for action in priority:
        if primary_action == action or tail_action == action:
            return action
    return "hold_existing"

def _action_phase(action: str) -> Literal["immediate", "deferred", "roll"]:
    if action in {"add_primary_spreads", "add_tail_spreads", "add_tail_spreads_now", "add", "reduce", "close"}:
        return "immediate"
    if action in {"replace_on_roll", "roll"}:
        return "roll"
    return "deferred"

def _action_priority(action: str) -> int:
    priority_map = {
        "add_tail_spreads_now": 1,
        "add_primary_spreads": 2,
        "reduce": 3,
        "close": 4,
        "replace": 5,
        "replace_on_roll": 6,
        "roll": 7,
        "hold_existing": 8,
        "keep_partial": 9,
        "add_tail_spreads": 10,
        "add": 11,
    }
    return priority_map.get(action, 99)

def _build_execution_priority(
    *,
    primary_action: HedgeReconciliationAction,
    tail_action: HedgeReconciliationAction,
) -> tuple[list, list[str], list[str], list[str]]:
    items = []

    for action_obj in [tail_action, primary_action]:
        action = action_obj.action
        phase = _action_phase(action)
        priority = _action_priority(action)

        items.append(
            HedgeExecutionPriorityItem(
                phase=phase,
                bucket=action_obj.bucket,
                action=action,
                priority=priority,
                reason=action_obj.reason,
            )
        )

    items.sort(key=lambda x: x.priority)

    immediate_actions = [
        f"{item.bucket}: {item.action}"
        for item in items
        if item.phase == "immediate"
        and item.action not in {"hold_existing", "keep_partial"}
    ]

    deferred_actions = [
        f"{item.bucket}: {item.action}"
        for item in items
        if item.phase == "deferred"
        and item.action not in {"hold_existing", "keep_partial"}
    ]

    roll_actions = [
        f"{item.bucket}: {item.action}"
        for item in items
        if item.phase == "roll"
    ]

    return items, immediate_actions, deferred_actions, roll_actions


def build_hedge_reconciliation_engine(
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
    vix_level: float = 20.0,
    spot_price: float | None = None,
) -> HedgeReconciliationResponse:
    holdings_resp = get_portfolio_holdings_data(
        db=db,
        account_ids=account_ids,
        target_date=as_of_date,
        get_client_for_account_fn=get_client_for_account,
    )
    holdings = _get_field(holdings_resp, "holdings", []) or []

    qqq_spot = float(spot_price) if spot_price is not None and spot_price > 0 else None

    if qqq_spot is None:
        live_spot = get_latest_price("QQQ")
        if live_spot is not None and live_spot > 0:
            qqq_spot = float(live_spot)

    print("RECON qqq_spot =", qqq_spot)


    composer_positions = _extract_current_hedge_positions(
        holdings=holdings,
        as_of_date=as_of_date,
        spot_price=qqq_spot,
    )

    alpaca_positions = load_alpaca_hedge_positions(
        as_of_date=as_of_date,
        underlying=underlying,
        spot_price=qqq_spot,
    )

    current_positions = composer_positions + alpaca_positions

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

    target_primary_exposure = float(plan.primary_spread.estimated_coverage_dollars or 0.0)
    target_tail_exposure = float(plan.tail_spread.estimated_coverage_dollars or 0.0)

    current_primary_exposure = _bucket_exposure_dollars(current_positions, "primary")
    current_tail_exposure = _bucket_exposure_dollars(current_positions, "tail")

    primary_action = _decide_primary_action(
        as_of_date=as_of_date,
        current_positions=current_positions,
        target_contracts=plan.primary_spread.contracts,
        target_exposure_dollars=target_primary_exposure,
        target_expiry=plan.primary_spread.selected_expiry,
        target_structure_name=plan.primary_spread.structure_name,
        target_long_symbol=plan.primary_spread.long_leg.symbol if plan.primary_spread.long_leg else None,
        target_short_symbol=plan.primary_spread.short_leg.symbol if plan.primary_spread.short_leg else None,
    )

    tail_action = _decide_tail_action(
        current_positions=current_positions,
        target_contracts=plan.tail_spread.contracts,
        target_exposure_dollars=target_tail_exposure,
        target_expiry=plan.tail_spread.selected_expiry,
        target_structure_name=plan.tail_spread.structure_name,
        target_long_symbol=plan.tail_spread.long_leg.symbol if plan.tail_spread.long_leg else None,
        target_short_symbol=plan.tail_spread.short_leg.symbol if plan.tail_spread.short_leg else None,
    )

    notes: List[str] = []

    if current_primary_exposure > 0:
        notes.append("Existing naked QQQ puts already provide meaningful primary hedge exposure.")
    if current_tail_exposure <= 0:
        notes.append("No dedicated tail hedge sleeve is currently in place.")
    
    execution_priority, immediate_actions, deferred_actions, roll_actions = _build_execution_priority(
        primary_action=primary_action,
        tail_action=tail_action,
    )

    if immediate_actions:
        notes.append("Immediate actions are prioritized before roll-time migrations.")
    if roll_actions:
        notes.append("Some hedge migrations are better deferred until the roll window.")

    return HedgeReconciliationResponse(
        as_of_date=as_of_date,
        benchmark="SPY",
        hedge_style=hedge_style,
        hedge_asset=underlying,
        market_regime=market_regime,
        current_positions=current_positions,
        current_primary_exposure_dollars=current_primary_exposure,
        current_tail_exposure_dollars=current_tail_exposure,
        target_primary_exposure_dollars=target_primary_exposure,
        target_tail_exposure_dollars=target_tail_exposure,
        primary_action=primary_action,
        tail_action=tail_action,
        summary_action=_summary_action(primary_action.action, tail_action.action),
        execution_priority=execution_priority,
        immediate_actions=immediate_actions,
        deferred_actions=deferred_actions,
        roll_actions=roll_actions,
        notes=notes,
    )