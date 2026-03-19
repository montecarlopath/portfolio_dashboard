"""Sync service: backfill and incremental update from Composer API to local DB."""

import logging
import time
from datetime import date, datetime, timedelta
from typing import Optional

from sqlalchemy.orm import Session
from sqlalchemy import func

from app.composer_client import ComposerClient
from app.models import (
    Account,
    Transaction, HoldingsHistory, CashFlow, DailyPortfolio,
    DailyMetrics, BenchmarkData, SyncState, SymphonyAllocationHistory,
    SymphonyDailyPortfolio, SymphonyDailyMetrics,
)
from app.services.holdings import reconstruct_holdings
from app.services.finnhub_market_data import (
    FinnhubAccessError,
    FinnhubError,
    get_daily_closes,
    get_daily_closes_stooq,
)
from app.services.metrics import compute_all_metrics, compute_latest_metrics
from app.config import get_settings
from app.market_hours import is_after_close, get_allocation_target_date

logger = logging.getLogger(__name__)
_INITIAL_SYNC_STEP_RETRIES = 2
_INITIAL_SYNC_STEP_RETRY_DELAY_SECONDS = 2.0

# Map Composer non-trade type codes to our DB types
_CASH_FLOW_TYPE_MAP = {
    ("CSD", ""): "deposit",
    ("CSW", ""): "withdrawal",
    ("FEE", "CAT"): "fee_cat",
    ("FEE", "TAF"): "fee_taf",
    ("DIV", ""): "dividend",
}


def _map_cash_flow_type(type_code: str, subtype: str) -> Optional[str]:
    """Map Composer type/subtype to our simplified type string."""
    key = (type_code, subtype)
    if key in _CASH_FLOW_TYPE_MAP:
        return _CASH_FLOW_TYPE_MAP[key]

    key = (type_code, "")
    if key in _CASH_FLOW_TYPE_MAP:
        return _CASH_FLOW_TYPE_MAP[key]

    if type_code == "CSD":
        return "deposit"
    if type_code == "CSW":
        return "withdrawal"
    if type_code == "FEE":
        return f"fee_{subtype.lower()}" if subtype else "fee"
    if type_code == "DIV":
        return "dividend"
    return None


def get_sync_state(db: Session, account_id: str) -> dict:
    """Read sync state from DB for a specific sub-account."""
    rows = db.query(SyncState).filter_by(account_id=account_id).all()
    return {r.key: r.value for r in rows}


def set_sync_state(db: Session, account_id: str, key: str, value: str):
    existing = db.query(SyncState).filter_by(account_id=account_id, key=key).first()
    if existing:
        existing.value = value
    else:
        db.add(SyncState(account_id=account_id, key=key, value=value))
    db.commit()


def _safe_step(
    label: str,
    fn,
    *args,
    retries: int = 0,
    retry_delay_seconds: float = 0.0,
    raise_on_failure: bool = False,
    **kwargs,
):
    """Run a sync step with optional retries.

    Returns True on success. When `raise_on_failure` is False, returns False
    after final failure and allows the caller to continue.
    """
    max_attempts = max(1, int(retries) + 1)
    for attempt in range(1, max_attempts + 1):
        try:
            fn(*args, **kwargs)
            return True
        except Exception as e:
            if attempt < max_attempts:
                logger.warning(
                    "Sync step '%s' failed on attempt %d/%d: %s. Retrying in %.1fs...",
                    label,
                    attempt,
                    max_attempts,
                    e,
                    retry_delay_seconds,
                )
                if retry_delay_seconds > 0:
                    time.sleep(retry_delay_seconds)
                continue

            if raise_on_failure:
                logger.error(
                    "Sync step '%s' failed after %d attempts: %s",
                    label,
                    max_attempts,
                    e,
                )
                raise

            logger.warning(
                "Sync step '%s' failed after %d attempts (continuing): %s",
                label,
                max_attempts,
                e,
            )
            return False

    return False


def _historical_sync_start() -> str:
    """Central historical start date for full backfills."""
    return "2020-01-01"


def _chunked_date_ranges(since: str, until: Optional[str] = None, chunk_days: int = 90):
    """Yield inclusive [start, end] ISO date ranges in manageable chunks."""
    start = date.fromisoformat(since)
    end = date.fromisoformat(until) if until else date.today()

    while start <= end:
        chunk_end = min(start + timedelta(days=chunk_days - 1), end)
        yield start.isoformat(), chunk_end.isoformat()
        start = chunk_end + timedelta(days=1)


def _refresh_symphony_catalog_safe(db: Session):
    """Wrapper to refresh the symphony catalog during sync (lazy import to avoid circular deps)."""
    from app.services.symphony_catalog import _refresh_symphony_catalog
    _refresh_symphony_catalog(db)


def full_backfill(db: Session, client: ComposerClient, account_id: str):
    """One-time full backfill of all historical data for a sub-account."""
    logger.info("Starting full backfill for account %s...", account_id)

    historical_start = _historical_sync_start()

    tx_ok = _safe_step(
        "transactions",
        _sync_transactions,
        db,
        client,
        account_id,
        since=historical_start,
        retries=_INITIAL_SYNC_STEP_RETRIES,
        retry_delay_seconds=_INITIAL_SYNC_STEP_RETRY_DELAY_SECONDS,
    )

    cf_ok = _safe_step(
        "cash_flows",
        _sync_cash_flows,
        db,
        client,
        account_id,
        since=historical_start,
        retries=_INITIAL_SYNC_STEP_RETRIES,
        retry_delay_seconds=_INITIAL_SYNC_STEP_RETRY_DELAY_SECONDS,
    )

    portfolio_ok = _safe_step(
        "portfolio_history",
        _sync_portfolio_history,
        db,
        client,
        account_id,
        retries=_INITIAL_SYNC_STEP_RETRIES,
        retry_delay_seconds=_INITIAL_SYNC_STEP_RETRY_DELAY_SECONDS,
        raise_on_failure=True,
    )

    holdings_ok = _safe_step(
        "holdings_history",
        _sync_holdings_history,
        db,
        client,
        account_id,
        retries=_INITIAL_SYNC_STEP_RETRIES,
        retry_delay_seconds=_INITIAL_SYNC_STEP_RETRY_DELAY_SECONDS,
    )

    benchmark_ok = _safe_step(
        "benchmark",
        _sync_benchmark,
        db,
        account_id,
        retries=_INITIAL_SYNC_STEP_RETRIES,
        retry_delay_seconds=_INITIAL_SYNC_STEP_RETRY_DELAY_SECONDS,
    )

    metrics_ok = _safe_step(
        "metrics",
        _recompute_metrics,
        db,
        account_id,
        retries=_INITIAL_SYNC_STEP_RETRIES,
        retry_delay_seconds=_INITIAL_SYNC_STEP_RETRY_DELAY_SECONDS,
        raise_on_failure=True,
    )

    _safe_step(
        "symphony_allocations",
        _sync_symphony_allocations,
        db,
        client,
        account_id,
        retries=_INITIAL_SYNC_STEP_RETRIES,
        retry_delay_seconds=_INITIAL_SYNC_STEP_RETRY_DELAY_SECONDS,
    )

    _safe_step(
        "symphony_daily",
        _sync_symphony_daily_backfill,
        db,
        client,
        account_id,
        retries=_INITIAL_SYNC_STEP_RETRIES,
        retry_delay_seconds=_INITIAL_SYNC_STEP_RETRY_DELAY_SECONDS,
    )

    _safe_step(
        "symphony_metrics",
        _recompute_symphony_metrics,
        db,
        account_id,
        retries=_INITIAL_SYNC_STEP_RETRIES,
        retry_delay_seconds=_INITIAL_SYNC_STEP_RETRY_DELAY_SECONDS,
    )

    _safe_step(
        "symphony_catalog",
        _refresh_symphony_catalog_safe,
        db,
        retries=_INITIAL_SYNC_STEP_RETRIES,
        retry_delay_seconds=_INITIAL_SYNC_STEP_RETRY_DELAY_SECONDS,
    )

    if not (tx_ok and cf_ok and portfolio_ok and metrics_ok):
        raise RuntimeError(
            f"Full backfill incomplete for {account_id}: "
            f"tx_ok={tx_ok}, cf_ok={cf_ok}, portfolio_ok={portfolio_ok}, metrics_ok={metrics_ok}"
        )

    set_sync_state(db, account_id, "initial_backfill_core_done", "true")
    set_sync_state(db, account_id, "initial_backfill_done", "true")
    set_sync_state(db, account_id, "last_sync_date", datetime.now().strftime("%Y-%m-%d"))

    logger.info(
        "Full backfill complete for %s "
        "(tx_ok=%s, cf_ok=%s, portfolio_ok=%s, holdings_ok=%s, benchmark_ok=%s, metrics_ok=%s)",
        account_id,
        tx_ok,
        cf_ok,
        portfolio_ok,
        holdings_ok,
        benchmark_ok,
        metrics_ok,
    )



def incremental_update(db: Session, client: ComposerClient, account_id: str):
    """Update data from the last sync date to today for a sub-account."""
    state = get_sync_state(db, account_id)
    last_date = state.get("last_sync_date")
    if not last_date:
        logger.info("No last sync date found for %s — running full backfill instead", account_id)
        full_backfill(db, client, account_id)
        return

    logger.info("Incremental update for %s from %s", account_id, last_date)

    try:
        since_dt = date.fromisoformat(last_date) - timedelta(days=1)
        since = since_dt.isoformat()
    except Exception:
        since = last_date

    tx_ok = _safe_step(
        "transactions",
        _sync_transactions,
        db,
        client,
        account_id,
        since=since,
        retries=_INITIAL_SYNC_STEP_RETRIES,
        retry_delay_seconds=_INITIAL_SYNC_STEP_RETRY_DELAY_SECONDS,
    )

    cf_ok = _safe_step(
        "cash_flows",
        _sync_cash_flows,
        db,
        client,
        account_id,
        since=since,
        retries=_INITIAL_SYNC_STEP_RETRIES,
        retry_delay_seconds=_INITIAL_SYNC_STEP_RETRY_DELAY_SECONDS,
    )

    portfolio_ok = _safe_step(
        "portfolio_history",
        _sync_portfolio_history,
        db,
        client,
        account_id,
        retries=_INITIAL_SYNC_STEP_RETRIES,
        retry_delay_seconds=_INITIAL_SYNC_STEP_RETRY_DELAY_SECONDS,
        raise_on_failure=True,
    )

    holdings_ok = _safe_step(
        "holdings_history",
        _sync_holdings_history,
        db,
        client,
        account_id,
        retries=_INITIAL_SYNC_STEP_RETRIES,
        retry_delay_seconds=_INITIAL_SYNC_STEP_RETRY_DELAY_SECONDS,
    )

    _safe_step(
        "benchmark",
        _sync_benchmark,
        db,
        account_id,
        retries=_INITIAL_SYNC_STEP_RETRIES,
        retry_delay_seconds=_INITIAL_SYNC_STEP_RETRY_DELAY_SECONDS,
    )

    metrics_ok = _safe_step(
        "metrics",
        _recompute_metrics,
        db,
        account_id,
        retries=_INITIAL_SYNC_STEP_RETRIES,
        retry_delay_seconds=_INITIAL_SYNC_STEP_RETRY_DELAY_SECONDS,
        raise_on_failure=True,
    )

    _safe_step(
        "symphony_allocations",
        _sync_symphony_allocations,
        db,
        client,
        account_id,
        retries=_INITIAL_SYNC_STEP_RETRIES,
        retry_delay_seconds=_INITIAL_SYNC_STEP_RETRY_DELAY_SECONDS,
    )

    _safe_step(
        "symphony_daily",
        _sync_symphony_daily_incremental,
        db,
        client,
        account_id,
        retries=_INITIAL_SYNC_STEP_RETRIES,
        retry_delay_seconds=_INITIAL_SYNC_STEP_RETRY_DELAY_SECONDS,
    )

    _safe_step(
        "symphony_metrics",
        _recompute_symphony_metrics,
        db,
        account_id,
        retries=_INITIAL_SYNC_STEP_RETRIES,
        retry_delay_seconds=_INITIAL_SYNC_STEP_RETRY_DELAY_SECONDS,
    )

    _safe_step(
        "symphony_catalog",
        _refresh_symphony_catalog_safe,
        db,
        retries=_INITIAL_SYNC_STEP_RETRIES,
        retry_delay_seconds=_INITIAL_SYNC_STEP_RETRY_DELAY_SECONDS,
    )

    # For some account types (notably certain IRA accounts), Composer may reject
    # trade/non-trade report endpoints with 400s. Allow incremental sync to succeed
    # as long as portfolio history + metrics succeeded.
    if not (portfolio_ok and metrics_ok):
        raise RuntimeError(
            f"Incremental update incomplete for {account_id}: "
            f"tx_ok={tx_ok}, cf_ok={cf_ok}, portfolio_ok={portfolio_ok}, metrics_ok={metrics_ok}"
        )

    if not tx_ok:
        logger.warning(
            "Transaction incremental sync unavailable for %s; continuing with "
            "portfolio-history-based update only.",
            account_id,
        )

    if not cf_ok:
        logger.warning(
            "Cash-flow incremental sync unavailable for %s; continuing with "
            "portfolio-history-based update and fallback net_deposits where needed.",
            account_id,
        )

    set_sync_state(db, account_id, "last_sync_date", datetime.now().strftime("%Y-%m-%d"))
    logger.info(
        "Incremental update complete for %s "
        "(tx_ok=%s, cf_ok=%s, portfolio_ok=%s, holdings_ok=%s, metrics_ok=%s)",
        account_id,
        tx_ok,
        cf_ok,
        portfolio_ok,
        holdings_ok,
        metrics_ok,
    )

def full_backfill_core(db: Session, client: ComposerClient, account_id: str):
    """First-sync core backfill with blocking non-trade activity sync.

    Ensures non-trade activity (cash flows) is applied before portfolio history
    and metrics are returned to the user for the first dashboard load.
    Trade transactions continue in finish_initial_backfill_activity().
    """
    logger.info("Starting first-sync core backfill for account %s...", account_id)

    historical_start = _historical_sync_start()

    cf_ok = _safe_step(
        "cash_flows",
        _sync_cash_flows,
        db,
        client,
        account_id,
        since=historical_start,
        retries=_INITIAL_SYNC_STEP_RETRIES,
        retry_delay_seconds=_INITIAL_SYNC_STEP_RETRY_DELAY_SECONDS,
    )
    portfolio_ok = _safe_step(
        "portfolio_history",
        _sync_portfolio_history,
        db,
        client,
        account_id,
        retries=_INITIAL_SYNC_STEP_RETRIES,
        retry_delay_seconds=_INITIAL_SYNC_STEP_RETRY_DELAY_SECONDS,
        raise_on_failure=True,
    )
    metrics_ok = _safe_step(
        "metrics",
        _recompute_metrics,
        db,
        account_id,
        retries=_INITIAL_SYNC_STEP_RETRIES,
        retry_delay_seconds=_INITIAL_SYNC_STEP_RETRY_DELAY_SECONDS,
        raise_on_failure=True,
    )

    _safe_step(
        "holdings_history",
        _sync_holdings_history,
        db,
        client,
        account_id,
        retries=_INITIAL_SYNC_STEP_RETRIES,
        retry_delay_seconds=_INITIAL_SYNC_STEP_RETRY_DELAY_SECONDS,
    )
    _safe_step(
        "benchmark",
        _sync_benchmark,
        db,
        account_id,
        retries=_INITIAL_SYNC_STEP_RETRIES,
        retry_delay_seconds=_INITIAL_SYNC_STEP_RETRY_DELAY_SECONDS,
    )

    _safe_step(
        "symphony_allocations",
        _sync_symphony_allocations,
        db,
        client,
        account_id,
        retries=_INITIAL_SYNC_STEP_RETRIES,
        retry_delay_seconds=_INITIAL_SYNC_STEP_RETRY_DELAY_SECONDS,
    )
    _safe_step(
        "symphony_daily",
        _sync_symphony_daily_backfill,
        db,
        client,
        account_id,
        retries=_INITIAL_SYNC_STEP_RETRIES,
        retry_delay_seconds=_INITIAL_SYNC_STEP_RETRY_DELAY_SECONDS,
    )
    _safe_step(
        "symphony_metrics",
        _recompute_symphony_metrics,
        db,
        account_id,
        retries=_INITIAL_SYNC_STEP_RETRIES,
        retry_delay_seconds=_INITIAL_SYNC_STEP_RETRY_DELAY_SECONDS,
    )
    _safe_step(
        "symphony_catalog",
        _refresh_symphony_catalog_safe,
        db,
        retries=_INITIAL_SYNC_STEP_RETRIES,
        retry_delay_seconds=_INITIAL_SYNC_STEP_RETRY_DELAY_SECONDS,
    )
    if not (portfolio_ok and metrics_ok):
        raise RuntimeError(
            f"Core backfill incomplete for {account_id}: "
            f"cf_ok={cf_ok}, portfolio_ok={portfolio_ok}, metrics_ok={metrics_ok}"
    )

    if not cf_ok:
        logger.warning(
            "Cash-flow backfill incomplete for %s due to report failure/rate limiting; "
            "continuing with portfolio history + metrics and fallback net_deposits where needed.",
            account_id,
        )


def finish_initial_backfill_activity(db: Session, client: ComposerClient, account_id: str):
    """Continuation for first sync focused on trade activity tables.

    Cash-flow-driven portfolio history and metrics are already finalized in
    full_backfill_core().

    Some Composer account types (for example certain IRA accounts) may not
    support the trade/non-trade report endpoints and can return 400 errors.
    In that case, we allow the account to finish initial sync as long as
    portfolio history already exists from the core backfill.

    We also prevent falsely marking a completely empty account as synced.
    """
    logger.info("Starting first-sync trade-activity backfill for account %s...", account_id)

    historical_start = _historical_sync_start()

    tx_ok = _safe_step(
        "transactions",
        _sync_transactions,
        db,
        client,
        account_id,
        since=historical_start,
        retries=_INITIAL_SYNC_STEP_RETRIES,
        retry_delay_seconds=_INITIAL_SYNC_STEP_RETRY_DELAY_SECONDS,
    )

    holdings_ok = _safe_step(
        "holdings_history",
        _sync_holdings_history,
        db,
        client,
        account_id,
        retries=_INITIAL_SYNC_STEP_RETRIES,
        retry_delay_seconds=_INITIAL_SYNC_STEP_RETRY_DELAY_SECONDS,
    )

    # Check whether core backfill already produced portfolio history
    has_portfolio_history = (
        db.query(DailyPortfolio.date)
        .filter(DailyPortfolio.account_id == account_id)
        .first()
        is not None
    )

    # Count synced transactions
    tx_count = (
        db.query(Transaction.id)
        .filter(Transaction.account_id == account_id)
        .count()
    )

    # Never mark a completely empty account as synced
    if not has_portfolio_history and tx_count == 0:
        raise RuntimeError(
            f"Refusing to mark initial backfill done for {account_id}: "
            "no portfolio history and no transactions"
        )

    # If transaction reports are unavailable but portfolio history exists,
    # allow the account to complete sync using portfolio-based data only.
    if has_portfolio_history and tx_count == 0:
        logger.warning(
            "Marking initial sync complete for %s using portfolio-history-based data only. "
            "Transactions are unavailable from Composer reports for this account.",
            account_id,
        )

    # If transaction sync itself failed, only allow completion when portfolio
    # history exists from the core backfill.
    if not tx_ok:
        if has_portfolio_history:
            logger.warning(
                "Transaction backfill unavailable for %s; "
                "marking initial sync complete using portfolio-history-based data only. "
                "Trade table and reconstructed holdings history may be incomplete/unavailable.",
                account_id,
            )
        else:
            raise RuntimeError(f"Transaction backfill failed for {account_id}")

    set_sync_state(db, account_id, "initial_backfill_done", "true")
    set_sync_state(db, account_id, "last_sync_date", datetime.now().strftime("%Y-%m-%d"))

    logger.info(
        "First-sync trade-activity backfill complete for %s "
        "(tx_ok=%s, holdings_ok=%s, has_portfolio_history=%s, tx_count=%s)",
        account_id,
        tx_ok,
        holdings_ok,
        has_portfolio_history,
        tx_count,
    )

# ------------------------------------------------------------------
# Internal sync helpers
# ------------------------------------------------------------------

def _sync_transactions(db: Session, client: ComposerClient, account_id: str, since: str):
    """Fetch trade activity in chunks and upsert into transactions table."""
    total_new = 0
    total_seen = 0

    for chunk_since, chunk_until in _chunked_date_ranges(since, chunk_days=180):
        trades = client.get_trade_activity(account_id, since=chunk_since, until=chunk_until)
        logger.info(
            "Fetched %d trade rows for %s in chunk %s -> %s",
            len(trades),
            account_id,
            chunk_since,
            chunk_until,
        )

        chunk_new = 0
        for t in trades:
            total_seen += 1
            order_id = t.get("order_id", "")
            if not order_id:
                continue

            exists = db.query(Transaction).filter_by(
                account_id=account_id,
                order_id=order_id,
            ).first()
            if exists:
                continue

            raw_date = t.get("date", "")
            try:
                if len(raw_date) == 10:
                    tx_date = date.fromisoformat(raw_date)
                else:
                    tx_date = datetime.strptime(
                        raw_date.split(".")[0].replace("T", " "),
                        "%Y-%m-%d %H:%M:%S"
                    ).date()
            except Exception:
                continue

            db.add(Transaction(
                account_id=account_id,
                date=tx_date,
                symbol=t["symbol"],
                action=t["action"],
                quantity=t["quantity"],
                price=t["price"],
                total_amount=t["total_amount"],
                order_id=order_id,
            ))
            chunk_new += 1
            total_new += 1

        db.commit()
        logger.info(
            "Transactions synced for %s chunk %s -> %s: %d new",
            account_id,
            chunk_since,
            chunk_until,
            chunk_new,
        )

        time.sleep(1)

    logger.info(
        "Transactions synced for %s: %d new rows total (%d rows fetched across all chunks)",
        account_id,
        total_new,
        total_seen,
    )


def _sync_cash_flows(db: Session, client: ComposerClient, account_id: str, since: str):
    """Fetch non-trade activity in chunks and upsert into cash_flows table."""
    existing = set()
    for cf in db.query(CashFlow).filter_by(account_id=account_id).all():
        existing.add((str(cf.date), cf.type, round(cf.amount, 4), (cf.description or "").strip()))

    total_new = 0
    total_seen = 0

    for chunk_since, chunk_until in _chunked_date_ranges(since, chunk_days=180):
        rows = client.get_non_trade_activity(account_id, since=chunk_since, until=chunk_until)
        logger.info(
            "Fetched %d non-trade rows for %s in chunk %s -> %s",
            len(rows),
            account_id,
            chunk_since,
            chunk_until,
        )

        chunk_new = 0
        for r in rows:
            total_seen += 1

            mapped_type = _map_cash_flow_type(r["type"], r.get("subtype", ""))
            if mapped_type is None:
                continue

            raw_date = r.get("date", "")
            try:
                cf_date = date.fromisoformat(raw_date)
            except Exception:
                continue

            amount = round(float(r["amount"]), 4)
            description = (r.get("description", "") or "").strip()
            key = (raw_date, mapped_type, amount, description)

            if key in existing:
                continue

            db.add(CashFlow(
                account_id=account_id,
                date=cf_date,
                type=mapped_type,
                amount=float(r["amount"]),
                description=description,
                is_manual=0,
            ))
            existing.add(key)
            chunk_new += 1
            total_new += 1

        db.commit()
        logger.info(
            "Cash flows synced for %s chunk %s -> %s: %d new",
            account_id,
            chunk_since,
            chunk_until,
            chunk_new,
        )

        time.sleep(1)

    logger.info(
        "Cash flows synced for %s: %d new rows total (%d rows fetched across all chunks)",
        account_id,
        total_new,
        total_seen,
    )


def _roll_forward_cash_flow_totals(
    db: Session,
    account_id: str,
    *,
    preserve_baseline: bool = True,
) -> int:
    """Recompute cumulative cash-flow totals into existing DailyPortfolio rows.

    When `preserve_baseline` is True, the first portfolio row's existing totals
    are treated as a baseline offset. This keeps fallback net-deposit values
    stable for accounts where Composer non-trade reports are unavailable.
    """
    daily_rows = (
        db.query(DailyPortfolio)
        .filter_by(account_id=account_id)
        .order_by(DailyPortfolio.date)
        .all()
    )
    if not daily_rows:
        return 0

    all_cf = (
        db.query(CashFlow)
        .filter_by(account_id=account_id)
        .order_by(CashFlow.date)
        .all()
    )

    cum_deposits = 0.0
    cum_fees = 0.0
    cum_dividends = 0.0
    cum_by_date = {}

    cf_by_date: dict[str, list[CashFlow]] = {}
    for cf in all_cf:
        ds = str(cf.date)
        cf_by_date.setdefault(ds, []).append(cf)

    for ds in sorted(cf_by_date.keys()):
        for cf in cf_by_date[ds]:
            if cf.type == "deposit":
                cum_deposits += cf.amount
            elif cf.type == "withdrawal":
                cum_deposits += cf.amount
            elif cf.type.startswith("fee"):
                cum_fees += cf.amount
                if cf.type == "fee_cat":
                    cum_deposits += cf.amount
            elif cf.type == "dividend":
                cum_dividends += cf.amount
        cum_by_date[ds] = {
            "net_deposits": round(cum_deposits, 2),
            "total_fees": round(cum_fees, 2),
            "total_dividends": round(cum_dividends, 2),
        }

    cash_flow_dates = sorted(cum_by_date.keys())

    baseline_net_deposits = 0.0
    baseline_total_fees = 0.0
    baseline_total_dividends = 0.0
    if preserve_baseline:
        first_ds = daily_rows[0].date.isoformat()
        baseline_cum = {"net_deposits": 0.0, "total_fees": 0.0, "total_dividends": 0.0}
        baseline_idx = 0
        while baseline_idx < len(cash_flow_dates) and cash_flow_dates[baseline_idx] <= first_ds:
            baseline_cum = cum_by_date[cash_flow_dates[baseline_idx]]
            baseline_idx += 1

        baseline_net_deposits = round(
            float(daily_rows[0].net_deposits or 0.0) - baseline_cum["net_deposits"],
            2,
        )
        baseline_total_fees = round(
            float(daily_rows[0].total_fees or 0.0) - baseline_cum["total_fees"],
            2,
        )
        baseline_total_dividends = round(
            float(daily_rows[0].total_dividends or 0.0) - baseline_cum["total_dividends"],
            2,
        )

    updated_count = 0
    last_cum = {"net_deposits": 0.0, "total_fees": 0.0, "total_dividends": 0.0}
    cash_flow_idx = 0
    for row in daily_rows:
        ds = row.date.isoformat()
        while cash_flow_idx < len(cash_flow_dates) and cash_flow_dates[cash_flow_idx] <= ds:
            last_cum = cum_by_date[cash_flow_dates[cash_flow_idx]]
            cash_flow_idx += 1

        next_net_deposits = round(baseline_net_deposits + last_cum["net_deposits"], 2)
        next_total_fees = round(baseline_total_fees + last_cum["total_fees"], 2)
        next_total_dividends = round(
            baseline_total_dividends + last_cum["total_dividends"],
            2,
        )

        if row.net_deposits != next_net_deposits:
            row.net_deposits = next_net_deposits
            updated_count += 1
        if row.total_fees != next_total_fees:
            row.total_fees = next_total_fees
            updated_count += 1
        if row.total_dividends != next_total_dividends:
            row.total_dividends = next_total_dividends
            updated_count += 1

    db.commit()
    return updated_count


def _sync_portfolio_history(db: Session, client: ComposerClient, account_id: str):
    """Fetch portfolio history and upsert into daily_portfolio table."""
    history = client.get_portfolio_history(account_id)

    try:
        cash_balance = client.get_cash_balance(account_id)
    except Exception:
        cash_balance = 0.0

    new_count = 0
    today = datetime.now().strftime("%Y-%m-%d")
    history_sorted = sorted(history, key=lambda item: str(item.get("date", "")))
    for entry in history_sorted:
        ds_raw = str(entry.get("date", ""))
        try:
            d = date.fromisoformat(ds_raw)
        except Exception:
            continue
        ds = d.isoformat()

        existing = db.query(DailyPortfolio).filter_by(account_id=account_id, date=d).first()
        if existing:
            existing.portfolio_value = entry["portfolio_value"]
            if ds == today:
                existing.cash_balance = cash_balance
        else:
            db.add(DailyPortfolio(
                account_id=account_id,
                date=d,
                portfolio_value=entry["portfolio_value"],
                cash_balance=cash_balance if ds == today else 0.0,
                net_deposits=0.0,
                total_fees=0.0,
                total_dividends=0.0,
            ))
            new_count += 1

    db.commit()

    _roll_forward_cash_flow_totals(db, account_id, preserve_baseline=False)

    has_cash_flows = db.query(CashFlow.id).filter(CashFlow.account_id == account_id).first() is not None
    if not has_cash_flows:
        try:
            total_stats = client.get_total_stats(account_id)
            fallback_deposits = float(total_stats.get("net_deposits", 0))
            for row in db.query(DailyPortfolio).filter_by(account_id=account_id).all():
                row.net_deposits = round(fallback_deposits, 2)
            db.commit()
            logger.info(
                "No cash flow data for %s - using total-stats net_deposits=%.2f as fallback",
                account_id,
                fallback_deposits,
            )
        except Exception:
            pass

    logger.info("Daily portfolio synced for %s: %d new rows", account_id, new_count)


def _sync_holdings_history(db: Session, client: ComposerClient, account_id: str):
    """Reconstruct holdings from transactions and store snapshots."""
    txs = db.query(Transaction).filter_by(account_id=account_id).order_by(Transaction.date).all()
    tx_dicts = [
        {"date": str(t.date), "symbol": t.symbol, "action": t.action, "quantity": t.quantity}
        for t in txs
    ]

    if not tx_dicts:
        return

    snapshots = reconstruct_holdings(tx_dicts)

    new_count = 0
    for snap in snapshots:
        d = date.fromisoformat(snap["date"])
        db.query(HoldingsHistory).filter_by(account_id=account_id, date=d).delete()
        for sym, qty in snap["holdings"].items():
            db.add(HoldingsHistory(account_id=account_id, date=d, symbol=sym, quantity=qty))
            new_count += 1

    db.commit()
    logger.info("Holdings history synced for %s: %d rows across %d dates", account_id, new_count, len(snapshots))


def _sync_benchmark(db: Session, account_id: str):
    """Fetch benchmark daily closes and store.

    Incremental: only fetches from the last stored benchmark date onward.
    Provider order: Stooq (free historical) -> Finnhub candles fallback.
    """
    settings = get_settings()
    ticker = settings.benchmark_ticker

    first = db.query(func.min(DailyPortfolio.date)).filter(
        DailyPortfolio.account_id == account_id
    ).scalar()
    if not first:
        return

    last_stored = db.query(func.max(BenchmarkData.date)).filter(
        BenchmarkData.symbol == ticker
    ).scalar()
    if last_stored:
        fetch_start = str(last_stored - timedelta(days=1))
    else:
        fetch_start = str(first)

    start_date = date.fromisoformat(fetch_start)
    end_date = date.today()
    rows = get_daily_closes_stooq(ticker, start_date, end_date)
    if not rows:
        try:
            rows = get_daily_closes(ticker, start_date, end_date)
        except FinnhubAccessError as e:
            logger.warning("Failed to fetch benchmark data (Finnhub access): %s", e)
            return
        except FinnhubError as e:
            logger.warning("Failed to fetch benchmark data (Finnhub error): %s", e)
            return
    if not rows:
        return

    new_count = 0
    for d, close_val in rows:
        existing = db.query(BenchmarkData).filter_by(date=d).first()
        if existing:
            existing.close = close_val
            existing.symbol = ticker
        else:
            db.add(BenchmarkData(date=d, symbol=ticker, close=close_val))
            new_count += 1

    db.commit()
    logger.info("Benchmark data synced: %d new rows", new_count)


def _recompute_metrics(db: Session, account_id: str):
    """Recompute all daily metrics from stored data for a sub-account."""
    portfolio_rows = db.query(DailyPortfolio).filter_by(
        account_id=account_id
    ).order_by(DailyPortfolio.date).all()
    if not portfolio_rows:
        return

    daily_dicts = [
        {"date": r.date, "portfolio_value": r.portfolio_value, "net_deposits": r.net_deposits}
        for r in portfolio_rows
    ]

    ext_flows = db.query(CashFlow).filter(
        CashFlow.account_id == account_id,
        CashFlow.type.in_(["deposit", "withdrawal"]),
    ).order_by(CashFlow.date).all()
    cf_dicts = [{"date": cf.date, "amount": cf.amount} for cf in ext_flows]

    bench_rows = db.query(BenchmarkData).order_by(BenchmarkData.date).all()
    bench_dicts = [{"date": r.date, "close": r.close} for r in bench_rows] if bench_rows else None

    settings = get_settings()
    metrics = compute_all_metrics(daily_dicts, cf_dicts, bench_dicts, settings.risk_free_rate)

    _dm_cols = {c.key for c in DailyMetrics.__table__.columns} - {"account_id"}
    for m in metrics:
        d = m["date"]
        filtered = {k: v for k, v in m.items() if k in _dm_cols}
        existing = db.query(DailyMetrics).filter_by(account_id=account_id, date=d).first()
        if existing:
            for k, v in filtered.items():
                if k != "date":
                    setattr(existing, k, v)
        else:
            db.add(DailyMetrics(account_id=account_id, **filtered))

    db.commit()
    logger.info("Metrics recomputed for %s: %d rows", account_id, len(metrics))


def _sync_symphony_allocations(db: Session, client: ComposerClient, account_id: str):
    """Snapshot current symphony holdings mapped to the next trading day."""
    target = get_allocation_target_date()
    if target is None:
        logger.info("Skipping symphony allocation snapshot during market hours for %s", account_id)
        return

    if not is_after_close():
        logger.info("Skipping symphony allocation snapshot — not in post-close window for %s", account_id)
        return

    existing = db.query(SymphonyAllocationHistory).filter_by(
        account_id=account_id, date=target
    ).first()
    if existing:
        logger.info("Symphony allocations already captured for %s on %s", account_id, target)
        return

    try:
        symphonies = client.get_symphony_stats(account_id)
    except Exception as e:
        logger.warning("Failed to fetch symphony stats for %s: %s", account_id, e)
        return

    new_count = 0
    for s in symphonies:
        sym_id = s.get("id", "")
        if not sym_id:
            continue
        for h in s.get("holdings", []):
            ticker = h.get("ticker", "")
            if not ticker:
                continue
            db.add(SymphonyAllocationHistory(
                account_id=account_id,
                symphony_id=sym_id,
                date=target,
                ticker=ticker,
                allocation_pct=round(h.get("allocation", 0) * 100, 2),
                value=round(h.get("value", 0), 2),
            ))
            new_count += 1

    db.commit()
    logger.info(
        "Symphony allocations captured for %s (target date %s): %d holdings across %d symphonies",
        account_id, target, new_count, len(symphonies)
    )


# ------------------------------------------------------------------
# Symphony daily data (backfill + incremental)
# ------------------------------------------------------------------

def _infer_net_deposits_from_history(history: list) -> list[float]:
    """Infer cumulative net deposits per day from symphony history."""
    if not history:
        return []

    initial_val = history[0]["value"]
    cum_net_dep = initial_val
    net_deposits = [cum_net_dep]

    for i in range(1, len(history)):
        prev_val = history[i - 1]["value"]
        prev_adj = history[i - 1]["deposit_adjusted_value"]
        adj_i = history[i]["deposit_adjusted_value"]
        val_i = history[i]["value"]

        mkt_ret = (adj_i / prev_adj) if prev_adj > 0 else 1.0
        expected_val = prev_val * mkt_ret
        cf = val_i - expected_val
        if abs(cf) > 0.50:
            cum_net_dep += cf
        net_deposits.append(cum_net_dep)

    return net_deposits


def _sync_symphony_daily_backfill(db: Session, client: ComposerClient, account_id: str):
    """Fetch full daily history for each active symphony and store all rows."""
    try:
        symphonies = client.get_symphony_stats(account_id)
    except Exception as e:
        logger.warning("Failed to fetch symphony stats for backfill %s: %s", account_id, e)
        return

    total_new = 0
    for s in symphonies:
        sym_id = s.get("id", "")
        if not sym_id:
            continue

        try:
            history = client.get_symphony_history(account_id, sym_id)
        except Exception as e:
            logger.warning("Failed to fetch history for symphony %s: %s", sym_id, e)
            continue

        if not history:
            continue

        net_deps = _infer_net_deposits_from_history(history)

        for i, pt in enumerate(history):
            try:
                d = date.fromisoformat(pt["date"])
            except Exception:
                continue

            existing = db.query(SymphonyDailyPortfolio).filter_by(
                account_id=account_id, symphony_id=sym_id, date=d
            ).first()
            if existing:
                existing.portfolio_value = pt["value"]
                existing.net_deposits = round(net_deps[i], 2)
            else:
                db.add(SymphonyDailyPortfolio(
                    account_id=account_id,
                    symphony_id=sym_id,
                    date=d,
                    portfolio_value=pt["value"],
                    net_deposits=round(net_deps[i], 2),
                ))
                total_new += 1

        time.sleep(0.5)

    db.commit()
    logger.info(
        "Symphony daily backfill for %s: %d new rows across %d symphonies",
        account_id, total_new, len(symphonies)
    )


def _sync_symphony_daily_incremental(db: Session, client: ComposerClient, account_id: str):
    """Store today's symphony values using symphony-stats-meta (1 API call)."""
    today = date.today()
    if today.weekday() >= 5:
        logger.info("Skipping symphony daily on weekend for %s", account_id)
        return

    try:
        symphonies = client.get_symphony_stats(account_id)
    except Exception as e:
        logger.warning("Failed to fetch symphony stats for incremental %s: %s", account_id, e)
        return

    new_count = 0
    for s in symphonies:
        sym_id = s.get("id", "")
        if not sym_id:
            continue

        value = s.get("value", 0)
        net_dep = s.get("net_deposits", 0)

        existing = db.query(SymphonyDailyPortfolio).filter_by(
            account_id=account_id, symphony_id=sym_id, date=today
        ).first()
        if existing:
            existing.portfolio_value = round(value, 2)
            existing.net_deposits = round(net_dep, 2)
        else:
            db.add(SymphonyDailyPortfolio(
                account_id=account_id,
                symphony_id=sym_id,
                date=today,
                portfolio_value=round(value, 2),
                net_deposits=round(net_dep, 2),
            ))
            new_count += 1

    db.commit()
    logger.info(
        "Symphony daily incremental for %s: %d new rows for %d symphonies",
        account_id, new_count, len(symphonies)
    )


def _recompute_symphony_metrics(db: Session, account_id: str):
    """Compute daily metrics for each symphony from stored SymphonyDailyPortfolio data."""
    sym_ids = [
        row[0] for row in
        db.query(SymphonyDailyPortfolio.symphony_id).filter_by(
            account_id=account_id
        ).distinct().all()
    ]

    settings = get_settings()

    for sym_id in sym_ids:
        portfolio_rows = db.query(SymphonyDailyPortfolio).filter_by(
            account_id=account_id, symphony_id=sym_id,
        ).order_by(SymphonyDailyPortfolio.date).all()

        if not portfolio_rows:
            continue

        daily_dicts = [
            {"date": r.date, "portfolio_value": r.portfolio_value, "net_deposits": r.net_deposits}
            for r in portfolio_rows
        ]

        cf_dicts = []
        for j in range(1, len(portfolio_rows)):
            delta = portfolio_rows[j].net_deposits - portfolio_rows[j - 1].net_deposits
            if abs(delta) > 0.50:
                cf_dicts.append({"date": portfolio_rows[j].date, "amount": delta})

        last_metric_date = db.query(func.max(SymphonyDailyMetrics.date)).filter_by(
            account_id=account_id, symphony_id=sym_id,
        ).scalar()

        latest_portfolio_date = portfolio_rows[-1].date
        second_latest_date = portfolio_rows[-2].date if len(portfolio_rows) >= 2 else None

        use_incremental = (
            last_metric_date is not None
            and second_latest_date is not None
            and last_metric_date >= second_latest_date
            and last_metric_date < latest_portfolio_date
        )

        if use_incremental:
            m = compute_latest_metrics(daily_dicts, cf_dicts, settings.risk_free_rate)
            if m:
                metrics_to_persist = [m]
                logger.debug("Incremental metrics for symphony %s: 1 new day", sym_id)
            else:
                metrics_to_persist = []
        else:
            metrics_to_persist = compute_all_metrics(daily_dicts, cf_dicts, None, settings.risk_free_rate)
            logger.debug("Full backfill metrics for symphony %s: %d days", sym_id, len(metrics_to_persist))

        _sdm_cols = {c.key for c in SymphonyDailyMetrics.__table__.columns} - {"account_id", "symphony_id"}
        for m in metrics_to_persist:
            d = m["date"]
            filtered = {k: v for k, v in m.items() if k in _sdm_cols}
            existing = db.query(SymphonyDailyMetrics).filter_by(
                account_id=account_id, symphony_id=sym_id, date=d
            ).first()
            if existing:
                for k, v in filtered.items():
                    if k != "date":
                        setattr(existing, k, v)
            else:
                db.add(SymphonyDailyMetrics(
                    account_id=account_id, symphony_id=sym_id, **filtered
                ))

    db.commit()
    logger.info("Symphony metrics computed for %s: %d symphonies", account_id, len(sym_ids))