from __future__ import annotations

"""
hedge_efficiency_optimizer.py  (Phase 6 Step 3 update)

Added profit-take and hedge exit triggers on top of the existing
DTE + moneyness + vol scoring.

New triggers (checked BEFORE the score-based logic):

  Trigger 1 — Profit-take:
    position worth > PROFIT_TAKE_MULTIPLIER × cost_basis (default 2.5×)
    → decision = "close_profit_take", close 50% recommended
    → reason includes actual P&L and multiplier

  Trigger 2 — Regime improvement exit:
    regime has improved since position was opened AND
    position has appreciated > REGIME_EXIT_MIN_GAIN_PCT (default 50%)
    → decision = "close_regime_exit", regime no longer warrants this hedge
    → only fires if regime is now better (lower risk) than when put was opened

  Trigger 3 — Decay close:
    position worth < DECAY_CLOSE_THRESHOLD × cost_basis (default 30%)
    AND DTE < 21
    → decision = "close_decay", let expire or close cheaply
    → transaction costs exceed remaining value

  Trigger 4 — Efficiency decay (existing, kept):
    DTE < 14 AND moneyness < 0.95
    → score penalty, may become "replace"

All constants come from hedge_config.py and can be changed without code edits.
"""

from dataclasses import dataclass, field
from datetime import date
from typing import Literal, Optional

from app.services.hedge_config import (
    PROFIT_RULES,
    DECAY_RULES,
)


OptimizerDecision = Literal[
    "keep",
    "roll",
    "replace",
    "close_profit_take",   # NEW — take profit, position >2.5× cost
    "close_regime_exit",   # NEW — regime improved, position has gains
    "close_decay",         # NEW — decayed below 30% of cost, DTE < 21
]

# Regime risk ordering — lower index = lower risk = better market
REGIME_RISK_ORDER = [
    "strong_bull",
    "extended_bull",
    "neutral",
    "localized_bubble",
    "early_breakdown",
    "high_crash_risk",
]


@dataclass
class HedgeEfficiencyResult:
    decision: OptimizerDecision
    score: float
    dte: Optional[int]
    moneyness: Optional[float]
    reasons: list[str]
    close_fraction: float = 1.0   # how much to close (1.0 = all, 0.5 = half)
    pnl_dollars: Optional[float] = None
    pnl_pct: Optional[float] = None


def _safe_dte(as_of_date: str, expiry: Optional[str]) -> Optional[int]:
    if not expiry:
        return None
    try:
        return (date.fromisoformat(expiry) - date.fromisoformat(as_of_date)).days
    except Exception:
        return None


def _regime_risk_rank(regime: Optional[str]) -> int:
    """Lower rank = lower risk (better market). Unknown = middle."""
    if not regime:
        return 3
    try:
        return REGIME_RISK_ORDER.index(regime)
    except ValueError:
        return 3


def evaluate_hedge_efficiency(
    *,
    as_of_date: str,
    expiry: Optional[str],
    strike: Optional[float],
    option_type: Optional[str],
    underlying_price: Optional[float],
    vix_level: Optional[float],
    # NEW — profit-take and regime-exit inputs
    current_market_value: Optional[float] = None,
    total_cost_basis: Optional[float] = None,
    current_regime: Optional[str] = None,
    entry_regime: Optional[str] = None,   # regime when position was opened (optional)
    structure_type: Optional[str] = None,
) -> HedgeEfficiencyResult:
    """
    Evaluate whether to keep, roll, replace, or close a hedge position.

    Checks profit-take, regime exit, and decay triggers FIRST.
    Falls through to score-based DTE + moneyness + vol logic if none fire.
    """
    reasons: list[str] = []
    hedge_type = structure_type or "primary_spread"
    profit_rules = PROFIT_RULES.get(hedge_type, PROFIT_RULES["primary_spread"])
    decay_threshold = DECAY_RULES.get(hedge_type, DECAY_RULES["primary_spread"])

    if option_type != "P":
        return HedgeEfficiencyResult(
            decision="replace",
            score=0.0,
            dte=None,
            moneyness=None,
            reasons=["Only put hedges are supported."],
        )

    dte = _safe_dte(as_of_date, expiry)

    # ── Compute P&L if we have cost data ─────────────────────────────────────
    pnl_dollars: Optional[float] = None
    pnl_pct: Optional[float] = None
    value_multiple: Optional[float] = None

    if current_market_value is not None and total_cost_basis is not None and total_cost_basis > 0:
        pnl_dollars = current_market_value - total_cost_basis
        pnl_pct = pnl_dollars / total_cost_basis
        value_multiple = current_market_value / total_cost_basis

    # ── Trigger 1: Profit-take ────────────────────────────────────────────────
    # Position is worth more than PROFIT_TAKE_MULTIPLIER × what we paid.
    # Close 50% — capture most of the gain, keep remaining protection.
    if value_multiple is not None:
        pnl_str = f"${pnl_dollars:,.0f}" if pnl_dollars is not None else "n/a"

        if value_multiple >= profit_rules.get("full_exit", float("inf")):
            return HedgeEfficiencyResult(
                decision="close_profit_take",
                score=99.0,
                dte=dte,
                moneyness=strike / underlying_price if (strike and underlying_price and underlying_price > 0) else None,
                reasons=[
                    f"Profit-take trigger: position is worth {value_multiple:.1f}× cost basis "
                    f"(full-exit threshold {profit_rules['full_exit']:.1f}×). P&L = {pnl_str}.",
                    "Recommend closing 100% of position to harvest extreme convex gains.",
                ],
                close_fraction=1.0,
                pnl_dollars=pnl_dollars,
                pnl_pct=pnl_pct,
            )

        if value_multiple >= profit_rules.get("take_profit_2", float("inf")):
            return HedgeEfficiencyResult(
                decision="close_profit_take",
                score=95.0,
                dte=dte,
                moneyness=strike / underlying_price if (strike and underlying_price and underlying_price > 0) else None,
                reasons=[
                    f"Profit-take trigger: position is worth {value_multiple:.1f}× cost basis "
                    f"(second threshold {profit_rules['take_profit_2']:.1f}×). P&L = {pnl_str}.",
                    "Recommend closing 50% of remaining position.",
                ],
                close_fraction=0.5,
                pnl_dollars=pnl_dollars,
                pnl_pct=pnl_pct,
            )

        if value_multiple >= profit_rules.get("take_profit_1", float("inf")):
            return HedgeEfficiencyResult(
                decision="close_profit_take",
                score=90.0,
                dte=dte,
                moneyness=strike / underlying_price if (strike and underlying_price and underlying_price > 0) else None,
                reasons=[
                    f"Profit-take trigger: position is worth {value_multiple:.1f}× cost basis "
                    f"(threshold {profit_rules['take_profit_1']:.1f}×). P&L = {pnl_str}.",
                    "Recommend closing 50% of position to capture gains.",
                    "Keep remaining protection in place while the hedge thesis is still active.",
                ],
                close_fraction=0.5,
                pnl_dollars=pnl_dollars,
                pnl_pct=pnl_pct,
            )

    # ── Trigger 2: Regime improvement exit ────────────────────────────────────
    # Regime has improved (moved to lower risk) AND position has meaningful gains.
    # The risk we were hedging against has reduced — lock in the premium value.
    MIN_GAIN_FOR_REGIME_EXIT = 0.50   # position must be up at least 50% to trigger
    if (
        current_regime is not None
        and entry_regime is not None
        and _regime_risk_rank(current_regime) < _regime_risk_rank(entry_regime)
        and pnl_pct is not None
        and pnl_pct >= MIN_GAIN_FOR_REGIME_EXIT
    ):
        return HedgeEfficiencyResult(
            decision="close_regime_exit",
            score=80.0,
            dte=dte,
            moneyness=strike / underlying_price if (strike and underlying_price and underlying_price > 0) else None,
            reasons=[
                f"Regime has improved from {entry_regime} → {current_regime}.",
                f"Position up {pnl_pct:.0%} — lock in gains now that risk has reduced.",
                "Close 100% — hedge was built for a worse regime that no longer applies.",
            ],
            close_fraction=1.0,
            pnl_dollars=pnl_dollars,
            pnl_pct=pnl_pct,
        )

    # ── Trigger 3: Decay close ────────────────────────────────────────────────
    # Position has lost most of its value AND is near expiry.
    # Not worth keeping — transaction costs exceed remaining value.
    if (
        value_multiple is not None
        and value_multiple <= decay_threshold
        and dte is not None
        and dte < 21
    ):
        return HedgeEfficiencyResult(
            decision="close_decay",
            score=-99.0,
            dte=dte,
            moneyness=strike / underlying_price if (strike and underlying_price and underlying_price > 0) else None,
            reasons=[
                f"Decay close trigger: position worth only {value_multiple:.0%} of cost basis "
                f"(threshold {decay_threshold:.0%}) with {dte} DTE.",
                "Let expire or close cheaply — remaining value below transaction cost threshold.",
            ],
            close_fraction=1.0,
            pnl_dollars=pnl_dollars,
            pnl_pct=pnl_pct,
        )

    # ── Score-based logic (existing — unchanged) ──────────────────────────────
    if strike is None or underlying_price is None or underlying_price <= 0:
        return HedgeEfficiencyResult(
            decision="replace",
            score=0.0,
            dte=dte,
            moneyness=None,
            reasons=["Missing strike or underlying price."],
            pnl_dollars=pnl_dollars,
            pnl_pct=pnl_pct,
        )

    moneyness = strike / underlying_price
    vix = float(vix_level or 20.0)
    score = 0.0

    # 1) DTE component
    if dte is None:
        reasons.append("DTE unavailable.")
    elif dte > 45:
        score += 2.0
        reasons.append(f"DTE is healthy at {dte} days.")
    elif 20 <= dte <= 45:
        score += 1.0
        reasons.append(f"DTE is moderate at {dte} days.")
    elif 10 <= dte < 20:
        score -= 1.0
        reasons.append(f"DTE is getting short at {dte} days.")
    else:
        score -= 2.5
        reasons.append(f"DTE is very short at {dte} days.")

    # 2) Moneyness component
    if moneyness >= 1.00:
        score += 2.0
        reasons.append(f"Put is ITM/ATM-like with strike ratio {moneyness:.2f}.")
    elif 0.95 <= moneyness < 1.00:
        score += 1.5
        reasons.append(f"Put is near-ATM with strike ratio {moneyness:.2f}.")
    elif 0.88 <= moneyness < 0.95:
        score += 0.75
        reasons.append(f"Put is still relevant OTM protection with strike ratio {moneyness:.2f}.")
    else:
        score -= 1.5
        reasons.append(f"Put is far OTM with strike ratio {moneyness:.2f}.")

    # 3) Vol regime component
    if vix >= 28:
        score += 1.25
        reasons.append(f"Vol regime is elevated (VIX {vix:.1f}); keeping existing hedge is favored.")
    elif 20 <= vix < 28:
        score += 0.5
        reasons.append(f"Vol regime is moderate (VIX {vix:.1f}).")
    else:
        score -= 0.5
        reasons.append(f"Vol regime is subdued (VIX {vix:.1f}); replacing can be more attractive.")

    # 4) Carry / decay heuristic
    if dte is not None and dte < 14 and moneyness < 0.95:
        score -= 1.5
        reasons.append("Short-dated and OTM: carry efficiency is deteriorating.")
    elif dte is not None and dte < 21:
        score -= 0.75
        reasons.append("Time decay is accelerating.")
    else:
        score += 0.25
        reasons.append("Carry profile is still acceptable.")

    # Add P&L context to reasons if available
    if value_multiple is not None:
        reasons.append(f"Current value is {value_multiple:.1f}× cost basis.")

    # Decision bands
    if score >= 2.5:
        decision: OptimizerDecision = "keep"
    elif score >= 0.5:
        decision = "roll"
    else:
        decision = "replace"

    return HedgeEfficiencyResult(
        decision=decision,
        score=score,
        dte=dte,
        moneyness=moneyness,
        reasons=reasons,
        close_fraction=1.0,
        pnl_dollars=pnl_dollars,
        pnl_pct=pnl_pct,
    )