from __future__ import annotations

from dataclasses import dataclass
from typing import Any


FACTOR_DEFS = {
    "tech": {"proxies": ["QQQ", "XLK"]},
    "btc": {"proxies": ["IBIT", "BITO"]},
    "gold": {"proxies": ["GLD"]},
}

TICKER_FACTOR_MAP = {
    # tech
    "QQQ": "tech",
    "TQQQ": "tech",
    "TECL": "tech",
    "SOXL": "tech",
    "SOXX": "tech",
    "SMH": "tech",
    "SPY": "tech",
    "XLK": "tech",
    "NVDA": "tech",
    "TSLA": "tech",
    "AAPL": "tech",
    "MSFT": "tech",
    "AMZN": "tech",
    "META": "tech",
    "GOOGL": "tech",

    # btc
    "IBIT": "btc",
    "BITO": "btc",
    "MSTR": "btc",
    "COIN": "btc",
    "WULF": "btc",
    "MARA": "btc",
    "RIOT": "btc",
    "CLSK": "btc",
    "IREN": "btc",

    # gold
    "GLD": "gold",
    "IAU": "gold",
    "GDX": "gold",
}

FACTOR_THRESHOLDS = {
    "tech": 0.30,
    "btc": 0.10,
    "gold": 0.10,
}


@dataclass
class FactorExposureRow:
    factor: str
    gross_exposure_dollars: float
    exposure_pct: float
    threshold_pct: float
    excess_pct: float
    hedge_proxy: str | None


def _extract_symbol(position: Any) -> str | None:
    for key in ("symbol", "ticker", "underlying"):
        value = getattr(position, key, None)
        if value:
            return str(value).upper()
    if isinstance(position, dict):
        for key in ("symbol", "ticker", "underlying"):
            value = position.get(key)
            if value:
                return str(value).upper()
    return None


def _extract_market_value(position: Any) -> float:
    if isinstance(position, dict):
        for key in ("market_value", "notional_value", "position_value"):
            value = position.get(key)
            if value is not None:
                return float(value)
        return 0.0

    for key in ("market_value", "notional_value", "position_value"):
        value = getattr(position, key, None)
        if value is not None:
            return float(value)
    return 0.0


def compute_factor_exposures(
    *,
    positions: list[Any],
    portfolio_value: float,
) -> list[FactorExposureRow]:
    factor_totals: dict[str, float] = {}

    for p in positions:
        symbol = _extract_symbol(p)
        if not symbol:
            continue

        factor = TICKER_FACTOR_MAP.get(symbol)
        if not factor:
            continue

        mv = _extract_market_value(p)
        if mv <= 0:
            continue

        factor_totals[factor] = factor_totals.get(factor, 0.0) + mv

    rows: list[FactorExposureRow] = []

    for factor, gross_dollars in factor_totals.items():
        exposure_pct = gross_dollars / portfolio_value if portfolio_value > 0 else 0.0
        threshold_pct = FACTOR_THRESHOLDS.get(factor, 1.0)
        excess_pct = max(exposure_pct - threshold_pct, 0.0)
        hedge_proxy = FACTOR_DEFS.get(factor, {}).get("proxies", [None])[0]

        rows.append(
            FactorExposureRow(
                factor=factor,
                gross_exposure_dollars=gross_dollars,
                exposure_pct=exposure_pct,
                threshold_pct=threshold_pct,
                excess_pct=excess_pct,
                hedge_proxy=hedge_proxy,
            )
        )

    return sorted(rows, key=lambda r: r.gross_exposure_dollars, reverse=True)


def allocate_factor_hedge_budget(
    *,
    factor_rows: list[FactorExposureRow],
    total_budget_dollars: float,
) -> list[dict]:
    total_excess = sum(r.excess_pct for r in factor_rows if r.excess_pct > 0)

    if total_excess <= 0 or total_budget_dollars <= 0:
        return []

    allocations: list[dict] = []

    for row in factor_rows:
        if row.excess_pct <= 0:
            continue

        budget = total_budget_dollars * (row.excess_pct / total_excess)

        allocations.append(
            {
                "factor": row.factor,
                "hedge_proxy": row.hedge_proxy,
                "gross_exposure_dollars": row.gross_exposure_dollars,
                "exposure_pct": row.exposure_pct,
                "threshold_pct": row.threshold_pct,
                "excess_pct": row.excess_pct,
                "allocated_budget_dollars": budget,
            }
        )

    return allocations