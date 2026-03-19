from __future__ import annotations

import logging
import re
from typing import Dict, List, Optional, Tuple, Any

from sqlalchemy.orm import Session

from app.schemas import PortfolioBetaResponse
from app.services.account_clients import get_client_for_account
from app.services.finnhub_market_data import get_stock_beta
from app.services.portfolio_holdings_read import get_portfolio_holdings_data

logger = logging.getLogger(__name__)

_DEFAULT_BETA = 1.0
_BETA_BENCHMARK = "SPY"

# Leveraged / inverse / structural manual betas
_MANUAL_BETA_MAP: Dict[str, float] = {
    "TQQQ": 3.45,
    "SQQQ": -3.45,
    "PSQ": -1.15,
    "SOXS": -3.80,
    "TECL": 3.60,
    "USD": 2.00,
    "MIDU": 3.00,
    "DUSL": 3.00,
    "GUSH": 3.00,
    "ERX": 2.20,
    "UTSL": 2.40,
    "UVXY": -2.50,
    "VIXM": -1.50,
    "SVXY": 0.50,
    "TMF": -1.20,
    "TMV": 1.20,
    "AGQ": 0.40,
    "GDXU": 1.80,
    "UGL": 0.10,
    "EUM": -0.65,
}

# Stable non-API mappings
_STABLE_BETA_MAP: Dict[str, float] = {
    "SPY": 1.00,
    "QQQ": 1.15,
    "BIL": 0.00,
    "BOXX": 0.05,
    "GLD": 0.05,
    "SLV": 0.20,
    "PDBC": 0.20,
    "XLU": 0.35,
    "XLE": 1.10,
    "XHS": 0.90,
    "MLPX": 0.85,
    "DGRW": 0.95,
    "XOP": 1.35,
    "BE": 1.40,
}

_SPECIAL_CASE_BETA: Dict[str, float] = {
    "MSTR": 3.20,
}


def _is_option_symbol(symbol: str) -> bool:
    """
    Crude OCC-style option symbol detector, good enough for dashboard use.
    Examples:
      QQQ260515P00550000
      SPY250919C00550000
    """
    if not symbol:
        return False
    return bool(re.fullmatch(r"[A-Z]+[0-9]{6}[CP][0-9]{8}", symbol))


def resolve_beta(symbol: str) -> Tuple[float, str]:
    if not symbol:
        return _DEFAULT_BETA, "default"

    if _is_option_symbol(symbol):
        # Placeholder for options for now.
        # If market_value is zero, they contribute nothing anyway.
        return 0.0, "option_placeholder"

    if symbol in _MANUAL_BETA_MAP:
        return float(_MANUAL_BETA_MAP[symbol]), "manual"

    if symbol in _SPECIAL_CASE_BETA:
        return float(_SPECIAL_CASE_BETA[symbol]), "manual"

    if symbol in _STABLE_BETA_MAP:
        return float(_STABLE_BETA_MAP[symbol]), "manual"

    try:
        beta = get_stock_beta(symbol)
        if beta is not None:
            return float(beta), "api"
    except Exception as e:
        logger.warning("BETA api fetch failed for %s: %s", symbol, e)

    return _DEFAULT_BETA, "default"


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


def get_portfolio_beta_data(
    db: Session,
    account_ids: List[str],
    target_date: Optional[str] = None,
) -> PortfolioBetaResponse:
    logger.info("BETA account_ids=%s target_date=%s", account_ids, target_date)

    holdings_resp = get_portfolio_holdings_data(
        db=db,
        account_ids=account_ids,
        target_date=target_date,
        get_client_for_account_fn=get_client_for_account,
    )

    logger.info("BETA holdings_resp type=%s", type(holdings_resp).__name__)
    logger.info("BETA raw holdings_resp=%s", holdings_resp)

    holdings = _get_field(holdings_resp, "holdings", [])
    as_of_date = _get_field(holdings_resp, "date", target_date)

    portfolio_value = sum(_get_holding_value(h) for h in holdings)

    logger.info(
        "BETA extracted date=%s portfolio_value=%s holdings_count=%s",
        as_of_date,
        portfolio_value,
        len(holdings or []),
    )

    if not holdings or portfolio_value <= 0:
        return PortfolioBetaResponse(
            date=str(as_of_date or ""),
            benchmark=_BETA_BENCHMARK,
            portfolio_value=round(portfolio_value, 2),
            portfolio_beta=0.0,
            portfolio_dollar_beta=0.0,
            rows=[],
        )

    symbols: List[str] = []
    for h in holdings:
        sym = _get_field(h, "symbol", "")
        if sym and sym != "$USD" and sym not in symbols:
            symbols.append(sym)

    beta_map: Dict[str, float] = {}
    beta_source_map: Dict[str, str] = {}

    for sym in symbols:
        beta, source = resolve_beta(sym)
        beta_map[sym] = float(beta)
        beta_source_map[sym] = source

    rows: List[dict] = []
    portfolio_beta = 0.0
    portfolio_dollar_beta = 0.0

    for h in holdings:
        symbol = _get_field(h, "symbol", "")
        value = _get_holding_value(h)

        if not symbol or symbol == "$USD":
            continue

        weight = value / portfolio_value if portfolio_value > 0 else 0.0
        beta = beta_map.get(symbol, _DEFAULT_BETA)
        beta_source = beta_source_map.get(symbol, "default")

        beta_adjusted_exposure = weight * beta
        dollar_beta_exposure = value * beta

        portfolio_beta += beta_adjusted_exposure
        portfolio_dollar_beta += dollar_beta_exposure

        row_data = {
            "symbol": symbol,
            "value": round(value, 2),
            "weight": round(weight, 6),
            "beta": round(beta, 4),
            "beta_adjusted_exposure": round(beta_adjusted_exposure, 6),
            "dollar_beta_exposure": round(dollar_beta_exposure, 2),
            "beta_source": beta_source,
        }

        rows.append(row_data)

    rows.sort(
        key=lambda r: abs(float(r.get("dollar_beta_exposure", 0.0) or 0.0)),
        reverse=True,
    )

    logger.info(
        "BETA result date=%s portfolio_value=%s portfolio_beta=%s portfolio_dollar_beta=%s rows=%s",
        as_of_date,
        round(portfolio_value, 2),
        round(portfolio_beta, 4),
        round(portfolio_dollar_beta, 2),
        len(rows),
    )

    return PortfolioBetaResponse(
        date=str(as_of_date or ""),
        benchmark=_BETA_BENCHMARK,
        portfolio_value=round(portfolio_value, 2),
        portfolio_beta=round(portfolio_beta, 4),
        portfolio_dollar_beta=round(portfolio_dollar_beta, 2),
        rows=rows,
    )