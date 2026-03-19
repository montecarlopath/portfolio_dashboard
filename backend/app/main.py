"""FastAPI application entry point."""

import asyncio
import logging
import os
import time
from contextlib import asynccontextmanager

import httpx
# Add after the imports (around line 10):
_INTERNAL_API_BASE = os.environ.get("INTERNAL_API_URL", f"{_INTERNAL_API_BASE}")

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.database import init_db, SessionLocal
from app.routers import portfolio, health, symphonies
from app.config import load_accounts, is_test_mode, validate_composer_config
from app.composer_client import ComposerClient
from app.models import Account
from app.security import get_allowed_origins
from app.market_hours import now_et
from app.services.hedge_snapshot_writer import write_hedge_snapshot
from app.services.eod_hedge_engine import (
    check_spread_width_acceptable,
    compute_limit_price,
    is_final_attempt,
    get_eod_attempt_number,
    minutes_to_close,
    add_eod_alert,
    clear_eod_alerts,
    FINAL_ATTEMPT_SLIPPAGE,
)

from app.services.hedge_close_engine import execute_close_tickets

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

if os.getenv("PD_VERBOSE_ACCESS_LOGS", "").lower() not in {"1", "true", "yes", "on"}:
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)

scheduler = AsyncIOScheduler()


def _discover_accounts():
    """For each credential set in config.json, discover sub-accounts and persist to DB."""
    accounts_creds = load_accounts()
    db = SessionLocal()
    try:
        for creds in accounts_creds:
            client = ComposerClient.from_credentials(creds)
            try:
                subs = client.list_sub_accounts()
            except Exception as e:
                logger.error("Failed to discover sub-accounts for '%s': %s", creds.name, e)
                continue

            for sub in subs:
                display = f"{creds.name}: {sub['display_name']}"
                existing = db.query(Account).filter_by(id=sub["account_id"]).first()
                if existing:
                    existing.credential_name = creds.name
                    existing.account_type = sub["account_type"]
                    existing.display_name = display
                    existing.status = sub["status"]
                else:
                    db.add(Account(
                        id=sub["account_id"],
                        credential_name=creds.name,
                        account_type=sub["account_type"],
                        display_name=display,
                        status=sub["status"],
                    ))
                logger.info("Discovered sub-account: %s (%s)", display, sub["account_id"])

            if len(accounts_creds) > 1:
                time.sleep(1)

        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


# ── Scheduler jobs ────────────────────────────────────────────────────────────

async def _run_monitor():
    """
    3:00 PM ET — poll open Alpaca orders, update lifecycle store.
    Detects fills and triggers post-fill reconciliation automatically.
    No auto-reprice here — EOD engine handles submissions.
    """
    now = now_et()
    if now.weekday() >= 5:
        return
    if not (9 <= now.hour < 16):
        logger.info("Monitor skipped: outside market hours (%s ET)", now.strftime("%H:%M"))
        return

    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{_INTERNAL_API_BASE}/hedge/orders/monitor",
                params={"reprice": "false"},
                timeout=60,
            )
            result = resp.json()

        logger.info(
            "Monitor: checked=%s filled=%s stale=%s",
            result.get("orders_checked", 0),
            len(result.get("newly_filled", [])),
            len(result.get("stale_orders", [])),
        )

        newly_filled = result.get("newly_filled", [])
        if newly_filled:
            filled_ids = [o["client_order_id"] for o in newly_filled]
            logger.info("Fills detected → post-fill reconciliation: %s", filled_ids)
            async with httpx.AsyncClient() as client:
                recon_resp = await client.post(
                    f"{_INTERNAL_API_BASE}/hedge/reconcile/post-fill",
                    timeout=60,
                )
            recon = recon_resp.json()
            logger.info("Post-fill reconciliation result: %s", recon)
            # Clear any EOD alerts for today since we have fills
            clear_eod_alerts(date=now.date().isoformat())

    except Exception as e:
        logger.error("Monitor failed: %s", e, exc_info=True)


async def _cancel_all_open_hedge_orders():
    """Cancel all open hedge orders before resubmitting at a new price."""
    try:
        async with httpx.AsyncClient() as client:
            open_resp = await client.get(
                f"{_INTERNAL_API_BASE}/hedge/orders/open",
                timeout=30,
            )
            open_orders = open_resp.json().get("orders", [])

        for order in open_orders:
            broker_order_id = order.get("broker_order_id")
            if broker_order_id:
                async with httpx.AsyncClient() as client:
                    await client.post(
                        f"{_INTERNAL_API_BASE}/hedge/orders/cancel",
                        params={"broker_order_id": broker_order_id},
                        timeout=30,
                    )
                logger.info("EOD: cancelled %s before resubmit", broker_order_id)
                await asyncio.sleep(1.0)

    except Exception as e:
        logger.warning("EOD: cancel open orders failed: %s", e)


async def _run_eod_submission():
    """
    EOD hedge submission — fires at 3:15, 3:25, 3:35, 3:45 PM ET.

    Attempt 1-3 (3:15, 3:25, 3:35): submit at mid price.
    Attempt 4   (3:45):             submit at ask if spread width acceptable.

    Bid-ask thresholds:
      Primary: reject if width > 15% of net debit
      Tail:    reject if width > 40% of net debit

    On final attempt if spread still too wide: add dashboard alert, skip to tomorrow.
    """
    now = now_et()
    if now.weekday() >= 5:
        return
    if now.hour < 9 or (now.hour == 9 and now.minute < 30):
        return
    # Hard cutoff — never submit after 3:55 PM ET
    if now.hour > 15 or (now.hour == 15 and now.minute >= 55):
        logger.info("EOD: past 3:55 cutoff (%s ET)", now.strftime("%H:%M"))
        return

    attempt = get_eod_attempt_number()
    if attempt == 0:
        logger.info("EOD: not in submission window at %s ET", now.strftime("%H:%M"))
        return

    final = is_final_attempt()
    mins_left = minutes_to_close()
    today = now.date().isoformat()

    logger.info(
        "EOD attempt %d/4 at %s ET (%d min to close, use_ask=%s)",
        attempt, now.strftime("%H:%M"), mins_left, final,
    )

    try:
        # ── Check hedge gap ───────────────────────────────────────────────────
        async with httpx.AsyncClient() as client:
            intel_resp = await client.post(
                f"{_INTERNAL_API_BASE}/hedge/reconcile/post-fill",
                timeout=60,
            )
            intel = intel_resp.json()

        error = intel.get("error", "")
        needs_more = intel.get("needs_more_hedge", False)
        gap_pct = intel.get("remaining_gap_pct", 0.0)
        budget = intel.get("remaining_budget_dollars", 0.0)

        # "No fills to process" is not a real error — just means no recent fills
        if error and "No fills" not in str(error):
            logger.warning("EOD: reconciliation error: %s", error)
            return

        if not needs_more:
            logger.info(
                "EOD: hedge target met (gap=%.1f%% budget=$%.0f) — no action",
                gap_pct * 100, budget,
            )
            return

        logger.info(
            "EOD: gap=%.1f%% budget=$%.0f — proceeding with attempt %d",
            gap_pct * 100, budget, attempt,
        )

        # ── Check for exit tickets FIRST ──────────────────────────────────────
        # Before adding new hedges, check if any existing positions should be
        # closed (profit-take, regime exit, decay). Exit first, then fill gap.
        try:
            async with httpx.AsyncClient() as client:
                tickets_resp = await client.get(
                    f"{_INTERNAL_API_BASE}/hedge/tickets",
                    params={"account_id": "all", "mode": "preview"},
                    timeout=60,
                )
                all_tickets = tickets_resp.json().get("tickets", [])

                close_actions = {"close_profit_take", "close_regime_exit", "close_decay"}
                from app.schemas import HedgeTradeTicket

                # Convert dicts to objects for execute_close_tickets
                class _TicketProxy:
                    def __init__(self, d):
                        for k, v in d.items():
                            setattr(self, k, v)

                close_ticket_objs = [
                    _TicketProxy(t) for t in all_tickets
                    if t.get("action") in close_actions
                ]

                if close_ticket_objs:
                    close_results = execute_close_tickets(
                        tickets=close_ticket_objs,
                        mode="submit",
                        use_bid=final,  # use bid on final attempt
                    )
                    submitted_closes = [r for r in close_results if r.get("submitted")]
                    if submitted_closes:
                        logger.info(
                            "EOD: closed %d position(s): %s",
                            len(submitted_closes),
                            [r["symbol"] for r in submitted_closes],
                        )
        except Exception as e:
            logger.warning("EOD: close ticket execution failed: %s", e)

        # ── Get spread selection with fresh bid/ask ───────────────────────────
        async with httpx.AsyncClient() as client:
            select_resp = await client.get(
                f"{_INTERNAL_API_BASE}/hedge/select",
                params={"account_id": "all"},
                timeout=60,
            )
            selection = select_resp.json()

        # ── Cancel stale orders on attempts 2+ ───────────────────────────────
        # Do this BEFORE the width checks so we start clean each attempt.
        if attempt > 1:
            await _cancel_all_open_hedge_orders()
            await asyncio.sleep(1.0)  # give Alpaca time to process cancels
 
        # ── Check bid-ask width per bucket ────────────────────────────────────
        # We check widths separately so we can alert per bucket, but submit ONCE.
        submitted_any = False
        any_bucket_ok = False
 
        for bucket_name, spread in [
            ("primary", selection.get("primary_spread", {})),
            ("tail",    selection.get("tail_spread", {})),
        ]:
            long_leg  = spread.get("long_leg")  or {}
            short_leg = spread.get("short_leg") or {}
 
            long_bid  = long_leg.get("bid")
            long_ask  = long_leg.get("ask")
            short_bid = short_leg.get("bid")
            short_ask = short_leg.get("ask")
 
            ok, width_pct, reason = check_spread_width_acceptable(
                bucket=bucket_name,
                long_bid=long_bid,
                long_ask=long_ask,
                short_bid=short_bid,
                short_ask=short_ask,
            )
 
            if not ok:
                logger.warning(
                    "EOD attempt %d: %s too wide (%.1f%%) — skipping. %s",
                    attempt, bucket_name, width_pct * 100, reason,
                )
                if final:
                    threshold = 15 if "primary" in bucket_name else 40
                    add_eod_alert(
                        alert_type="wide_spread",
                        bucket=bucket_name,
                        message=(
                            f"{bucket_name.title()} spread bid-ask too wide at all "
                            f"4 EOD attempts. Width: {width_pct:.1%} "
                            f"(threshold {threshold}%). "
                            f"Skipped today — will retry tomorrow at 3:15 PM ET."
                        ),
                        width_pct=width_pct,
                    )
                continue
 
            # Log the limit price for this bucket (informational)
            limit_price = compute_limit_price(
                bucket=bucket_name,
                long_bid=long_bid,
                long_ask=long_ask,
                short_bid=short_bid,
                short_ask=short_ask,
                use_ask=final,
            )
            bucket_key = "primary" if "primary" in bucket_name else "tail"
            buffer_pct = FINAL_ATTEMPT_SLIPPAGE.get(bucket_key, 0.0) if final else 0.0
 
            logger.info(
                "EOD %s attempt=%d limit=%.2f width=%.1f%% use_ask=%s buffer=%.1f%%",
                bucket_name, attempt, limit_price or 0,
                width_pct * 100, final, buffer_pct * 100,
            )
            any_bucket_ok = True
 
        # ── Submit ONCE for all acceptable buckets ────────────────────────────
        # KEY FIX: call /hedge/orders?mode=submit only once, not per bucket.
        # The orders endpoint generates tickets for all gaps in a single call.
        # Calling it per bucket caused double-submission of the primary spread.
        if any_bucket_ok:
            max_slippage = FINAL_ATTEMPT_SLIPPAGE.get("primary", 0.05)
            buffer_pct = FINAL_ATTEMPT_SLIPPAGE.get("primary", 0.0) if final else 0.0
 
            async with httpx.AsyncClient() as client:
                submit_resp = await client.get(
                    f"{_INTERNAL_API_BASE}/hedge/orders",
                    params={
                        "account_id": "all",
                        "mode": "submit",
                        "limit_price_buffer_pct": str(buffer_pct),
                        "max_slippage_pct": str(max_slippage),
                    },
                    timeout=120,
                )
                result = submit_resp.json()
 
            submitted = [
                o for o in result.get("orders", [])
                if (o.get("submission_result") or {}).get("submitted")
            ]
            if submitted:
                submitted_any = True
                # Log which buckets were submitted
                for o in submitted:
                    bucket = (o.get("ticket_bucket") or "unknown")
                    qty = (o.get("alpaca_payload") or {}).get("qty", "?")
                    logger.info("EOD: %s submitted qty=%s", bucket, qty)
 
        # ── Final attempt: alert if nothing submitted ─────────────────────────
        if final and not submitted_any:
            add_eod_alert(
                alert_type="no_fill",
                bucket="all",
                message=(
                    f"EOD: no orders submitted after 4 attempts. "
                    f"Hedge gap remains {gap_pct:.1%} (${budget:,.0f} budget). "
                    f"Will retry tomorrow at 3:15 PM ET."
                ),
            )

    except Exception as e:
        logger.error("EOD attempt %d failed: %s", attempt, e, exc_info=True)


async def _write_daily_hedge_snapshot():
    """4:30 PM ET — write today's hedge state to DB."""
    now = now_et()
    if now.weekday() >= 5:
        return

    try:
        result = write_hedge_snapshot(account_id="all")
        if result["success"]:
            logger.info(
                "Daily hedge snapshot: date=%s hedge=%.1f%% portfolio=$%.0f regime=%s",
                result["date"],
                (result["current_hedge_pct"] or 0) * 100,
                result["portfolio_value"] or 0,
                result["market_regime"],
            )
        else:
            logger.warning("Daily hedge snapshot failed: %s", result["error"])
    except Exception as e:
        logger.warning("Daily hedge snapshot error: %s", e)


# ── App lifespan ──────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Create DB tables, discover accounts, start scheduler."""
    init_db()
    try:
        if is_test_mode():
            logger.info("Test mode active: skipping real account discovery")
        else:
            ok, err = validate_composer_config()
            if not ok:
                logger.warning("Account discovery skipped: %s", err)
            else:
                _discover_accounts()
    except FileNotFoundError as e:
        logger.warning("Account discovery skipped: %s", e)
    except Exception as e:
        logger.error("Account discovery failed: %s", e, exc_info=True)

    # ── Scheduler ─────────────────────────────────────────────────────────────
    # 3:00 PM ET — monitor: check fills, update store
    scheduler.add_job(
        _run_monitor,
        trigger="cron",
        day_of_week="mon-fri",
        hour=15, minute=0,
        timezone="America/New_York",
        id="hedge_monitor_eod",
        replace_existing=True,
    )

    # 3:15 PM ET — EOD attempt 1 (mid price)
    scheduler.add_job(
        _run_eod_submission,
        trigger="cron",
        day_of_week="mon-fri",
        hour=15, minute=15,
        timezone="America/New_York",
        id="hedge_eod_attempt1",
        replace_existing=True,
    )

    # 3:25 PM ET — EOD attempt 2 (cancel stale + mid)
    scheduler.add_job(
        _run_eod_submission,
        trigger="cron",
        day_of_week="mon-fri",
        hour=15, minute=25,
        timezone="America/New_York",
        id="hedge_eod_attempt2",
        replace_existing=True,
    )

    # 3:35 PM ET — EOD attempt 3 (cancel stale + mid)
    scheduler.add_job(
        _run_eod_submission,
        trigger="cron",
        day_of_week="mon-fri",
        hour=15, minute=35,
        timezone="America/New_York",
        id="hedge_eod_attempt3",
        replace_existing=True,
    )

    # 3:45 PM ET — EOD attempt 4 / final (ask price if width acceptable)
    scheduler.add_job(
        _run_eod_submission,
        trigger="cron",
        day_of_week="mon-fri",
        hour=15, minute=45,
        timezone="America/New_York",
        id="hedge_eod_attempt4_final",
        replace_existing=True,
    )

    # 4:30 PM ET — daily hedge snapshot
    scheduler.add_job(
        _write_daily_hedge_snapshot,
        trigger="cron",
        day_of_week="mon-fri",
        hour=16, minute=30,
        timezone="America/New_York",
        id="hedge_daily_snapshot",
        replace_existing=True,
    )

    scheduler.start()
    logger.info(
        "EOD hedge scheduler started: "
        "monitor@15:00, submissions@15:15/25/35/45, snapshot@16:30 ET"
    )

    yield  # app is running

    scheduler.shutdown(wait=False)
    logger.info("Scheduler stopped.")


# ── App ───────────────────────────────────────────────────────────────────────

app = FastAPI(
    title="Portfolio Dashboard",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=sorted(get_allowed_origins()),
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(health.router)
app.include_router(portfolio.router)
app.include_router(symphonies.router)