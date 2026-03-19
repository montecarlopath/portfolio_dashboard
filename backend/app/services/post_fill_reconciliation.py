from __future__ import annotations

"""
post_fill_reconciliation.py

Triggered automatically when the order monitor detects a fill.

What it does in sequence:
  1. Loads a DB session and resolves account_ids
  2. Re-runs hedge intelligence  → picks up new Alpaca positions,
     recomputes current_hedge_pct, gap, and budget
  3. Re-runs reconciliation      → generates updated hold/add actions
     against the new hedge state
  4. Writes a FillReconciliationSnapshot to the store
     so the API can report what changed

The result answers:
  - What is the hedge gap NOW (after the fill)?
  - Has the target been met, partially met, or is more needed?
  - What actions remain?
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional


from app.database import SessionLocal
from app.services.account_scope import resolve_account_ids
from app.services.hedge_intelligence_read import get_hedge_intelligence_data
from app.services.hedge_reconciliation_engine import build_hedge_reconciliation_engine
from app.services.broker_submission_store import (
    list_all_orders,
    update_order_lifecycle,
    list_open_orders,
)

logger = logging.getLogger(__name__)

# ── Gap threshold — below this we consider the hedge target met ───────────────
# 0.5% of portfolio is noise; don't generate new tickets for tiny residuals.
GAP_MET_THRESHOLD_PCT: float = 0.005


# ── Result type ───────────────────────────────────────────────────────────────

@dataclass
class FillReconciliationResult:
    """
    Returned by run_post_fill_reconciliation().
    Contains the new hedge state and whether more action is needed.
    """
    triggered_by: List[str]            # client_order_ids that filled
    as_of_date: str

    # New hedge state after fill
    current_hedge_pct: float
    recommended_hedge_pct: float
    remaining_gap_pct: float
    remaining_gap_dollars: float
    remaining_budget_dollars: float

    # Alpaca sleeve (was $0 before any fills)
    alpaca_hedge_exposure_dollars: float

    # Whether we need to place more orders
    target_met: bool
    needs_more_hedge: bool
    immediate_actions: List[str] = field(default_factory=list)

    error: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "triggered_by": self.triggered_by,
            "as_of_date": self.as_of_date,
            "current_hedge_pct": round(self.current_hedge_pct, 4),
            "recommended_hedge_pct": round(self.recommended_hedge_pct, 4),
            "remaining_gap_pct": round(self.remaining_gap_pct, 4),
            "remaining_gap_dollars": round(self.remaining_gap_dollars, 2),
            "remaining_budget_dollars": round(self.remaining_budget_dollars, 2),
            "alpaca_hedge_exposure_dollars": round(self.alpaca_hedge_exposure_dollars, 2),
            "target_met": self.target_met,
            "needs_more_hedge": self.needs_more_hedge,
            "immediate_actions": self.immediate_actions,
            "error": self.error,
        }


# ── Core function ─────────────────────────────────────────────────────────────

def run_post_fill_reconciliation(
    filled_orders,          # List[OrderCheckResult] from monitor loop
    account_id: str = "all",
) -> FillReconciliationResult:
    """
    Re-run the hedge intelligence + reconciliation chain after fills are detected.

    Call this immediately after the monitor loop returns newly_filled results.
    It is safe to call with an empty list — returns early with no DB calls.
    """
    if not filled_orders:
        logger.info("Post-fill reconciliation: no fills to process.")
        return FillReconciliationResult(
            triggered_by=[],
            as_of_date=datetime.now(timezone.utc).date().isoformat(),
            current_hedge_pct=0.0,
            recommended_hedge_pct=0.0,
            remaining_gap_pct=0.0,
            remaining_gap_dollars=0.0,
            remaining_budget_dollars=0.0,
            alpaca_hedge_exposure_dollars=0.0,
            target_met=False,
            needs_more_hedge=False,
            error="No fills to process.",
        )

    filled_ids = [o.client_order_id for o in filled_orders]
    logger.info("Post-fill reconciliation triggered by: %s", filled_ids)

    db = SessionLocal()
    try:
        # ── 1. resolve accounts ───────────────────────────────────────────────
        account_ids = resolve_account_ids(db, account_id)

        # ── 2. re-run hedge intelligence ──────────────────────────────────────
        # This already calls load_alpaca_hedge_positions() internally,
        # so it will pick up the newly-filled Alpaca positions automatically.
        hedge_intel = get_hedge_intelligence_data(
            db=db,
            account_ids=account_ids,
        )

        current_hedge_pct = float(hedge_intel.current_hedge_pct or 0.0)
        recommended_hedge_pct = float(hedge_intel.recommended_hedge_pct or 0.0)
        remaining_gap_pct = float(hedge_intel.additional_hedge_pct or 0.0)
        remaining_gap_dollars = float(hedge_intel.additional_hedge_exposure_dollars or 0.0)
        remaining_budget = float(hedge_intel.remaining_hedge_budget_dollars or 0.0)

        # Pull Alpaca sleeve from the source breakdown
        alpaca_exposure = 0.0
        if hedge_intel.hedge_source_breakdown:
            alpaca = getattr(hedge_intel.hedge_source_breakdown, "alpaca", None)
            if alpaca:
                alpaca_exposure = float(
                    getattr(alpaca, "current_hedge_exposure_dollars", 0.0) or 0.0
                )
            
            

        # ── 3. re-run reconciliation ──────────────────────────────────────────
        recon = build_hedge_reconciliation_engine(
            db=db,
            account_ids=account_ids,
            as_of_date=hedge_intel.as_of_date,
            underlying="QQQ",
            market_regime=hedge_intel.market_regime,
            hedge_style="correction_focused",
            portfolio_value=float(hedge_intel.portfolio_value or 0.0),
            current_hedge_pct=current_hedge_pct,
            recommended_hedge_pct=recommended_hedge_pct,
            additional_hedge_pct=remaining_gap_pct,
            remaining_hedge_budget_pct=float(
                hedge_intel.remaining_hedge_budget_dollars or 0.0
            ) / max(float(hedge_intel.portfolio_value or 1.0), 1.0),
        )

        immediate_actions = list(getattr(recon, "immediate_actions", []) or [])

        # ── 4. evaluate gap ───────────────────────────────────────────────────
        open_orders = list_open_orders()
        has_pending_orders = len(open_orders) > 0
        target_met = remaining_gap_pct <= GAP_MET_THRESHOLD_PCT
        needs_more = (not target_met) and (remaining_budget > 500) and (not has_pending_orders)

        as_of = hedge_intel.as_of_date or datetime.now(timezone.utc).date().isoformat()

        result = FillReconciliationResult(
            triggered_by=filled_ids,
            as_of_date=as_of,
            current_hedge_pct=current_hedge_pct,
            recommended_hedge_pct=recommended_hedge_pct,
            remaining_gap_pct=remaining_gap_pct,
            remaining_gap_dollars=remaining_gap_dollars,
            remaining_budget_dollars=remaining_budget,
            alpaca_hedge_exposure_dollars=alpaca_exposure,
            target_met=target_met,
            needs_more_hedge=needs_more,
            immediate_actions=immediate_actions,
        )

        logger.info(
            "Post-fill reconciliation complete: "
            "current_hedge=%.1f%% recommended=%.1f%% gap=%.1f%% "
            "alpaca_exposure=$%.0f target_met=%s needs_more=%s",
            current_hedge_pct * 100,
            recommended_hedge_pct * 100,
            remaining_gap_pct * 100,
            alpaca_exposure,
            target_met,
            needs_more,
        )

        return result

    except Exception as exc:
        logger.error("Post-fill reconciliation failed: %s", exc, exc_info=True)
        return FillReconciliationResult(
            triggered_by=filled_ids,
            as_of_date=datetime.now(timezone.utc).date().isoformat(),
            current_hedge_pct=0.0,
            recommended_hedge_pct=0.0,
            remaining_gap_pct=0.0,
            remaining_gap_dollars=0.0,
            remaining_budget_dollars=0.0,
            alpaca_hedge_exposure_dollars=0.0,
            target_met=False,
            needs_more_hedge=False,
            error=str(exc),
        )
    finally:
        db.close()