from __future__ import annotations

"""
hedge_snapshot_writer.py

Writes one HedgeSnapshot row to the DB for today (or a given date).

Called:
  1. Daily by the scheduler at ~17:30 after market close
  2. On-demand via POST /api/hedge/history/snapshot (for seeding/backfill)

Design:
  - Idempotent: re-running for the same date overwrites the existing row.
  - Never raises: errors are logged and returned as a result dict.
  - Single DB write per call — no loops, no historical recomputation.
"""

import logging
from datetime import date, datetime, timezone
from typing import Any, Dict, List

from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.models import HedgeSnapshot
from app.services.account_scope import resolve_account_ids
from app.services.hedge_intelligence_read import get_hedge_intelligence_data

logger = logging.getLogger(__name__)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def write_hedge_snapshot(
    *,
    account_id: str = "all",
    target_date: str | None = None,
    db: Session | None = None,
) -> Dict[str, Any]:
    """
    Compute hedge intelligence for the given date and persist to hedge_snapshots.

    Args:
        account_id:  "all" or a specific account ID (passed to hedge intelligence)
        target_date: ISO date string e.g. "2026-03-15". Defaults to today.
        db:          Optional existing DB session. If None, a new one is created
                     and closed on exit.

    Returns a dict with keys:
        date, success, written_at, current_hedge_pct, portfolio_value, error
    """
    if target_date is None:
        target_date = date.today().isoformat()

    owns_db = db is None
    if owns_db:
        db = SessionLocal()

    try:
        account_ids: List[str] = resolve_account_ids(db, account_id)

        hedge = get_hedge_intelligence_data(
            db=db,
            account_ids=account_ids,
            target_date=None,   # always use today's live data for today's snapshot
        )

        snap_date = date.fromisoformat(target_date)
        now = _utc_now()

        # Upsert: delete existing row for this date then insert fresh.
        db.query(HedgeSnapshot).filter(HedgeSnapshot.date == snap_date).delete()

        snap = HedgeSnapshot(
            date=snap_date,
            portfolio_value=float(hedge.portfolio_value or 0.0),
            portfolio_beta=float(hedge.portfolio_beta or 0.0),
            current_hedge_exposure_dollars=float(hedge.current_hedge_exposure_dollars or 0.0),
            current_hedge_pct=float(hedge.current_hedge_pct or 0.0),
            recommended_hedge_pct=float(hedge.recommended_hedge_pct or 0.0),
            structural_hedge_exposure_dollars=float(hedge.structural_hedge_exposure_dollars or 0.0),
            option_hedge_exposure_dollars=float(hedge.option_hedge_exposure_dollars or 0.0),
            current_hedge_premium_market_value=float(hedge.current_hedge_premium_market_value or 0.0),
            current_hedge_premium_cost_basis=float(hedge.current_hedge_premium_cost_basis or 0.0),
            hedge_unrealized_pnl=float(hedge.hedge_unrealized_pnl or 0.0),
            hedged_beta_estimate=float(hedge.hedged_beta_estimate or 0.0),
            unhedged_beta_estimate=float(hedge.unhedged_beta_estimate or 0.0),
            market_regime=hedge.market_regime,
            market_risk_score=float(hedge.market_risk_score or 0.0),
            written_at=now,
        )

        db.add(snap)
        db.commit()

        logger.info(
            "Hedge snapshot written: date=%s hedge_pct=%.1f%% portfolio=$%.0f",
            target_date,
            float(hedge.current_hedge_pct or 0.0) * 100,
            float(hedge.portfolio_value or 0.0),
        )

        return {
            "date": target_date,
            "success": True,
            "written_at": now.isoformat(),
            "current_hedge_pct": float(hedge.current_hedge_pct or 0.0),
            "recommended_hedge_pct": float(hedge.recommended_hedge_pct or 0.0),
            "portfolio_value": float(hedge.portfolio_value or 0.0),
            "market_regime": hedge.market_regime,
            "error": None,
        }

    except Exception as exc:
        if owns_db:
            db.rollback()
        logger.error("Hedge snapshot write failed for %s: %s", target_date, exc, exc_info=True)
        return {
            "date": target_date,
            "success": False,
            "written_at": None,
            "current_hedge_pct": None,
            "recommended_hedge_pct": None,
            "portfolio_value": None,
            "market_regime": None,
            "error": str(exc),
        }
    finally:
        if owns_db:
            db.close()