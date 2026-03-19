"""Portfolio holdings read services."""

from __future__ import annotations

from datetime import date
from typing import Callable, Dict, List, Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models import Account, HoldingsHistory, SymphonyAllocationHistory
from app.services.date_filters import parse_iso_date


def _safe_float(value, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except Exception:
        return default


def _normalize_live_symbol(symbol: str) -> str:
    """
    Convert live broker symbols like:
      OPTIONS::QQQ260515P00550000//USD
    into:
      QQQ260515P00550000
    """
    if not symbol:
        return ""

    if symbol.startswith("OPTIONS::") and "//" in symbol:
        return symbol.split("OPTIONS::", 1)[1].split("//", 1)[0]

    return symbol


def _is_option_symbol(symbol: str) -> bool:
    if not symbol:
        return False
    return len(symbol) > 15 and ("P" in symbol or "C" in symbol)


def _extract_option_price(holding: Dict) -> float:
    return _safe_float(
        holding.get("price")
        or holding.get("option_price")
        or holding.get("current_price")
        or holding.get("mark_price")
        or holding.get("last_price")
        or 0.0
    )


def _extract_avg_cost_basis(holding: Dict) -> float:
    return _safe_float(
        holding.get("average_cost_basis")
        or holding.get("avg_cost_basis")
        or 0.0
    )


def _extract_total_cost_basis(holding: Dict) -> float:
    return _safe_float(
        holding.get("cost_basis")
        or holding.get("total_cost_basis")
        or 0.0
    )


def _extract_market_value(holding: Dict) -> float:
    """
    For options, prefer direct.value.
    For regular holdings, use market/current/notional value.
    """
    asset_class = str(holding.get("asset_class", "") or "").upper()

    if asset_class == "OPTIONS":
        direct = holding.get("direct") or {}
        direct_value = _safe_float(direct.get("value"), 0.0)
        if direct_value > 0:
            return direct_value

    return _safe_float(
        holding.get("market_value")
        or holding.get("current_value")
        or holding.get("value")
        or holding.get("notional_value")
        or 0.0
    )


def _extract_delta_dollars(holding: Dict) -> float:
    return _safe_float(holding.get("delta_dollars"), 0.0)


def get_portfolio_holdings_data(
    db: Session,
    account_ids: List[str],
    target_date: Optional[str],
    get_client_for_account_fn: Callable[[Session, str], object],
) -> Dict:
    """Holdings for a specific date (defaults to latest)."""
    base_query = db.query(HoldingsHistory).filter(HoldingsHistory.account_id.in_(account_ids))

    rows = []
    latest_date = None
    if target_date:
        resolved_date = parse_iso_date(target_date, "date")
        rows = base_query.filter(
            HoldingsHistory.date <= resolved_date
        ).order_by(HoldingsHistory.date.desc()).all()
        if rows:
            latest_date = rows[0].date
            rows = [row for row in rows if row.date == latest_date]
        else:
            latest_date = resolved_date
    else:
        latest_date_row = base_query.with_entities(HoldingsHistory.date).order_by(
            HoldingsHistory.date.desc()
        ).first()
        if latest_date_row:
            latest_date = latest_date_row[0]
            rows = base_query.filter_by(date=latest_date).all()

    live_map: Dict[str, Dict] = {}
    test_ids = {
        acct.id
        for acct in db.query(Account).filter_by(credential_name="__TEST__").all()
    }

    for aid in account_ids:
        if aid in test_ids:
            alloc_rows = (
                db.query(SymphonyAllocationHistory)
                .filter_by(account_id=aid)
                .order_by(SymphonyAllocationHistory.date.desc())
                .all()
            )
            if alloc_rows:
                alloc_date = alloc_rows[0].date
                for row in alloc_rows:
                    if row.date == alloc_date and row.value > 0:
                        if row.ticker not in live_map:
                            live_map[row.ticker] = {
                                "symbol": row.ticker,
                                "market_value": 0.0,
                                "option_price": 0.0,
                                "avg_cost_basis": 0.0,
                                "total_cost_basis": 0.0,
                                "delta_dollars": 0.0,
                            }
                        live_map[row.ticker]["market_value"] += _safe_float(row.value)
            continue

        try:
            client = get_client_for_account_fn(db, aid)
            stats = client.get_holding_stats(aid)

            for holding in stats.get("holdings", []):
                raw_symbol = holding.get("symbol", "")
                symbol = _normalize_live_symbol(raw_symbol)

                if not symbol or symbol == "$USD":
                    continue

                market_value = _extract_market_value(holding)
                option_price = _extract_option_price(holding)
                avg_cost_basis = _extract_avg_cost_basis(holding)
                total_cost_basis = _extract_total_cost_basis(holding)
                delta_dollars = _extract_delta_dollars(holding)

                if symbol not in live_map:
                    live_map[symbol] = {
                        "symbol": symbol,
                        "market_value": 0.0,
                        "option_price": 0.0,
                        "avg_cost_basis": 0.0,
                        "total_cost_basis": 0.0,
                        "delta_dollars": 0.0,
                    }

                live_map[symbol]["market_value"] += market_value
                live_map[symbol]["total_cost_basis"] += total_cost_basis
                live_map[symbol]["delta_dollars"] += delta_dollars

                if option_price > 0:
                    live_map[symbol]["option_price"] = option_price
                if avg_cost_basis > 0:
                    live_map[symbol]["avg_cost_basis"] = avg_cost_basis

        except Exception:
            pass

    holdings_by_symbol: Dict[str, Dict] = {}
    for row in rows:
        if row.symbol in holdings_by_symbol:
            holdings_by_symbol[row.symbol]["quantity"] += row.quantity
        else:
            holdings_by_symbol[row.symbol] = {
                "symbol": row.symbol,
                "quantity": row.quantity,
            }

    if holdings_by_symbol:
        holdings = []
        for symbol, holding in holdings_by_symbol.items():
            live = live_map.get(symbol, {})
            market_value = _safe_float(live.get("market_value", 0.0))
            option_price = _safe_float(live.get("option_price", 0.0))
            avg_cost_basis = _safe_float(live.get("avg_cost_basis", 0.0))
            total_cost_basis = _safe_float(live.get("total_cost_basis", 0.0))
            delta_dollars = _safe_float(live.get("delta_dollars", 0.0))

            holding_row = {
                "symbol": symbol,
                "quantity": holding["quantity"],
                "market_value": round(market_value, 2),
            }

            if _is_option_symbol(symbol):
                holding_row["option_price"] = round(option_price, 6) if option_price > 0 else 0.0
                holding_row["avg_cost_basis"] = round(avg_cost_basis, 6) if avg_cost_basis > 0 else 0.0
                holding_row["total_cost_basis"] = round(total_cost_basis, 2) if total_cost_basis > 0 else 0.0
                holding_row["delta_dollars"] = round(delta_dollars, 2)

            holdings.append(holding_row)

    elif live_map:
        holdings = []
        for symbol, values in live_map.items():
            holding_row = {
                "symbol": symbol,
                "quantity": 0,
                "market_value": round(_safe_float(values.get("market_value", 0.0)), 2),
            }

            if _is_option_symbol(symbol):
                option_price = _safe_float(values.get("option_price", 0.0))
                avg_cost_basis = _safe_float(values.get("avg_cost_basis", 0.0))
                total_cost_basis = _safe_float(values.get("total_cost_basis", 0.0))
                delta_dollars = _safe_float(values.get("delta_dollars", 0.0))

                holding_row["option_price"] = round(option_price, 6) if option_price > 0 else 0.0
                holding_row["avg_cost_basis"] = round(avg_cost_basis, 6) if avg_cost_basis > 0 else 0.0
                holding_row["total_cost_basis"] = round(total_cost_basis, 2) if total_cost_basis > 0 else 0.0
                holding_row["delta_dollars"] = round(delta_dollars, 2)

            holdings.append(holding_row)

        latest_date = date.today()
    else:
        return {"date": str(latest_date) if latest_date else None, "holdings": []}

    total_value = sum(holding["market_value"] for holding in holdings)
    for holding in holdings:
        holding["allocation_pct"] = (
            round(holding["market_value"] / total_value * 100, 2) if total_value > 0 else 0
        )

    return {"date": str(latest_date), "holdings": holdings}


def get_portfolio_holdings_history_data(
    db: Session,
    account_ids: List[str],
) -> List[Dict]:
    """All holdings history dates with position counts."""
    rows = db.query(
        HoldingsHistory.date,
        func.count(HoldingsHistory.symbol).label("num_positions"),
    ).filter(
        HoldingsHistory.account_id.in_(account_ids)
    ).group_by(HoldingsHistory.date).order_by(HoldingsHistory.date).all()
    return [{"date": str(row.date), "num_positions": row.num_positions} for row in rows]