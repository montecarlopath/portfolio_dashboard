from __future__ import annotations

"""
crash_simulation_engine.py

Simulates portfolio P&L under market drop scenarios.

Math per scenario:
    portfolio_loss    = portfolio_crash_beta_dollars × drop_pct
    structural_gain   = structural_hedge_exposure_dollars × drop_pct × decay_factor
    option_gain       = option_hedge_exposure_dollars × drop_pct × convexity_factor
    net_result        = structural_gain + option_gain − portfolio_loss
    hedge_offset_pct  = (structural_gain + option_gain) / portfolio_loss

Key factors applied:
  - crash_beta (not normal beta) — portfolio loses more in a crash than beta implies
  - structural decay — leveraged inverse ETFs (SQQQ, PSQ etc.) lose efficiency
    at larger drops due to daily rebalancing / vol drag. Applied as a linear
    decay that reaches ~0.70× at a 30% market drop.
  - option convexity — put options gain convexity as they move deeper ITM.
    A 30% drop produces ~2× the linear delta gain because gamma accelerates.
    Applied as a factor anchored to 1.0× at a 10% drop.

These factors are reasonable approximations. A full model would require the
IV surface, term structure, and individual option Greeks per position. The
purpose here is directional sizing intuition, not P&L attribution.
"""

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# ── Default scenario set ──────────────────────────────────────────────────────
DEFAULT_SCENARIOS_PCT = [0.05, 0.10, 0.15, 0.20, 0.25, 0.30]

# ── Structural hedge decay factors (leveraged inverse ETF vol drag) ────────────
# At a 5% drop → 1.00× (no decay, move is small enough to be near-linear)
# At a 30% drop → 0.72× (significant decay from daily rebalancing drag)
STRUCTURAL_DECAY: Dict[float, float] = {
    0.05: 1.00,
    0.10: 0.95,
    0.15: 0.90,
    0.20: 0.85,
    0.25: 0.78,
    0.30: 0.72,
}

# ── Option convexity factors (put gamma acceleration) ─────────────────────────
# At a 5% drop → 0.70× (OTM puts barely move)
# At a 10% drop → 1.00× (anchor — delta-linear approximation is reasonable here)
# At a 30% drop → 2.20× (puts deep ITM, gamma has added significantly to gains)
OPTION_CONVEXITY: Dict[float, float] = {
    0.05: 0.70,
    0.10: 1.00,
    0.15: 1.25,
    0.20: 1.55,
    0.25: 1.85,
    0.30: 2.20,
}


# ── Result types ──────────────────────────────────────────────────────────────

@dataclass
class ScenarioResult:
    drop_pct: float               # e.g. 0.10 = 10% market drop
    drop_label: str               # e.g. "-10%"

    # Losses
    portfolio_loss_dollars: float

    # Gains (positive numbers = money made by hedges)
    structural_gain_dollars: float
    option_gain_dollars: float
    total_hedge_gain_dollars: float

    # Net
    net_dollars: float            # negative = net loss, positive = net gain
    hedge_offset_pct: float       # what % of portfolio loss the hedges cover

    # Context
    structural_decay_factor: float
    option_convexity_factor: float

    def to_dict(self) -> Dict[str, Any]:
        return {
            "drop_pct": self.drop_pct,
            "drop_label": self.drop_label,
            "portfolio_loss_dollars": round(self.portfolio_loss_dollars, 0),
            "structural_gain_dollars": round(self.structural_gain_dollars, 0),
            "option_gain_dollars": round(self.option_gain_dollars, 0),
            "total_hedge_gain_dollars": round(self.total_hedge_gain_dollars, 0),
            "net_dollars": round(self.net_dollars, 0),
            "hedge_offset_pct": round(self.hedge_offset_pct, 4),
            "structural_decay_factor": self.structural_decay_factor,
            "option_convexity_factor": self.option_convexity_factor,
        }


@dataclass
class CrashSimulationResult:
    portfolio_value: float
    portfolio_beta: float
    portfolio_crash_beta: float
    portfolio_crash_beta_dollars: float    # = portfolio_value × crash_beta
    structural_hedge_exposure_dollars: float
    option_hedge_exposure_dollars: float
    total_hedge_exposure_dollars: float

    scenarios: List[ScenarioResult] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "portfolio_value": round(self.portfolio_value, 2),
            "portfolio_beta": round(self.portfolio_beta, 4),
            "portfolio_crash_beta": round(self.portfolio_crash_beta, 4),
            "portfolio_crash_beta_dollars": round(self.portfolio_crash_beta_dollars, 0),
            "structural_hedge_exposure_dollars": round(self.structural_hedge_exposure_dollars, 0),
            "option_hedge_exposure_dollars": round(self.option_hedge_exposure_dollars, 0),
            "total_hedge_exposure_dollars": round(self.total_hedge_exposure_dollars, 0),
            "scenarios": [s.to_dict() for s in self.scenarios],
            "notes": self.notes,
        }


# ── Interpolation helpers ──────────────────────────────────────────────────────

def _interpolate(table: Dict[float, float], drop_pct: float) -> float:
    """
    Linearly interpolate a factor from the lookup table for arbitrary drop_pct.
    Clamps to the nearest table boundary if outside range.
    """
    keys = sorted(table.keys())
    if drop_pct <= keys[0]:
        return table[keys[0]]
    if drop_pct >= keys[-1]:
        return table[keys[-1]]

    for i in range(len(keys) - 1):
        lo, hi = keys[i], keys[i + 1]
        if lo <= drop_pct <= hi:
            t = (drop_pct - lo) / (hi - lo)
            return table[lo] + t * (table[hi] - table[lo])

    return 1.0


# ── Core scenario calculator ──────────────────────────────────────────────────

def _run_scenario(
    *,
    drop_pct: float,
    portfolio_crash_beta_dollars: float,
    structural_hedge_exposure_dollars: float,
    option_hedge_exposure_dollars: float,
) -> ScenarioResult:
    decay = _interpolate(STRUCTURAL_DECAY, drop_pct)
    convexity = _interpolate(OPTION_CONVEXITY, drop_pct)

    portfolio_loss = portfolio_crash_beta_dollars * drop_pct
    structural_gain = structural_hedge_exposure_dollars * drop_pct * decay
    option_gain = option_hedge_exposure_dollars * drop_pct * convexity

    total_hedge_gain = structural_gain + option_gain
    net = total_hedge_gain - portfolio_loss

    hedge_offset = total_hedge_gain / portfolio_loss if portfolio_loss > 0 else 0.0

    pct_int = round(drop_pct * 100)
    label = f"-{pct_int}%"

    return ScenarioResult(
        drop_pct=drop_pct,
        drop_label=label,
        portfolio_loss_dollars=portfolio_loss,
        structural_gain_dollars=structural_gain,
        option_gain_dollars=option_gain,
        total_hedge_gain_dollars=total_hedge_gain,
        net_dollars=net,
        hedge_offset_pct=hedge_offset,
        structural_decay_factor=decay,
        option_convexity_factor=convexity,
    )


# ── Public entry point ────────────────────────────────────────────────────────

def run_crash_simulation(
    *,
    portfolio_value: float,
    portfolio_beta: float,
    portfolio_crash_beta: float,
    structural_hedge_exposure_dollars: float,
    option_hedge_exposure_dollars: float,
    scenarios_pct: Optional[List[float]] = None,
) -> CrashSimulationResult:
    """
    Run crash scenarios given hedge intelligence inputs.

    All inputs are available directly from GET /api/risk/hedge-intelligence.

    Args:
        portfolio_value:                  total portfolio value in dollars
        portfolio_beta:                   normal SPY beta
        portfolio_crash_beta:             tail-risk beta (typically 1.35× normal)
        structural_hedge_exposure_dollars: beta-adjusted hedge from inverse ETFs
        option_hedge_exposure_dollars:     delta-adjusted hedge from puts/spreads
        scenarios_pct:                    list of drop fractions to simulate
                                          (default: 5%, 10%, 15%, 20%, 25%, 30%)
    """
    if scenarios_pct is None:
        scenarios_pct = DEFAULT_SCENARIOS_PCT

    # Validate inputs
    portfolio_value = max(float(portfolio_value or 0.0), 0.0)
    portfolio_beta = float(portfolio_beta or 0.0)
    portfolio_crash_beta = float(portfolio_crash_beta or 0.0)
    structural = max(float(structural_hedge_exposure_dollars or 0.0), 0.0)
    options = max(float(option_hedge_exposure_dollars or 0.0), 0.0)

    portfolio_crash_beta_dollars = portfolio_value * portfolio_crash_beta

    notes = []
    if portfolio_crash_beta_dollars <= 0:
        notes.append("Warning: portfolio_crash_beta_dollars is zero — loss estimates will be zero.")
    if structural == 0 and options == 0:
        notes.append("No hedges detected — hedge gain columns will be zero.")
    notes.append(
        "Structural gains include leverage decay factor (inverse ETF vol drag). "
        "Option gains include convexity factor (put gamma acceleration)."
    )
    notes.append(
        "These are approximations. Full accuracy requires per-position Greeks and IV surface."
    )

    scenarios = [
        _run_scenario(
            drop_pct=pct,
            portfolio_crash_beta_dollars=portfolio_crash_beta_dollars,
            structural_hedge_exposure_dollars=structural,
            option_hedge_exposure_dollars=options,
        )
        for pct in sorted(set(max(0.01, min(p, 0.99)) for p in scenarios_pct))
    ]

    return CrashSimulationResult(
        portfolio_value=portfolio_value,
        portfolio_beta=portfolio_beta,
        portfolio_crash_beta=portfolio_crash_beta,
        portfolio_crash_beta_dollars=portfolio_crash_beta_dollars,
        structural_hedge_exposure_dollars=structural,
        option_hedge_exposure_dollars=options,
        total_hedge_exposure_dollars=structural + options,
        scenarios=scenarios,
        notes=notes,
    )