"""
hedge_config.py

Central policy constants for the hedge engine.
Edit this file to change hedge policy. Restart the server after changes.
These values are intentionally human-readable — no magic numbers buried in code.
"""

from __future__ import annotations

# ── Loss tolerance ─────────────────────────────────────────────────────────────
# Maximum portfolio loss you are willing to accept in the worst-case crash scenario
# implied by the current regime.  Options are sized to cover any loss beyond this.
#
# Trade-off:
#   0.08  → very aggressive protection, ~3-4% annual premium drag
#   0.12  → recommended — covers -30% SPY crash, ~1.5-2% annual drag
#   0.15  → light touch, nearly free at current portfolio composition
#   0.20  → no puts needed with current structural positions
MAX_TOLERATED_LOSS_PCT: float = 0.12


# ── Regime scenario drops ──────────────────────────────────────────────────────
# The SPY drop scenario to size hedges against per regime.
# primary = what the primary put spread must protect against (moderate pullback)
# tail    = what the tail put spread must protect against (crash scenario)
#
# In early_breakdown: two real scenarios — correction (-15%) is most likely,
# crash (-30%) is the tail. Both are hedged; budget splits 75/25 primary/tail.
REGIME_SCENARIO_DROPS: dict[str, dict[str, float]] = {
    "strong_bull":      {"primary": 0.20, "tail": 0.35},
    "extended_bull":    {"primary": 0.20, "tail": 0.30},
    "neutral":          {"primary": 0.20, "tail": 0.30},
    "early_breakdown":  {"primary": 0.15, "tail": 0.30},
    "high_crash_risk":  {"primary": 0.10, "tail": 0.35},
    "localized_bubble": {"primary": 0.20, "tail": 0.30},
}

# Crash beta multiplier by regime — how much worse beta gets in a fast selloff.
# These are empirical estimates, not the old synthetic beta*1.35.
# In strong_bull markets, leveraged ETFs don't get as much gamma — multiplier is lower.
# In high_crash_risk, everything amplifies — multiplier is highest.
REGIME_CRASH_BETA_MULTIPLIER: dict[str, float] = {
    "strong_bull":      1.10,
    "extended_bull":    1.20,
    "neutral":          1.25,
    "early_breakdown":  1.35,
    "high_crash_risk":  1.50,
    "localized_bubble": 1.20,
}

# ── Beta direction thresholds ──────────────────────────────────────────────────
# Controls when the engine switches from put hedging to call hedging (or neither).
#
# net_beta > +BETA_LONG_THRESHOLD   → hedge downside with puts (normal mode)
# net_beta < -BETA_SHORT_THRESHOLD  → hedge upside with calls (over-hedged / short bias)
# between the two                   → balanced, no directional hedge needed
BETA_LONG_THRESHOLD:  float = 0.05   # above this → buy puts
BETA_SHORT_THRESHOLD: float = 0.05   # below negative this → buy calls (future phase)


# ── Budget guardrails ─────────────────────────────────────────────────────────
# Maximum premium to spend per year as % of portfolio, by regime.
# This is the COST cap — the crash-loss math may say "buy $50k of puts"
# but the budget cap limits how much premium you actually spend.
# If crash-loss math requires less than budget, the lower number wins.
HEDGE_BUDGET_PCT: dict[str, float] = {
    "strong_bull":      0.0125,
    "extended_bull":    0.0250,
    "neutral":          0.0150,
    "early_breakdown":  0.0350,
    "high_crash_risk":  0.0500,
    "localized_bubble": 0.0100,
}


# ── Profit-taking thresholds ──────────────────────────────────────────────────
# If an option position has appreciated this much above cost basis → close 50%.
PROFIT_TAKE_MULTIPLIER: float = 2.5    # 2.5× cost basis → take half off

# If an option position has decayed to this fraction of cost basis AND DTE < 21 → let expire.
DECAY_CLOSE_THRESHOLD: float = 0.30   # worth less than 30% of what you paid

# ── Structural hedge symbols ───────────────────────────────────────────────────
# These positions have negative beta already factored into portfolio_beta.
# They are NOT counted as "option hedges" — they are part of portfolio composition.
# Do not add them to current_hedge_pct (that's the double-count bug).
STRUCTURAL_HEDGE_SYMBOLS: frozenset[str] = frozenset({
    "PSQ",
    "SQQQ",
    "SOXS",
    "UVXY",
    "VIXM",
    "TMV",
    "EUM",
    "EDZ",
    "SH"
    "TMF",
})