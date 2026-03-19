from __future__ import annotations

import hashlib
from typing import List

from app.schemas import (
    BrokerExecutionControls,
    BrokerOrderLeg,
    BrokerOrderPayload,
    BrokerOrderPayloadResponse,
    BrokerSubmissionResult,
    BrokerValidationFlags,
)
from app.services.hedge_trade_ticket_engine import build_hedge_trade_tickets
import time


SUPPORTED_EXECUTABLE_ACTIONS = {"buy_spread"}
SUPPORTED_MODES = {"preview", "dry_run", "submit"}


def _safe_float(value, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except Exception:
        return default


def _round_limit_price(price: float) -> float:
    return round(price, 2)


def _build_client_order_id(
    *,
    as_of_date: str,
    bucket: str,
    priority: int,
    underlying: str,
    leg_symbols: list[str],
) -> str:
    ts = int(time.time())   # seconds since epoch — unique per submission
    raw = f"{as_of_date}|{bucket}|{priority}|{underlying}|{'|'.join(leg_symbols)}|{ts}"
    digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]
    return f"hedge-{digest}"


def _build_execution_controls(
    *,
    mode: str,
    limit_price_buffer_pct: float,
    max_slippage_pct: float,
) -> BrokerExecutionControls:
    return BrokerExecutionControls(
        limit_price_source="ticket_estimated_debit",
        limit_price_buffer_pct=limit_price_buffer_pct,
        max_slippage_pct=max_slippage_pct,
        preview_only=(mode == "preview"),
    )


def _apply_limit_buffer(limit_price: float | None, buffer_pct: float) -> float | None:
    if limit_price is None:
        return None
    if buffer_pct == 0:
        return _round_limit_price(limit_price)
    adjusted = limit_price * (1.0 + buffer_pct)
    return _round_limit_price(adjusted)


def _infer_order_intent(*, order_class: str, net_side: str, legs: list[BrokerOrderLeg]) -> str:
    if order_class == "mleg" and net_side == "buy" and len(legs) == 2:
        return "open_debit_spread"
    return "unknown"


def _build_validation(
    *,
    legs: list[BrokerOrderLeg],
    limit_price: float | None,
    order_class: str,
    alpaca_payload: dict,
) -> BrokerValidationFlags:
    all_leg_symbols_present = all(bool(leg.symbol) for leg in legs)
    positive_limit_price = limit_price is not None and limit_price > 0
    valid_ratio_structure = len(legs) >= 2 and all(int(leg.ratio_qty) > 0 for leg in legs)
    has_supported_order_class = order_class == "mleg"

    broker_payload_complete = (
        isinstance(alpaca_payload, dict)
        and bool(alpaca_payload.get("order_class"))
        and bool(alpaca_payload.get("type"))
        and bool(alpaca_payload.get("time_in_force"))
        and bool(alpaca_payload.get("qty"))
        and bool(alpaca_payload.get("client_order_id"))
        and isinstance(alpaca_payload.get("legs"), list)
        and len(alpaca_payload.get("legs"),) >= 2
    )

    executable_now = (
        all_leg_symbols_present
        and positive_limit_price
        and valid_ratio_structure
        and has_supported_order_class
        and broker_payload_complete
    )

    return BrokerValidationFlags(
        all_leg_symbols_present=all_leg_symbols_present,
        positive_limit_price=positive_limit_price,
        valid_ratio_structure=valid_ratio_structure,
        executable_now=executable_now,
        has_supported_order_class=has_supported_order_class,
        broker_payload_complete=broker_payload_complete,
        broker_precheck_passed=False,
    )


def _build_alpaca_payload(
    *,
    qty: int,
    limit_price: float | None,
    time_in_force: str,
    client_order_id: str,
    legs: list[BrokerOrderLeg],
) -> dict:
    def _position_intent(side: str) -> str:
        side = (side or "").lower()
        if side == "buy":
            return "buy_to_open"
        if side == "sell":
            return "sell_to_open"
        raise ValueError(f"Unsupported leg side: {side}")

    return {
        "order_class": "mleg",
        "qty": str(qty),
        "type": "limit",
        "limit_price": str(limit_price) if limit_price is not None else None,
        "time_in_force": time_in_force,
        "client_order_id": client_order_id,
        "legs": [
            {
                "symbol": leg.symbol,
                "ratio_qty": str(leg.ratio_qty),
                "side": leg.side,
                "position_intent": _position_intent(leg.side),
            }
            for leg in legs
        ],
    }


def _ticket_to_broker_order(
    *,
    ticket,
    as_of_date: str,
    underlying: str,
    broker: str,
    mode: str,
    default_tif: str,
    default_order_type: str,
    limit_price_buffer_pct: float,
    max_slippage_pct: float,
) -> BrokerOrderPayload | None:
    action = str(getattr(ticket, "action", "") or "").lower()
    if action not in SUPPORTED_EXECUTABLE_ACTIONS:
        return None

    contracts = int(getattr(ticket, "contracts", 0) or 0)
    if contracts <= 0:
        return None

    source_legs = getattr(ticket, "legs", []) or []
    if len(source_legs) < 2:
        return None

    legs: List[BrokerOrderLeg] = []
    for leg in source_legs:
        legs.append(
            BrokerOrderLeg(
                symbol=str(getattr(leg, "symbol", "") or ""),
                side=str(getattr(leg, "side", "") or "").lower(),
                ratio_qty=1,
            )
        )

    estimated_debit_dollars = _safe_float(getattr(ticket, "estimated_debit_dollars", 0.0), 0.0)
    estimated_max_payoff_dollars = _safe_float(getattr(ticket, "estimated_max_payoff_dollars", 0.0), 0.0)
    estimated_coverage_added_dollars = _safe_float(getattr(ticket, "estimated_coverage_added_dollars", 0.0), 0.0)

    estimated_debit_per_spread = 0.0
    if contracts > 0 and estimated_debit_dollars > 0:
        estimated_debit_per_spread = estimated_debit_dollars / contracts

    base_limit_price = None
    if contracts > 0 and estimated_debit_dollars > 0:
        base_limit_price = estimated_debit_dollars / (contracts * 100.0)

    limit_price = _apply_limit_buffer(base_limit_price, limit_price_buffer_pct)

    client_order_id = _build_client_order_id(
        as_of_date=as_of_date,
        bucket=str(getattr(ticket, "bucket", "") or ""),
        priority=int(getattr(ticket, "priority", 0) or 0),
        underlying=underlying,
        leg_symbols=[leg.symbol for leg in legs],
    )

    alpaca_payload = _build_alpaca_payload(
        qty=contracts,
        limit_price=limit_price,
        time_in_force=default_tif,
        client_order_id=client_order_id,
        legs=legs,
    )

    validation = _build_validation(
        legs=legs,
        limit_price=limit_price,
        order_class="mleg",
        alpaca_payload=alpaca_payload,
    )

    execution_controls = _build_execution_controls(
        mode=mode,
        limit_price_buffer_pct=limit_price_buffer_pct,
        max_slippage_pct=max_slippage_pct,
    )

    net_side = "buy"
    order_intent = _infer_order_intent(
        order_class="mleg",
        net_side=net_side,
        legs=legs,
    )

    notes = list(getattr(ticket, "notes", []) or [])
    notes.append("Generated from hedge trade ticket.")
    notes.append("Broker-native payload attached.")
    if mode == "preview":
        notes.append("Preview mode: no broker submission.")
    elif mode == "dry_run":
        notes.append("Dry run mode: payload validated locally and not submitted.")
    else:
        notes.append("Submit mode: eligible for broker submission after prechecks.")

    return BrokerOrderPayload(
        ticket_priority=int(getattr(ticket, "priority", 0) or 0),
        ticket_phase=str(getattr(ticket, "phase", "") or ""),
        ticket_bucket=str(getattr(ticket, "bucket", "") or ""),
        ticket_action=str(getattr(ticket, "action", "") or ""),
        broker=broker,
        underlying=underlying,
        order_class="mleg",
        order_type=default_order_type,
        time_in_force=default_tif,
        net_side=net_side,
        order_intent=order_intent,
        client_order_id=client_order_id,
        limit_price=limit_price,
        qty=contracts,
        estimated_debit_per_spread=estimated_debit_per_spread,
        execution_controls=execution_controls,
        validation=validation,
        legs=legs,
        alpaca_payload=alpaca_payload,
        estimated_debit_dollars=estimated_debit_dollars,
        estimated_max_payoff_dollars=estimated_max_payoff_dollars,
        estimated_coverage_added_dollars=estimated_coverage_added_dollars,
        notes=notes,
        submission_result=BrokerSubmissionResult(
            mode=mode,
            submitted=False,
            broker_order_id=None,
            client_order_id=client_order_id,
            status="preview_ready" if mode == "preview" else "dry_run_ready" if mode == "dry_run" else "pending_submission",
            message="Payload generated successfully.",
        ),
    )


def build_broker_order_payloads(
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
    broker: str = "alpaca",
    mode: str = "preview",
    limit_price_buffer_pct: float = 0.0,
    max_slippage_pct: float = 0.02,
) -> BrokerOrderPayloadResponse:
    if mode not in SUPPORTED_MODES:
        raise ValueError(f"Unsupported mode: {mode}")

    tickets_response = build_hedge_trade_tickets(
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

    orders: List[BrokerOrderPayload] = []
    for ticket in tickets_response.tickets:
        payload = _ticket_to_broker_order(
            ticket=ticket,
            as_of_date=as_of_date,
            underlying=underlying,
            broker=broker,
            mode=mode,
            default_tif="day",
            default_order_type="limit",
            limit_price_buffer_pct=limit_price_buffer_pct,
            max_slippage_pct=max_slippage_pct,
        )
        if payload is not None:
            orders.append(payload)

    notes = ["Broker payloads generated from executable hedge tickets only."]
    if mode == "preview":
        notes.append("Preview mode does not simulate broker acceptance.")
    elif mode == "dry_run":
        notes.append("Dry run mode builds broker payloads and local validations only.")
    else:
        notes.append("Submit mode uses Alpaca execution adapter with safety checks.")

    if not orders:
        notes.append("No executable broker orders were generated.")

    return BrokerOrderPayloadResponse(
        as_of_date=as_of_date,
        benchmark=tickets_response.benchmark,
        hedge_style=tickets_response.hedge_style,
        hedge_asset=tickets_response.hedge_asset,
        market_regime=tickets_response.market_regime,
        mode=mode,
        broker=broker,
        orders=orders,
        notes=notes,
    )