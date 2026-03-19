from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from app.schemas import UnifiedHoldingRow, UnifiedHoldingsResponse
from app.services.broker_positions_engine import get_broker_positions
from app.services.portfolio_holdings_read import get_portfolio_holdings_data
from app.services.account_clients import get_client_for_account


_OPTION_RE = re.compile(r"^([A-Z]+)(\d{6})([CP])(\d{8})$")


def _safe_float(value, default: float | None = None) -> float | None:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except Exception:
        return default


def _parse_occ_option_symbol(symbol: str) -> dict[str, Any]:
    m = _OPTION_RE.match(symbol or "")
    if not m:
        return {
            "underlying": symbol,
            "expiry": None,
            "option_type": None,
            "strike": None,
            "position_type": "equity",
        }

    underlying, yymmdd, cp, strike_raw = m.groups()
    expiry = f"20{yymmdd[0:2]}-{yymmdd[2:4]}-{yymmdd[4:6]}"
    strike = int(strike_raw) / 1000.0

    return {
        "underlying": underlying,
        "expiry": expiry,
        "option_type": cp,
        "strike": strike,
        "position_type": "option",
    }


def _normalize_composer_holding(
    row: Dict[str, Any],
    *,
    account_id: str | None = None,
    account_name: str | None = None,
) -> UnifiedHoldingRow:
    symbol = str(row.get("symbol") or "")
    parsed = _parse_occ_option_symbol(symbol)

    qty = _safe_float(row.get("quantity"))
    market_value = _safe_float(row.get("market_value"))
    option_price = _safe_float(row.get("option_price"))
    avg_cost_basis = _safe_float(row.get("avg_cost_basis"))
    total_cost_basis = _safe_float(row.get("total_cost_basis"))
    allocation_pct_raw = _safe_float(row.get("allocation_pct"))

    if parsed["position_type"] == "option":
        current_price = option_price
    else:
        current_price = (
            market_value / qty if market_value is not None and qty not in (None, 0) else None
        )

    return UnifiedHoldingRow(
        source="composer",
        source_account_id=account_id,
        source_account_name=account_name,
        broker="composer",
        broker_environment=None,
        symbol=symbol,
        underlying=parsed["underlying"],
        asset_class="us_option" if parsed["position_type"] == "option" else "us_equity",
        position_type=parsed["position_type"],
        quantity=qty,
        side="long",
        market_value=market_value,
        allocation_pct=(allocation_pct_raw / 100.0) if allocation_pct_raw is not None else None,
        avg_cost_basis=avg_cost_basis,
        total_cost_basis=total_cost_basis,
        current_price=current_price,
        delta_dollars=_safe_float(row.get("delta_dollars")),
        option_type=parsed["option_type"],
        strike=parsed["strike"],
        expiry=parsed["expiry"],
        unrealized_pl=(
            market_value - total_cost_basis
            if market_value is not None and total_cost_basis is not None
            else None
        ),
        unrealized_plpc=(
            (market_value - total_cost_basis) / total_cost_basis
            if market_value is not None and total_cost_basis not in (None, 0)
            else None
        ),
        raw_data=row,
        notes=[],
    )


def _normalize_alpaca_position(row) -> UnifiedHoldingRow:
    symbol = str(row.symbol or "")
    parsed = _parse_occ_option_symbol(symbol)

    total_cost_basis = _safe_float(row.cost_basis)
    qty = _safe_float(row.qty)
    avg_cost_basis = _safe_float(row.avg_entry_price)

    return UnifiedHoldingRow(
        source="alpaca",
        source_account_id="alpaca",
        source_account_name="Alpaca",
        broker=row.broker,
        broker_environment=row.broker_environment,
        symbol=symbol,
        underlying=parsed["underlying"],
        asset_class=row.asset_class,
        position_type=parsed["position_type"],
        quantity=qty,
        side=row.side,
        market_value=_safe_float(row.market_value),
        allocation_pct=None,
        avg_cost_basis=avg_cost_basis,
        total_cost_basis=total_cost_basis,
        current_price=_safe_float(row.current_price),
        delta_dollars=None,
        option_type=parsed["option_type"],
        strike=parsed["strike"],
        expiry=parsed["expiry"],
        unrealized_pl=_safe_float(row.unrealized_pl),
        unrealized_plpc=_safe_float(row.unrealized_plpc),
        raw_data=row.raw_position,
        notes=[],
    )


def load_alpaca_holdings_normalized() -> List[UnifiedHoldingRow]:
    resp = get_broker_positions(broker="alpaca")
    return [_normalize_alpaca_position(row) for row in resp.positions]


def load_composer_holdings_normalized(
    *,
    db: Session,
    account_ids: list[str],
) -> tuple[str | None, List[UnifiedHoldingRow]]:
    if not account_ids:
        return None, []

    holdings_resp = get_portfolio_holdings_data(
        db=db,
        account_ids=account_ids,
        target_date=None,
        get_client_for_account_fn=get_client_for_account,
    )

    as_of_date = holdings_resp.get("date")
    rows = holdings_resp.get("holdings", [])

    normalized = [
        _normalize_composer_holding(r)
        for r in rows
    ]
    return as_of_date, normalized


def load_unified_holdings(
    *,
    db: Session,
    composer_account_ids: Optional[list[str]] = None,
    include_composer: bool = True,
    include_alpaca: bool = False,
) -> UnifiedHoldingsResponse:
    out: List[UnifiedHoldingRow] = []
    notes: List[str] = []
    as_of_date: str | None = None

    if include_composer:
        composer_date, composer_rows = load_composer_holdings_normalized(
            db=db,
            account_ids=composer_account_ids or [],
        )
        as_of_date = composer_date
        out.extend(composer_rows)
        notes.append("Included normalized Composer holdings.")

    if include_alpaca:
        alpaca_rows = load_alpaca_holdings_normalized()
        out.extend(alpaca_rows)
        notes.append("Included normalized Alpaca positions.")

    total_mv = sum(_safe_float(r.market_value, 0.0) or 0.0 for r in out)

    if total_mv > 0:
        for r in out:
            mv = _safe_float(r.market_value, 0.0) or 0.0
            if r.source == "alpaca" or r.allocation_pct is None:
                r.allocation_pct = mv / total_mv

    return UnifiedHoldingsResponse(
        as_of_date=as_of_date,
        rows=out,
        notes=notes,
    )