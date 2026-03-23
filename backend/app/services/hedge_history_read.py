from __future__ import annotations

from datetime import date, timedelta
from typing import Dict, List, Optional

from sqlalchemy.orm import Session

from app.schemas import HedgeHistoryResponse, HedgeHistoryRow, HedgeAttributionSummaryResponse
from app.services.hedge_intelligence_read import get_hedge_intelligence_data
from app.services.market_signal_read import get_market_regime_signals




def _daterange(start_date: date, end_date: date):
    cur = start_date
    while cur <= end_date:
        yield cur
        cur += timedelta(days=1)


def get_hedge_history_data(
    db: Session,
    account_ids: List[str],
    start_date: str,
    end_date: str,
) -> HedgeHistoryResponse:
    start_dt = date.fromisoformat(start_date)
    end_dt = date.fromisoformat(end_date)

    rows: List[HedgeHistoryRow] = []

    for dt in _daterange(start_dt, end_dt):
        ds = dt.isoformat()

        try:
            hedge = get_hedge_intelligence_data(
                db=db,
                account_ids=account_ids,
                target_date=ds,
            )

            rows.append(
                HedgeHistoryRow(
                    date=ds,
                    portfolio_value=hedge.portfolio_value,
                    current_hedge_exposure_dollars=hedge.current_hedge_exposure_dollars,
                    current_hedge_pct=hedge.current_hedge_pct,
                    structural_hedge_exposure_dollars=hedge.structural_hedge_exposure_dollars,
                    option_hedge_exposure_dollars=hedge.option_hedge_exposure_dollars,
                    current_hedge_premium_market_value=hedge.current_hedge_premium_market_value,
                    current_hedge_premium_cost_basis=hedge.current_hedge_premium_cost_basis,
                    hedge_unrealized_pnl=hedge.hedge_unrealized_pnl,
                    hedged_beta_estimate=hedge.hedged_beta_estimate,
                    unhedged_beta_estimate=hedge.unhedged_beta_estimate,
                )
            )
        except Exception:
            continue

    as_of_date = rows[-1].date if rows else end_date

    return HedgeHistoryResponse(
        as_of_date=as_of_date,
        benchmark="SPY",
        rows=rows,
    )


def get_hedge_attribution_summary_data(
    db: Session,
    account_ids: List[str],
    start_date: str,
    end_date: str,
) -> HedgeAttributionSummaryResponse:
    history = get_hedge_history_data(
        db=db,
        account_ids=account_ids,
        start_date=start_date,
        end_date=end_date,
    )

    rows = history.rows
    if len(rows) < 2:
        return HedgeAttributionSummaryResponse(
            as_of_date=history.as_of_date,
            benchmark="SPY",
            hedge_pnl_ytd=0.0,
            hedge_pnl_ytd_pct=0.0,
            hedge_cost_drag_ytd=0.0,
            hedge_cost_drag_ytd_pct=0.0,
            hedge_benefit_on_down_days=0.0,
            hedge_benefit_on_down_days_pct=0.0,
            hedge_effectiveness_on_drawdowns=0.0,
            average_hedge_capacity_dollars=0.0,
            average_hedge_capacity_pct=0.0,
            best_hedge_day=0.0,
            worst_hedge_day=0.0,
            days_analyzed=0,
        )

    hedge_daily_changes: List[float] = []
    down_day_benefit = 0.0
    total_capacity = 0.0
    total_capacity_pct = 0.0

    for i in range(1, len(rows)):
        prev = rows[i - 1]
        cur = rows[i]

        hedge_change = cur.current_hedge_premium_market_value - prev.current_hedge_premium_market_value
        hedge_daily_changes.append(hedge_change)

        total_capacity += cur.current_hedge_exposure_dollars
        total_capacity_pct += cur.current_hedge_pct

        # crude first-pass drawdown/down-day proxy:
        # if hedged beta estimate fell below unhedged beta, count current hedge mark change as benefit day
        if cur.hedged_beta_estimate < cur.unhedged_beta_estimate and hedge_change > 0:
            down_day_benefit += hedge_change

    hedge_pnl_ytd = rows[-1].current_hedge_premium_market_value - rows[0].current_hedge_premium_market_value
    start_portfolio_value = rows[0].portfolio_value if rows[0].portfolio_value > 0 else 1.0

    hedge_pnl_ytd_pct = hedge_pnl_ytd / start_portfolio_value
    hedge_cost_drag_ytd = max(-hedge_pnl_ytd, 0.0)
    hedge_cost_drag_ytd_pct = hedge_cost_drag_ytd / start_portfolio_value

    hedge_benefit_on_down_days = down_day_benefit
    hedge_benefit_on_down_days_pct = hedge_benefit_on_down_days / start_portfolio_value

    hedge_effectiveness_on_drawdowns = (
        hedge_benefit_on_down_days / hedge_cost_drag_ytd
        if hedge_cost_drag_ytd > 0 else 0.0
    )

    average_hedge_capacity_dollars = total_capacity / max(len(rows) - 1, 1)
    average_hedge_capacity_pct = total_capacity_pct / max(len(rows) - 1, 1)

    best_hedge_day = max(hedge_daily_changes) if hedge_daily_changes else 0.0
    worst_hedge_day = min(hedge_daily_changes) if hedge_daily_changes else 0.0

    return HedgeAttributionSummaryResponse(
        as_of_date=history.as_of_date,
        benchmark="SPY",
        hedge_pnl_ytd=round(hedge_pnl_ytd, 2),
        hedge_pnl_ytd_pct=round(hedge_pnl_ytd_pct, 6),
        hedge_cost_drag_ytd=round(hedge_cost_drag_ytd, 2),
        hedge_cost_drag_ytd_pct=round(hedge_cost_drag_ytd_pct, 6),
        hedge_benefit_on_down_days=round(hedge_benefit_on_down_days, 2),
        hedge_benefit_on_down_days_pct=round(hedge_benefit_on_down_days_pct, 6),
        hedge_effectiveness_on_drawdowns=round(hedge_effectiveness_on_drawdowns, 4),
        average_hedge_capacity_dollars=round(average_hedge_capacity_dollars, 2),
        average_hedge_capacity_pct=round(average_hedge_capacity_pct, 6),
        best_hedge_day=round(best_hedge_day, 2),
        worst_hedge_day=round(worst_hedge_day, 2),
        days_analyzed=len(rows),
    )