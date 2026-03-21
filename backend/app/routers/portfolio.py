"""Portfolio API routes."""

from typing import List, Optional

from datetime import date

from fastapi import APIRouter, Depends, Query, Request, HTTPException
from sqlalchemy.orm import Session

from app.services.post_fill_reconciliation import run_post_fill_reconciliation
from app.schemas import PostFillReconciliationResponse

from app.services.hedge_snapshot_writer import write_hedge_snapshot

from app.services.hedge_close_engine import execute_close_tickets

from app.database import get_db
from app.models import (
    Account,
)
from app.schemas import (
    AccountInfo, PortfolioSummary, PortfolioHoldingsResponse, HoldingsHistoryRow,
    TransactionListResponse, CashFlowRow, PerformancePoint, BenchmarkHistoryResponse,
    TradingSessionsResponse,
    SyncStatus, SyncTriggerResponse, ManualCashFlowRequest, ManualCashFlowResponse,
    ManualCashFlowDeleteResponse,
    AppConfigResponse, SaveSymphonyExportResponse, SaveSymphonyExportRequest,
    OkResponse, ScreenshotUploadResponse, SaveScreenshotConfigRequest, SymphonyExportJobStatus,
)
from app.services.finnhub_market_data import (
    get_daily_closes,
    get_daily_closes_stooq,
    get_latest_price,
)
from app.services.account_scope import resolve_account_ids
from app.services.account_clients import get_client_for_account
from app.services.portfolio_activity_read import (
    get_portfolio_cash_flows_data,
    get_portfolio_transactions_data,
)
from app.services.benchmark_read import get_benchmark_history_data
from app.services.trading_sessions_read import get_trading_sessions_data
from app.services.portfolio_holdings_read import (
    get_portfolio_holdings_data,
    get_portfolio_holdings_history_data,
)
from app.services.portfolio_live_overlay import get_portfolio_live_summary_data
from app.services.portfolio_read import get_portfolio_performance_data, get_portfolio_summary_data
from app.services.portfolio_admin import (
    add_manual_cash_flow_data,
    delete_manual_cash_flow_data,
    cancel_symphony_export_job_data,
    get_app_config_data,
    get_sync_status_data,
    get_symphony_export_job_status_data,
    save_screenshot_config_data,
    save_symphony_export_config_data,
    trigger_sync_data,
    upload_screenshot_data,
)
from app.config import is_test_mode
from app.security import require_local_auth, require_local_strict_origin

from app.services.order_monitor_loop import run_order_monitor
from app.services.order_reprice_engine import reprice_stale_orders
from app.services.broker_submission_store import list_all_orders, summary_stats
from app.schemas import (
    HedgeOrderHistoryResponse, HedgeOrderHistoryRow, RepriceEvent,
    OrderMonitorResponse, OrderCheckResultSchema, RepriceResultSchema,
)

from app.services.crash_simulation_engine import run_crash_simulation
from app.schemas import CrashSimulationResponse, CrashScenarioRow

from app.services.eod_hedge_engine import get_eod_alerts, clear_eod_alerts

router = APIRouter(prefix="/api", tags=["portfolio"])


def _resolve_account_ids(db: Session, account_id: Optional[str]) -> List[str]:
    """Portfolio-scoped account resolution with existing error-message parity."""
    return resolve_account_ids(
        db,
        account_id,
        no_accounts_message="No accounts discovered. Check config.json and restart.",
    )


# ------------------------------------------------------------------
# Accounts
# ------------------------------------------------------------------

@router.get("/accounts", response_model=List[AccountInfo])
def list_accounts(db: Session = Depends(get_db)):
    """List all discovered Composer sub-accounts."""
    query = db.query(Account)
    if is_test_mode():
        query = query.filter(Account.credential_name == "__TEST__")
    else:
        query = query.filter(Account.credential_name != "__TEST__")

    query = query.filter(Account.status == "ACTIVE")
    query = query.filter(Account.id != "7f0a6253-777b-4b8f-abfb-48dc2d66de68")
    accounts = query.order_by(Account.display_name, Account.id).all()
    

    rows = query.order_by(Account.credential_name, Account.account_type).all()
    return [
        AccountInfo(
            id=r.id,
            credential_name=r.credential_name,
            account_type=r.account_type,
            display_name=r.display_name,
            status=r.status,
        )
        for r in rows
    ]


# ------------------------------------------------------------------
# Summary
# ------------------------------------------------------------------

@router.get("/summary", response_model=PortfolioSummary)
def get_summary(
    account_id: Optional[str] = Query(None, description="Sub-account ID or all:<credential_name>"),
    period: Optional[str] = Query(None, description="1W,1M,3M,YTD,1Y,ALL"),
    start_date: Optional[str] = Query(None, description="Custom start date YYYY-MM-DD"),
    end_date: Optional[str] = Query(None, description="Custom end date YYYY-MM-DD"),
    db: Session = Depends(get_db),
):
    """Portfolio summary with metrics, optionally filtered to a time period."""
    ids = _resolve_account_ids(db, account_id)
    return get_portfolio_summary_data(
        db=db,
        account_ids=ids,
        period=period,
        start_date=start_date,
        end_date=end_date,
    )


# ------------------------------------------------------------------
# Live Summary (intraday overlay)
# ------------------------------------------------------------------

@router.get("/summary/live", response_model=PortfolioSummary)
def get_summary_live(
    live_pv: float = Query(..., description="Live portfolio value from symphony data"),
    live_nd: float = Query(..., description="Live net deposits from symphony data"),
    account_id: Optional[str] = Query(None),
    period: Optional[str] = Query(None),
    start_date: Optional[str] = Query(None),
    end_date: Optional[str] = Query(None),
    db: Session = Depends(get_db),
):
    """Portfolio summary with today's value replaced by live symphony data."""
    ids = _resolve_account_ids(db, account_id)
    return get_portfolio_live_summary_data(
        db=db,
        account_ids=ids,
        live_pv=live_pv,
        live_nd=live_nd,
        period=period,
        start_date=start_date,
        end_date=end_date,
    )


# ------------------------------------------------------------------
# Performance
# ------------------------------------------------------------------

@router.get("/performance", response_model=List[PerformancePoint])
def get_performance(
    account_id: Optional[str] = Query(None, description="Sub-account ID or all:<credential_name>"),
    period: Optional[str] = Query(None, description="1W,1M,3M,YTD,1Y,ALL"),
    start_date: Optional[str] = Query(None, description="Custom start date YYYY-MM-DD"),
    end_date: Optional[str] = Query(None, description="Custom end date YYYY-MM-DD"),
    db: Session = Depends(get_db),
):
    """Performance chart data (portfolio value + deposits + returns over time)."""
    ids = _resolve_account_ids(db, account_id)
    return get_portfolio_performance_data(
        db=db,
        account_ids=ids,
        period=period,
        start_date=start_date,
        end_date=end_date,
    )


# ------------------------------------------------------------------
# Holdings
# ------------------------------------------------------------------

@router.get("/holdings", response_model=PortfolioHoldingsResponse)
def get_holdings(
    account_id: Optional[str] = Query(None, description="Sub-account ID or all:<credential_name>"),
    target_date: Optional[str] = Query(None, alias="date", description="YYYY-MM-DD"),
    db: Session = Depends(get_db),
):
    """Holdings for a specific date (defaults to latest)."""
    ids = _resolve_account_ids(db, account_id)
    return get_portfolio_holdings_data(
        db=db,
        account_ids=ids,
        target_date=target_date,
        get_client_for_account_fn=get_client_for_account,
    )

@router.get("/holdings-history", response_model=List[HoldingsHistoryRow])
def get_holdings_history(
    account_id: Optional[str] = Query(None, description="Sub-account ID"),
    db: Session = Depends(get_db),
):
    """All holdings history dates with position counts."""
    ids = _resolve_account_ids(db, account_id)
    return get_portfolio_holdings_history_data(
        db=db,
        account_ids=ids,
    )


# ------------------------------------------------------------------
# Transactions
# ------------------------------------------------------------------

@router.get("/transactions", response_model=TransactionListResponse)
def get_transactions(
    account_id: Optional[str] = Query(None, description="Sub-account ID or all:<credential_name>"),
    symbol: Optional[str] = None,
    limit: int = Query(100, le=5000),
    offset: int = 0,
    db: Session = Depends(get_db),
):
    """Transaction history with optional symbol filter."""
    ids = _resolve_account_ids(db, account_id)
    return get_portfolio_transactions_data(
        db=db,
        account_ids=ids,
        symbol=symbol,
        limit=limit,
        offset=offset,
    )


# ------------------------------------------------------------------
# Cash Flows
# ------------------------------------------------------------------

@router.get("/cash-flows", response_model=List[CashFlowRow])
def get_cash_flows(
    account_id: Optional[str] = Query(None, description="Sub-account ID or all:<credential_name>"),
    db: Session = Depends(get_db),
):
    """All deposits, fees, and dividends."""
    ids = _resolve_account_ids(db, account_id)
    return get_portfolio_cash_flows_data(
        db=db,
        account_ids=ids,
    )

@router.post(
    "/cash-flows/manual",
    response_model=ManualCashFlowResponse,
    dependencies=[Depends(require_local_auth)],
)
def add_manual_cash_flow(
    body: ManualCashFlowRequest,
    db: Session = Depends(get_db),
):
    """The Composer API does not support automatic cash flow detection for certain account types (e.g. Roth IRAs). Manually add a dated deposit/withdrawal for accounts where reports fail."""
    return add_manual_cash_flow_data(
        db,
        body,
        resolve_account_ids_fn=_resolve_account_ids,
        get_client_for_account_fn=get_client_for_account,
    )

@router.delete(
    "/cash-flows/manual/{cash_flow_id}",
    response_model=ManualCashFlowDeleteResponse,
    dependencies=[Depends(require_local_auth)],
)
def delete_manual_cash_flow(
    cash_flow_id: int,
    db: Session = Depends(get_db),
):
    """Delete a manual deposit/withdrawal entry by ID."""
    return delete_manual_cash_flow_data(
        db,
        cash_flow_id,
        resolve_account_ids_fn=_resolve_account_ids,
        get_client_for_account_fn=get_client_for_account,
    )


# ------------------------------------------------------------------
# Sync
# ------------------------------------------------------------------

@router.get("/sync/status", response_model=SyncStatus)
def get_sync_status(
    account_id: Optional[str] = Query(None, description="Sub-account ID"),
    db: Session = Depends(get_db),
):
    """Current sync status."""
    ids = _resolve_account_ids(db, account_id)
    return get_sync_status_data(db, ids[0])

@router.get("/symphony-export/status", response_model=SymphonyExportJobStatus)
def get_symphony_export_status():
    """Current symphony export background job status."""
    return get_symphony_export_job_status_data()

@router.post(
    "/symphony-export/cancel",
    response_model=OkResponse,
    dependencies=[Depends(require_local_auth)],
)
def cancel_symphony_export():
    """Request cancellation of an active symphony export background job."""
    return cancel_symphony_export_job_data()

@router.post(
    "/sync",
    response_model=SyncTriggerResponse,
    dependencies=[Depends(require_local_auth)],
)
def trigger_sync(
    account_id: Optional[str] = Query(None, description="Sub-account ID, all:<credential_name>, or omit to sync all"),
    db: Session = Depends(get_db),
):
    """Trigger data sync. Runs backfill on first call, incremental after."""
    return trigger_sync_data(
        db,
        account_id=account_id,
        resolve_account_ids_fn=_resolve_account_ids,
        get_client_for_account_fn=get_client_for_account,
    )


@router.get(
    "/config",
    response_model=AppConfigResponse,
    dependencies=[Depends(require_local_strict_origin)],
)
def get_app_config():
    """Return client-safe configuration (e.g. Finnhub API key, export settings)."""
    return get_app_config_data()

@router.post(
    "/config/symphony-export",
    response_model=SaveSymphonyExportResponse,
    dependencies=[Depends(require_local_auth)],
)
def set_symphony_export_config(body: SaveSymphonyExportRequest):
    """Save symphony export settings from the frontend settings modal."""
    return save_symphony_export_config_data(body.local_path, body.enabled)

@router.post(
    "/config/screenshot",
    response_model=OkResponse,
    dependencies=[Depends(require_local_auth)],
)
def set_screenshot_config(body: SaveScreenshotConfigRequest):
    """Save screenshot configuration from the frontend settings modal."""
    return save_screenshot_config_data(body.model_dump())

@router.post(
    "/screenshot",
    response_model=ScreenshotUploadResponse,
    dependencies=[Depends(require_local_auth)],
)
async def upload_screenshot(request: Request):
    """Receive a PNG screenshot and save it to the configured folder."""
    return await upload_screenshot_data(request)


# ---------------------------------------------------------------------------
# Benchmark history
# ---------------------------------------------------------------------------

@router.get("/benchmark-history", response_model=BenchmarkHistoryResponse)
def get_benchmark_history(
    ticker: str = Query(..., description="Ticker symbol, e.g. SPY"),
    start_date: Optional[str] = Query(None),
    end_date: Optional[str] = Query(None),
    account_id: Optional[str] = Query(None),
    db: Session = Depends(get_db),
):
    """Fetch benchmark price history and compute TWR, drawdown, and MWR series."""
    return get_benchmark_history_data(
        db=db,
        ticker=ticker,
        start_date=start_date,
        end_date=end_date,
        account_id=account_id,
        get_daily_closes_stooq_fn=get_daily_closes_stooq,
        get_daily_closes_fn=get_daily_closes,
        get_latest_price_fn=get_latest_price,
    )


@router.get("/trading-sessions", response_model=TradingSessionsResponse)
def get_trading_sessions(
    start_date: str = Query(..., description="Start date YYYY-MM-DD"),
    end_date: str = Query(..., description="End date YYYY-MM-DD"),
    exchange: str = Query("XNYS", description="Exchange calendar code, e.g. XNYS"),
):
    """Return exchange session dates for the requested range."""
    return get_trading_sessions_data(
        exchange=exchange,
        start_date=start_date,
        end_date=end_date,
    )



from app.schemas import PortfolioBetaResponse
from app.services.portfolio_risk_read import get_portfolio_beta_data

@router.get("/risk/beta", response_model=PortfolioBetaResponse)
def get_portfolio_beta(
    account_id: Optional[str] = Query(None, description="Sub-account ID or all:<credential_name>"),
    target_date: Optional[str] = Query(None, alias="date", description="YYYY-MM-DD"),
    db: Session = Depends(get_db),
):
    """Portfolio beta and beta-adjusted ticker exposure."""
    ids = _resolve_account_ids(db, account_id)
    return get_portfolio_beta_data(
        db=db,
        account_ids=ids,
        target_date=target_date,
    )

from app.schemas import HedgeIntelligenceResponse
from app.services.hedge_intelligence_read import get_hedge_intelligence_data

@router.get("/risk/hedge-intelligence", response_model=HedgeIntelligenceResponse)
def get_hedge_intelligence(
    account_id: Optional[str] = Query(None, description="Sub-account ID or all:<credential_name>"),
    target_date: Optional[str] = Query(None, alias="date", description="YYYY-MM-DD"),
    db: Session = Depends(get_db),
):
    ids = _resolve_account_ids(db, account_id)
    return get_hedge_intelligence_data(
        db=db,
        account_ids=ids,
        target_date=target_date,
    )

from app.services.hedge_history_read import (
    get_hedge_history_data,
    get_hedge_attribution_summary_data,
)
from app.schemas import HedgeHistoryResponse, HedgeAttributionSummaryResponse

@router.get("/hedge/history", response_model=HedgeHistoryResponse)
def get_hedge_history(
    account_id: str,
    start_date: str,
    end_date: str,
    db: Session = Depends(get_db),
):
    ids = resolve_account_ids(db, account_id)
    return get_hedge_history_data(
        db=db,
        account_ids=ids,
        start_date=start_date,
        end_date=end_date,
    )

@router.post("/hedge/history/snapshot")
def write_hedge_history_snapshot(
    account_id: Optional[str] = Query(None),
    target_date: Optional[str] = Query(
        None,
        description="ISO date to write snapshot for (default: today). "
                    "Format: YYYY-MM-DD",
    ),
    db: Session = Depends(get_db),
):
    """
    Write a hedge snapshot for the given date to the hedge_snapshots table.
 
    Use cases:
      - Seed the first row:    POST /api/hedge/history/snapshot
      - Backfill a past date:  POST /api/hedge/history/snapshot?target_date=2026-03-10
      - Re-run today:          POST /api/hedge/history/snapshot
 
    Note: historical backfill will use today's holdings (Composer doesn't
    expose historical holdings via API), so beta and hedge exposure values
    will reflect the current portfolio composition, not the historical one.
    The market regime signals will be correct for the target date.
 
    This endpoint is idempotent — running it twice for the same date
    overwrites the previous row.
    """
    result = write_hedge_snapshot(
        account_id=account_id or "all",
        target_date=target_date,
        db=db,
    )
    return result


@router.get("/hedge/attribution", response_model=HedgeAttributionSummaryResponse)
def get_hedge_attribution(
    account_id: str,
    start_date: str,
    end_date: str,
    db: Session = Depends(get_db),
):
    ids = resolve_account_ids(db, account_id)
    return get_hedge_attribution_summary_data(
        db=db,
        account_ids=ids,
        start_date=start_date,
        end_date=end_date,
    )

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


from app.schemas import HedgeExecutionPlanResponse, HedgeStyleType
from app.services.hedge_execution_planner import build_hedge_execution_plan


@router.get("/hedge/plan", response_model=HedgeExecutionPlanResponse)
def get_hedge_plan(
    account_id: str,
    target_date: str | None = None,
    hedge_style: HedgeStyleType | None = None,
    db: Session = Depends(get_db),
):
    ids = resolve_account_ids(db, account_id)

    hedge = get_hedge_intelligence_data(
        db=db,
        account_ids=ids,
        target_date=target_date,
    )

    if hedge_style is None:
        hedge_style = _auto_hedge_style(hedge.market_regime)

    return build_hedge_execution_plan(
        as_of_date=hedge.as_of_date,
        underlying="QQQ",
        market_regime=hedge.market_regime,
        hedge_style=hedge_style,
        portfolio_value=hedge.portfolio_value,
        recommended_hedge_pct=hedge.recommended_hedge_pct,
        additional_hedge_pct=hedge.additional_hedge_pct,
        remaining_hedge_budget_pct=hedge.remaining_hedge_budget_pct,
        vix_level=float(getattr(hedge, "vix_level", 20.0) or 20.0),
    )


from app.schemas import HedgeSpreadSelectionResponse, HedgeStyleType
from app.services.option_selector import select_hedge_spreads


@router.get("/hedge/select", response_model=HedgeSpreadSelectionResponse)
def get_hedge_selection(
    account_id: str,
    target_date: str | None = None,
    hedge_style: HedgeStyleType | None = None,
    db: Session = Depends(get_db),
):
    ids = resolve_account_ids(db, account_id)

    hedge = get_hedge_intelligence_data(
        db=db,
        account_ids=ids,
        target_date=target_date,
    )

    if hedge_style is None:
        hedge_style = _auto_hedge_style(hedge.market_regime)

    as_of_date = hedge.as_of_date or str(target_date or "")

    return select_hedge_spreads(
        as_of_date=as_of_date,
        underlying="QQQ",
        market_regime=hedge.market_regime,
        hedge_style=hedge_style,
    )

from app.schemas import HedgeRollEngineResponse, HedgeStyleType
from app.services.hedge_roll_engine import build_hedge_roll_engine

@router.get("/hedge/roll", response_model=HedgeRollEngineResponse)
def get_hedge_roll_engine(
    account_id: str,
    target_date: str | None = None,
    hedge_style: HedgeStyleType | None = None,
    db: Session = Depends(get_db),
):
    ids = resolve_account_ids(db, account_id)

    hedge = get_hedge_intelligence_data(
        db=db,
        account_ids=ids,
        target_date=target_date,
    )

    if hedge_style is None:
        hedge_style = _auto_hedge_style(hedge.market_regime)

    return build_hedge_roll_engine(
        as_of_date=hedge.as_of_date,
        underlying="QQQ",
        market_regime=hedge.market_regime,
        hedge_style=hedge_style,
        portfolio_value=hedge.portfolio_value,
        current_hedge_pct=hedge.current_hedge_pct,
        recommended_hedge_pct=hedge.recommended_hedge_pct,
        additional_hedge_pct=hedge.additional_hedge_pct,
        remaining_hedge_budget_pct=hedge.remaining_hedge_budget_pct,
        vix_level=float(getattr(hedge, "vix_level", 20.0) or 20.0),
    )

from app.schemas import HedgeReconciliationResponse, HedgeStyleType
from app.services.hedge_reconciliation_engine import build_hedge_reconciliation_engine

@router.get("/hedge/reconcile", response_model=HedgeReconciliationResponse)
def get_hedge_reconciliation(
    account_id: str,
    target_date: str | None = None,
    hedge_style: HedgeStyleType | None = None,
    db: Session = Depends(get_db),
):
    ids = resolve_account_ids(db, account_id)

    hedge = get_hedge_intelligence_data(
        db=db,
        account_ids=ids,
        target_date=target_date,
    )

    if hedge_style is None:
        hedge_style = _auto_hedge_style(hedge.market_regime)

    return build_hedge_reconciliation_engine(
        db=db,
        account_ids=ids,
        as_of_date=hedge.as_of_date,
        underlying="QQQ",
        market_regime=hedge.market_regime,
        hedge_style=hedge_style,
        portfolio_value=hedge.portfolio_value,
        current_hedge_pct=hedge.current_hedge_pct,
        recommended_hedge_pct=hedge.recommended_hedge_pct,
        additional_hedge_pct=hedge.additional_hedge_pct,
        remaining_hedge_budget_pct=hedge.remaining_hedge_budget_pct,
        vix_level=float(getattr(hedge, "vix_level", 20.0) or 20.0),
    )


from app.schemas import HedgeTradeTicketResponse, HedgeStyleType
from app.services.hedge_trade_ticket_engine import build_hedge_trade_tickets

@router.get("/hedge/tickets", response_model=HedgeTradeTicketResponse)
def get_hedge_trade_tickets(
    account_id: str,
    target_date: str | None = None,
    hedge_style: HedgeStyleType | None = None,
    db: Session = Depends(get_db),
):
    ids = resolve_account_ids(db, account_id)

    hedge = get_hedge_intelligence_data(
        db=db,
        account_ids=ids,
        target_date=target_date,
    )

    if hedge_style is None:
        hedge_style = _auto_hedge_style(hedge.market_regime)

    return build_hedge_trade_tickets(
        db=db,
        account_ids=ids,
        as_of_date=hedge.as_of_date,
        underlying="QQQ",
        market_regime=hedge.market_regime,
        hedge_style=hedge_style,
        portfolio_value=hedge.portfolio_value,
        current_hedge_pct=hedge.current_hedge_pct,
        recommended_hedge_pct=hedge.recommended_hedge_pct,
        additional_hedge_pct=hedge.additional_hedge_pct,
        remaining_hedge_budget_pct=hedge.remaining_hedge_budget_pct,
        vix_level=float(getattr(hedge, "vix_level", 20.0) or 20.0),
    )

@router.post("/hedge/close", response_model=list)
def execute_hedge_closes(
    account_id: str,
    mode: str = "preview",
    use_bid: bool = False,
    db: Session = Depends(get_db),
):
    """
    Execute close orders for any exit-trigger tickets.
    mode=preview: show what would be closed (no submission)
    mode=submit:  submit close orders to Alpaca
    """
    ids = resolve_account_ids(db, account_id)

    hedge = get_hedge_intelligence_data(db=db, account_ids=ids)

    hedge_style = _auto_hedge_style(hedge.market_regime)

    ticket_resp = build_hedge_trade_tickets(
        db=db,
        account_ids=ids,
        as_of_date=hedge.as_of_date,
        underlying="QQQ",
        market_regime=hedge.market_regime,
        hedge_style=hedge_style,
        portfolio_value=hedge.portfolio_value,
        current_hedge_pct=hedge.current_hedge_pct,
        recommended_hedge_pct=hedge.recommended_hedge_pct,
        additional_hedge_pct=hedge.additional_hedge_pct,
        remaining_hedge_budget_pct=hedge.remaining_hedge_budget_pct,
    )

    return execute_close_tickets(
        tickets=ticket_resp.tickets,
        mode=mode,
        use_bid=use_bid,
    )
 



from app.schemas import BrokerOrderPayloadResponse
from app.services.broker_execution_engine import execute_broker_orders

@router.get("/hedge/orders", response_model=BrokerOrderPayloadResponse)
def get_hedge_orders(
    account_id: str,
    target_date: str | None = None,
    hedge_style: str | None = None,
    mode: str = "preview",
    limit_price_buffer_pct: float = 0.0,
    max_slippage_pct: float = 0.02,
    db: Session = Depends(get_db),
):
    ids = resolve_account_ids(db, account_id)

    hedge = get_hedge_intelligence_data(
        db=db,
        account_ids=ids,
        target_date=target_date,
    )

    if hedge_style is None:
        hedge_style = _auto_hedge_style(hedge.market_regime)

    as_of_date = hedge.as_of_date or str(target_date or "")

    return execute_broker_orders(
        db=db,
        account_ids=ids,
        as_of_date=as_of_date,
        underlying="QQQ",
        market_regime=hedge.market_regime,
        hedge_style=hedge_style,
        portfolio_value=hedge.portfolio_value,
        current_hedge_pct=hedge.current_hedge_pct,
        recommended_hedge_pct=hedge.recommended_hedge_pct,
        additional_hedge_pct=hedge.additional_hedge_pct,
        remaining_hedge_budget_pct=hedge.remaining_hedge_budget_pct,
        broker="alpaca",
        mode=mode,
        limit_price_buffer_pct=limit_price_buffer_pct,
        max_slippage_pct=max_slippage_pct,
    )

from app.schemas import BrokerOrderStatusResponse
from app.services.broker_order_status_engine import get_broker_order_status

@router.get("/hedge/orders/status", response_model=BrokerOrderStatusResponse)
def get_hedge_order_status(
    broker_order_id: str | None = None,
    client_order_id: str | None = None,
    open_only: bool = True,
    limit: int = 50,
):
    return get_broker_order_status(
        broker="alpaca",
        broker_order_id=broker_order_id,
        client_order_id=client_order_id,
        open_only=open_only,
        limit=limit,
    )

from app.schemas import BrokerCancelResponse
from app.services.broker_cancel_engine import cancel_broker_order

@router.post("/hedge/orders/cancel", response_model=BrokerCancelResponse)
def cancel_hedge_order(
    broker_order_id: str | None = None,
    client_order_id: str | None = None,
):
    return cancel_broker_order(
        broker="alpaca",
        broker_order_id=broker_order_id,
        client_order_id=client_order_id,
    )

# ══════════════════════════════════════════════════════════════════════════════
# ADD TO: app/routers/portfolio.py
#
# Place after the existing /hedge/orders/cancel route (~line 737)
# ══════════════════════════════════════════════════════════════════════════════

from app.services.hedge_close_engine import (
    _submit_single_leg_close,
    _submit_spread_close,
    _compute_close_limit,
    _width_acceptable,
)
from app.services.option_chain_read import get_option_snapshots_alpaca

@router.post("/hedge/close-position")
def close_hedge_position(
    account_id: str,
    long_symbol: str,
    qty: int,
    short_symbol: str | None = None,
    mode: str = "preview",
    use_bid: bool = False,
):
    """
    Close a specific hedge spread or naked put position.

    long_symbol:  The long put leg to sell-to-close
    short_symbol: The short put leg to buy-to-close (None for naked puts)
    qty:          Number of spreads/contracts to close
    mode:         preview | submit
    use_bid:      True to use bid price instead of mid (final attempt)

    Returns list of result dicts, one per leg action.
    """
    symbols = [long_symbol]
    if short_symbol:
        symbols.append(short_symbol)

    snapshots = get_option_snapshots_alpaca(symbols)
    long_snap = snapshots.get(long_symbol, {}) or {}

    results = []

    if short_symbol:
        # Spread close — use long leg bid/ask for width check and limit pricing
        # Net credit = what we receive for the spread
        long_bid = long_snap.get("bid")
        long_ask = long_snap.get("ask")
        short_snap = snapshots.get(short_symbol, {}) or {}
        short_bid = short_snap.get("bid")
        short_ask = short_snap.get("ask")

        # Net credit mid = (long_bid + long_ask)/2 - (short_bid + short_ask)/2
        if all(x is not None for x in [long_bid, long_ask, short_bid, short_ask]):
            long_mid = (long_bid + long_ask) / 2.0
            short_mid = (short_bid + short_ask) / 2.0
            limit_price = round(long_mid - short_mid, 2)
            if use_bid:
                limit_price = round(long_bid - short_ask, 2)
            limit_price = max(limit_price, 0.01)
        else:
            results.append({"submitted": False, "message": "Could not fetch bid/ask for spread close"})
            return results

        from datetime import datetime, timezone
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        client_order_id = f"close-{long_symbol[-12:].lower()}-{ts}"

        result = _submit_spread_close(
            long_symbol=long_symbol,
            short_symbol=short_symbol,
            qty=qty,
            limit_price=limit_price,
            client_order_id=client_order_id,
            mode=mode,
        )
        result.update({
            "long_symbol": long_symbol,
            "short_symbol": short_symbol,
            "contracts": qty,
            "limit_price": limit_price,
            "long_bid": long_bid,
            "long_ask": long_ask,
            "short_bid": short_bid,
            "short_ask": short_ask,
        })
        results.append(result)

    else:
        # Single leg close
        bid = long_snap.get("bid")
        ask = long_snap.get("ask")
        limit_price = _compute_close_limit(bid, ask, use_bid=use_bid)

        if not limit_price:
            results.append({"submitted": False, "message": "Could not compute limit price"})
            return results

        from datetime import datetime, timezone
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        client_order_id = f"close-{long_symbol[-12:].lower()}-{ts}"

        result = _submit_single_leg_close(
            symbol=long_symbol,
            qty=qty,
            limit_price=limit_price,
            client_order_id=client_order_id,
            mode=mode,
        )
        result.update({
            "symbol": long_symbol,
            "contracts": qty,
            "limit_price": limit_price,
            "bid": bid,
            "ask": ask,
        })
        results.append(result)

    return results

@router.get("/hedge/orders/open", response_model=BrokerOrderStatusResponse)
def get_open_hedge_orders(
    hedge_only: bool = True,
    limit: int = 50,
):
    return get_broker_order_status(
        broker="alpaca",
        open_only=True,
        limit=limit,
        hedge_only=hedge_only,
    )

from app.schemas import BrokerPositionsResponse
from app.services.broker_positions_engine import get_broker_positions

@router.get("/hedge/positions/alpaca", response_model=BrokerPositionsResponse)
def get_alpaca_positions(
    symbol: str | None = None,
):
    return get_broker_positions(
        broker="alpaca",
        symbol=symbol,
    )

from app.schemas import UnifiedHoldingsResponse
from app.services.unified_holdings_engine import load_unified_holdings


@router.get("/hedge/holdings/unified", response_model=UnifiedHoldingsResponse)
def get_unified_holdings(
    account_id: Optional[str] = Query(None, description="Sub-account ID or all:<credential_name>"),
    include_composer: bool = True,
    include_alpaca: bool = True,
    db: Session = Depends(get_db),
):
    composer_account_ids = _resolve_account_ids(db, account_id) if include_composer else []

    return load_unified_holdings(
        db=db,
        composer_account_ids=composer_account_ids,
        include_composer=include_composer,
        include_alpaca=include_alpaca,
    )


# ══════════════════════════════════════════════════════════════════════════════
# PART 2 — New routes to add to app/routers/portfolio.py
#
# Add these two routes after the existing /hedge/orders/open route (~line 751).
# They need these imports at the top of portfolio.py (add if not present):
#
#   from app.services.order_monitor_loop import run_order_monitor
#   from app.services.order_reprice_engine import reprice_stale_orders
#   from app.services.broker_submission_store import list_all_orders, summary_stats
#   from app.schemas import (
#       HedgeOrderHistoryResponse, HedgeOrderHistoryRow, RepriceEvent,
#       OrderMonitorResponse, OrderCheckResultSchema, RepriceResultSchema,
#   )
# ══════════════════════════════════════════════════════════════════════════════
 
@router.get("/hedge/orders/history", response_model=HedgeOrderHistoryResponse)
def get_hedge_order_history(
    limit: int = 100,
    state: Optional[str] = None,   # e.g. "filled" or "open" to filter
):
    """
    Persistent audit trail of all hedge orders with lifecycle state,
    fill prices, and actual debit spent.
 
    Unlike /hedge/orders/status (which queries Alpaca live), this endpoint
    reads the local submission store and shows what actually happened over time.
 
    Query params:
      limit  — max rows to return (default 100)
      state  — filter to a specific lifecycle_state (e.g. "filled")
    """
    state_filter = [state] if state else None
    rows = list_all_orders(limit=limit, state_filter=state_filter)
    stats = summary_stats()
 
    history_rows = []
    for r in rows:
        history_rows.append(
            HedgeOrderHistoryRow(
                client_order_id=r.get("client_order_id", ""),
                broker_order_id=r.get("broker_order_id"),
                broker=r.get("broker", "alpaca"),
                broker_environment=r.get("broker_environment", "paper"),
                underlying=r.get("underlying"),
                ticket_bucket=r.get("ticket_bucket"),
                ticket_action=r.get("ticket_action"),
                mode=r.get("mode", "submit"),
                submitted_at_utc=r.get("submitted_at_utc"),
                submission_status=r.get("submission_status"),
                lifecycle_state=r.get("lifecycle_state", "submitted"),
                last_checked_utc=r.get("last_checked_utc"),
                filled_at_utc=r.get("filled_at_utc"),
                avg_fill_price=r.get("avg_fill_price"),
                filled_qty=r.get("filled_qty"),
                qty=r.get("qty"),
                estimated_debit_dollars=r.get("estimated_debit_dollars"),
                estimated_coverage_dollars=r.get("estimated_coverage_dollars"),
                actual_debit_dollars=r.get("actual_debit_dollars"),
                reprice_count=r.get("reprice_count", 0),
                reprice_history=[
                    RepriceEvent(**ev) for ev in r.get("reprice_history", [])
                ],
                replaced_by_client_order_id=r.get("replaced_by_client_order_id"),
            )
        )
 
    return HedgeOrderHistoryResponse(
        as_of_date=date.today().isoformat(),
        total_orders=stats["total_orders"],
        filled=stats["filled"],
        open=stats["open"],
        cancelled=stats["cancelled"],
        expired=stats["expired"],
        total_actual_debit_dollars=stats["total_actual_debit_dollars"],
        orders=history_rows,
        notes=[
            "Data sourced from local submission store, not live Alpaca API.",
            "Use /hedge/orders/status for live Alpaca order state.",
        ],
    )
 
 
@router.get("/hedge/orders/monitor", response_model=OrderMonitorResponse)
def run_hedge_order_monitor(
    reprice: bool = True,
    stale_threshold_minutes: int = 30,
):
    """
    Run one cycle of the order monitor loop.
 
    Checks all open hedge orders against Alpaca, updates the submission store,
    and optionally reprices stale orders.
 
    Query params:
      reprice                 — if True (default), automatically reprice stale orders
      stale_threshold_minutes — how old an order must be to be considered stale (default 30)
 
    Returns:
      - newly_filled    : orders that filled this cycle
      - newly_cancelled : orders that were cancelled or expired
      - stale_orders    : orders open longer than the threshold (before any reprice)
      - reprice_results : reprice actions taken (if reprice=True)
    """
    monitor_result = run_order_monitor(stale_threshold_minutes=stale_threshold_minutes)
 
    reprice_results = []
    if reprice and monitor_result.has_stale:
        raw_reprice = reprice_stale_orders(monitor_result.stale_orders)
        reprice_results = [
            RepriceResultSchema(
                original_client_order_id=r.original_client_order_id,
                new_client_order_id=r.new_client_order_id,
                old_limit_price=r.old_limit_price,
                new_limit_price=r.new_limit_price,
                reprice_number=r.reprice_number,
                submitted=r.submitted,
                broker_order_id=r.broker_order_id,
                status=r.status,
                message=r.message,
            )
            for r in raw_reprice
        ]
 
    def _to_schema(results):
        return [
            OrderCheckResultSchema(
                client_order_id=r.client_order_id,
                broker_order_id=r.broker_order_id,
                previous_state=r.previous_state,
                new_state=r.new_state,
                changed=r.changed,
                is_stale=r.is_stale,
                fill_price=r.fill_price,
                filled_qty=r.filled_qty,
                actual_debit_dollars=r.actual_debit_dollars,
                error=r.error,
            )
            for r in results
        ]
 
    notes = []
    if monitor_result.has_fills:
        notes.append(
            f"{len(monitor_result.newly_filled)} order(s) filled — "
            "run /hedge/reconcile to recompute hedge gap."
        )
    if monitor_result.has_stale and not reprice:
        notes.append(
            f"{len(monitor_result.stale_orders)} stale order(s) found — "
            "call with reprice=true to reprice automatically."
        )
 
    return OrderMonitorResponse(
        as_of_date=date.today().isoformat(),
        orders_checked=len(monitor_result.results),
        newly_filled=_to_schema(monitor_result.newly_filled),
        newly_cancelled=_to_schema(monitor_result.newly_cancelled),
        stale_orders=_to_schema(monitor_result.stale_orders),
        reprice_results=reprice_results,
        errors=_to_schema(monitor_result.errors),
        action_needed=monitor_result.has_stale or len(monitor_result.errors) > 0,
        notes=notes,
    )

@router.post("/hedge/reconcile/post-fill", response_model=PostFillReconciliationResponse)
def trigger_post_fill_reconciliation(
    account_id: Optional[str] = Query(None),
):
    """
    Re-run hedge intelligence and reconciliation after fills are detected.
 
    Called automatically by the scheduler when the monitor detects fills.
    Can also be called manually after confirming fills via /hedge/orders/history.
 
    What it does:
      1. Re-runs hedge intelligence — picks up new Alpaca positions
      2. Recomputes current_hedge_pct, gap, and budget
      3. Re-runs reconciliation — generates updated actions
      4. Returns whether the hedge target has been met
 
    If needs_more_hedge is True, call /hedge/orders?mode=submit to place
    additional orders for the remaining gap.
    """
    # Pull the most recently filled orders from the store to use as context.
    # If called manually with no specific fills, we just re-run the full chain.
    from app.services.broker_submission_store import list_all_orders
    filled_rows = list_all_orders(state_filter=["filled"], limit=20)
 
    # Build lightweight stand-ins for the filled order list.
    # post_fill_reconciliation only needs .client_order_id from each entry.
    class _FillStub:
        def __init__(self, client_order_id: str):
            self.client_order_id = client_order_id
 
    filled_stubs = [_FillStub(r["client_order_id"]) for r in filled_rows]
 
    result = run_post_fill_reconciliation(
    filled_orders=filled_stubs,
    account_id=account_id or "all",   # already correct — just double-check
    )
 
    notes = []
    if result.target_met:
        notes.append("Hedge target met — no additional orders needed.")
    elif result.needs_more_hedge:
        notes.append(
            f"Hedge gap remains: {result.remaining_gap_pct * 100:.1f}% "
            f"(${result.remaining_gap_dollars:,.0f}). "
            "Call /hedge/orders?mode=submit to fill the gap."
        )
    if result.alpaca_hedge_exposure_dollars > 0:
        notes.append(
            f"Alpaca hedge sleeve: ${result.alpaca_hedge_exposure_dollars:,.0f} "
            "(now reflected in hedge intelligence)."
        )
    if result.error and result.error != "No fills to process.":
        notes.append(f"Error during reconciliation: {result.error}")


 
    return PostFillReconciliationResponse(
        as_of_date=result.as_of_date,
        triggered_by=result.triggered_by,
        current_hedge_pct=result.current_hedge_pct,
        recommended_hedge_pct=result.recommended_hedge_pct,
        remaining_gap_pct=result.remaining_gap_pct,
        remaining_gap_dollars=result.remaining_gap_dollars,
        remaining_budget_dollars=result.remaining_budget_dollars,
        alpaca_hedge_exposure_dollars=result.alpaca_hedge_exposure_dollars,
        target_met=result.target_met,
        needs_more_hedge=result.needs_more_hedge,
        immediate_actions=result.immediate_actions,
        error=result.error,
        notes=notes,
    )

@router.get("/hedge/crash-sim", response_model=CrashSimulationResponse)
def get_crash_simulation(
    account_id: Optional[str] = Query(None),
    scenarios: Optional[str] = Query(None),
    db: Session = Depends(get_db),
):
    ids = _resolve_account_ids(db, account_id or "all")

    scenarios_pct = None
    if scenarios:
        try:
            scenarios_pct = [float(s.strip()) / 100.0 for s in scenarios.split(",") if s.strip()]
        except ValueError:
            pass

    hedge_intel = get_hedge_intelligence_data(db=db, account_ids=ids)

    current_pct = max(float(hedge_intel.current_hedge_pct or 0.01), 0.01)
    recommended_pct = float(hedge_intel.recommended_hedge_pct or current_pct)
    scale = recommended_pct / current_pct

    structural = hedge_intel.structural_hedge_exposure_dollars
    options = hedge_intel.option_hedge_exposure_dollars
    fully_hedged_structural = structural * scale
    fully_hedged_options = options * scale

    # Sim 1 — current hedge state
    sim_current = run_crash_simulation(
        portfolio_value=hedge_intel.portfolio_value,
        portfolio_beta=hedge_intel.portfolio_beta,
        portfolio_crash_beta=hedge_intel.portfolio_crash_beta,
        structural_hedge_exposure_dollars=structural,
        option_hedge_exposure_dollars=options,
        scenarios_pct=scenarios_pct,
    )

    # Sim 2 — at recommended hedge level
    sim_full = run_crash_simulation(
        portfolio_value=hedge_intel.portfolio_value,
        portfolio_beta=hedge_intel.portfolio_beta,
        portfolio_crash_beta=hedge_intel.portfolio_crash_beta,
        structural_hedge_exposure_dollars=fully_hedged_structural,
        option_hedge_exposure_dollars=fully_hedged_options,
        scenarios_pct=scenarios_pct,
    )

    def _rows(sim):
        return [
            CrashScenarioRow(
                drop_pct=s.drop_pct,
                drop_label=s.drop_label,
                portfolio_loss_dollars=s.portfolio_loss_dollars,
                structural_gain_dollars=s.structural_gain_dollars,
                option_gain_dollars=s.option_gain_dollars,
                total_hedge_gain_dollars=s.total_hedge_gain_dollars,
                net_dollars=s.net_dollars,
                hedge_offset_pct=s.hedge_offset_pct,
                structural_decay_factor=s.structural_decay_factor,
                option_convexity_factor=s.option_convexity_factor,
            )
            for s in sim.scenarios
        ]

    return CrashSimulationResponse(
        as_of_date=hedge_intel.as_of_date,
        market_regime=hedge_intel.market_regime,
        portfolio_value=sim_current.portfolio_value,
        portfolio_beta=sim_current.portfolio_beta,
        portfolio_crash_beta=sim_current.portfolio_crash_beta,
        portfolio_crash_beta_dollars=sim_current.portfolio_crash_beta_dollars,
        structural_hedge_exposure_dollars=structural,
        option_hedge_exposure_dollars=options,
        total_hedge_exposure_dollars=structural + options,
        current_hedge_pct=current_pct,
        recommended_hedge_pct=recommended_pct,
        fully_hedged_structural_dollars=fully_hedged_structural,
        fully_hedged_option_dollars=fully_hedged_options,
        scenarios=_rows(sim_current),
        scenarios_fully_hedged=_rows(sim_full),
        notes=sim_current.notes + [
            f"scenarios_fully_hedged assumes hedge scaled from "
            f"{current_pct*100:.1f}% to {recommended_pct*100:.1f}% "
            f"(scale factor {scale:.2f}x)."
        ],
    )

@router.get("/hedge/eod-alerts")
def get_hedge_eod_alerts(
    date: Optional[str] = Query(None, description="Filter to specific date (YYYY-MM-DD)"),
):
    """
    Return EOD hedge alerts — wide spread warnings and no-fill notifications.
    Used by the dashboard to show actionable warnings when automation skips
    a submission due to bad market conditions.
    """
    return {
        "alerts": get_eod_alerts(date=date),
        "as_of": date or "all",
    }
 
 
@router.post("/hedge/eod-alerts/clear")
def clear_hedge_eod_alerts(
    date: Optional[str] = Query(None, description="Clear alerts for this date"),
):
    """Clear resolved EOD alerts."""
    clear_eod_alerts(date=date)
    return {"cleared": True, "date": date}

from app.services.hedge_dashboard_bundle import build_hedge_dashboard_bundle

@router.get("/hedge/dashboard")
def get_hedge_dashboard(
    account_id: str = "all",
    target_date: str | None = None,
    hedge_style: HedgeStyleType | None = None,
    scenarios: str | None = None,
    db: Session = Depends(get_db),
):
    ids = resolve_account_ids(db, account_id)
    return build_hedge_dashboard_bundle(
        db=db,
        account_ids=ids,
        target_date=target_date,
        hedge_style=hedge_style,
        scenarios=scenarios,
    )