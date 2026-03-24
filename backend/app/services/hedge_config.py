"""
hedge_config.py

Central policy constants for the hedge engine.
Edit this file to change hedge policy. Restart the server after changes.
These values are intentionally human-readable — no magic numbers buried in code.
"""

from __future__ import annotations

# =============================================================================
# LOSS TOLERANCE
# =============================================================================
# Maximum portfolio loss you are willing to accept in the worst-case crash
# scenario implied by the current regime.
#
# The hedge engine uses this to estimate how much downside protection is needed
# in theory. The final practical hedge may still be lower because of premium
# budget limits.
#
# Trade-off:
#   0.08  -> very aggressive protection, high premium drag
#   0.12  -> balanced default; meaningful crash protection with manageable drag
#   0.15  -> lighter protection, lower cost
#   0.20  -> may require little or no option hedging if structural hedges exist
# =============================================================================
MAX_TOLERATED_LOSS_PCT: float = 0.12


# =============================================================================
# REGIME SCENARIO DROPS
# =============================================================================
# The SPY drop scenario used for hedge sizing in each regime.
#
# primary:
#   Moderate drawdown / correction scenario that primary hedges should address.
#
# tail:
#   Crash scenario that tail hedges should address.
#
# In early_breakdown, the model treats both:
# - correction (-15%) as the more likely scenario
# - crash (-30%) as the tail scenario
# =============================================================================
REGIME_SCENARIO_DROPS: dict[str, dict[str, float]] = {
    "strong_bull":      {"primary": 0.20, "tail": 0.35},
    "extended_bull":    {"primary": 0.20, "tail": 0.30},
    "neutral":          {"primary": 0.20, "tail": 0.30},
    "early_breakdown":  {"primary": 0.15, "tail": 0.30},
    "high_crash_risk":  {"primary": 0.10, "tail": 0.35},
    "localized_bubble": {"primary": 0.20, "tail": 0.30},
}


# =============================================================================
# REGIME CRASH BETA MULTIPLIER
# =============================================================================
# How much worse beta becomes in a fast selloff.
#
# This is used to estimate crash beta rather than assuming a fixed uplift.
# In calmer / strong-bull regimes, beta tends to amplify less in a crash.
# In high-crash-risk regimes, beta amplification can be much more severe.
# =============================================================================
REGIME_CRASH_BETA_MULTIPLIER: dict[str, float] = {
    "strong_bull":      1.10,
    "extended_bull":    1.20,
    "neutral":          1.25,
    "early_breakdown":  1.35,
    "high_crash_risk":  1.50,
    "localized_bubble": 1.20,
}

# =============================================================================
# STRUCTURE ALLOCATION BY REGIME
# =============================================================================
# Fractions of factor hedge budget allocated to each hedge structure.
#
# These weights apply AFTER factor budget is determined.
# So:
#   total hedge budget
#   -> factor budgets
#   -> structure budgets within each factor
#
# Rules:
# - primary = smoother, more linear drawdown control
# - tail = capped convex crash protection
# - convex = true crisis convexity (ratio backspread / future structures)
# =============================================================================

STRUCTURE_ALLOCATION_BY_REGIME: dict[str, dict[str, float]] = {
    "strong_bull": {
        "primary": 1.00,
        "tail": 0.00,
        "convex": 0.00,
    },
    "extended_bull": {
        "primary": 0.75,
        "tail": 0.20,
        "convex": 0.05,
    },
    "neutral": {
        "primary": 0.60,
        "tail": 0.30,
        "convex": 0.10,
    },
    "early_breakdown": {
        "primary": 0.50,
        "tail": 0.35,
        "convex": 0.15,
    },
    "high_crash_risk": {
        "primary": 0.65,
        "tail": 0.30,
        "convex": 0.05,
    },
    "localized_bubble": {
        "primary": 0.55,
        "tail": 0.35,
        "convex": 0.10,
    },
}

HEDGE_STYLE_STRUCTURE_SPLIT_MAP = {
    "balanced": {"primary": 0.60, "tail": 0.30, "convex": 0.10},
    "correction_focused": {"primary": 0.70, "tail": 0.25, "convex": 0.05},
    "crash_paranoid": {"primary": 0.45, "tail": 0.40, "convex": 0.15},
    "cost_sensitive": {"primary": 0.80, "tail": 0.15, "convex": 0.05},
}

# =============================================================================
# BETA DIRECTION THRESHOLDS
# =============================================================================
# Controls when the engine should think in terms of downside hedging with puts
# versus upside hedging with calls.
#
# net_beta > +BETA_LONG_THRESHOLD   -> downside hedge with puts
# net_beta < -BETA_SHORT_THRESHOLD  -> upside hedge with calls (future path)
# in-between                        -> no strong directional hedge needed
# =============================================================================
BETA_LONG_THRESHOLD: float = 0.05
BETA_SHORT_THRESHOLD: float = 0.05


# =============================================================================
# HEDGE BUDGET POLICY
# =============================================================================
# Maximum hedge premium spend as a fraction of portfolio by regime.
#
# Important:
# - This is a COST budget, not a hedge notional target.
# - The crash math may say you need far more protection in theory.
# - The planner uses this budget to determine what is actually feasible.
# =============================================================================
HEDGE_BUDGET_PCT: dict[str, float] = {
    "strong_bull":      0.0125,
    "extended_bull":    0.0250,
    "neutral":          0.0150,
    "early_breakdown":  0.0350,
    "high_crash_risk":  0.0500,
    "localized_bubble": 0.0100,
}


# =============================================================================
# PROFIT-TAKING RULES BY HEDGE STRUCTURE
# =============================================================================
# These are structure-aware exit rules.
#
# Different hedge shapes should be harvested differently:
# - naked_put        -> monetize earlier
# - primary_spread   -> medium-speed harvesting
# - tail_spread      -> hold longer for convexity
# - ratio_backspread -> hold longest; true crisis convexity
#
# Interpretation:
# - take_profit_1 / take_profit_2 / full_exit are value multiples of cost basis
# - vol_spike_exit means implied vol is rich enough to justify harvesting gains
# =============================================================================
PROFIT_RULES = {
    "naked_put": {
        "take_profit_1": 1.8,
        "take_profit_2": 3.0,
        "full_exit": 5.0,
    },
    "primary_spread": {
        "take_profit_1": 2.0,
        "take_profit_2": 3.5,
        "full_exit": 5.0,
    },
    "tail_spread": {
        "take_profit_1": 3.0,
        "take_profit_2": 5.0,
        "full_exit": 8.0,
        "vol_spike_exit": 30.0,
    },
    "ratio_backspread": {
        "take_profit_1": 5.0,
        "take_profit_2": 12.0,
        "full_exit": 25.0,
        "vol_spike_exit": 32.0,
        "crash_take_profit_drop_pct": 0.15,
        "crash_take_profit_fraction": 0.70,
    },
}


# =============================================================================
# DECAY RULES BY HEDGE STRUCTURE
# =============================================================================
# Defines when a hedge is considered too decayed / too close to expiry to keep.
#
# threshold:
#   Fraction of original cost basis below which the position is considered mostly
#   dead.
#
# dte_trigger:
#   Only apply the decay close rule when DTE is below this threshold.
# =============================================================================
DECAY_RULES = {
    "naked_put": {
        "threshold": 0.35,
        "dte_trigger": 21,
    },
    "primary_spread": {
        "threshold": 0.30,
        "dte_trigger": 21,
    },
    "tail_spread": {
        "threshold": 0.25,
        "dte_trigger": 25,
    },
    "ratio_backspread": {
        "threshold": 0.10,
        "dte_trigger": 14,
    },
}


# =============================================================================
# STRUCTURAL HEDGE SYMBOLS
# =============================================================================
# These are already-defensive / inverse / crash-sensitive holdings that should
# count as structural hedges in the portfolio.
#
# They affect total portfolio beta and crash behavior, but they are NOT counted
# as option hedges in current_hedge_pct.
# =============================================================================
STRUCTURAL_HEDGE_SYMBOLS: frozenset[str] = frozenset({
    "PSQ",
    "SQQQ",
    "SOXS",
    "UVXY",
    "VIXM",
    "TMV",
    "EUM",
    "EDZ",
    "SH",
    "TMF",
    "VIXY"
})


# =============================================================================
# CONVEX HEDGE BUDGET RESERVE
# =============================================================================
# Fraction of remaining hedge premium budget reserved for the convex sleeve.
#
# This is intended for crisis-convex structures such as future ratio
# backspreads. It is a fraction of hedge premium budget, NOT of portfolio.
#
# Philosophy:
# - strong_bull: no convex reserve needed
# - neutral / early_breakdown: reserve some convexity budget intentionally
# - high_crash_risk: lower reserve because convexity may be too expensive then
# =============================================================================
CONVEX_ALLOCATION_BY_REGIME: dict[str, float] = {
    "strong_bull": 0.00,
    "extended_bull": 0.05,
    "neutral": 0.10,
    "early_breakdown": 0.15,
    "high_crash_risk": 0.05,
    "localized_bubble": 0.08,
}


# =============================================================================
# FACTOR HEDGE PROXIES
# =============================================================================
# Tradable liquid proxies used to hedge each detected factor.
#
# Important:
# - These are only candidates.
# - A factor should only receive hedge budget if it is actually present in the
#   current holdings and exceeds the minimum exposure threshold.
# =============================================================================
FACTOR_HEDGE_PROXIES: dict[str, str] = {
    "tech": "QQQ",
    "btc": "IBIT",
    "gold": "GLD",
    "energy": "XLE",
    "residual_beta": "QQQ",
}


# =============================================================================
# FACTOR SYMBOL MAP
# =============================================================================
# Maps holdings to factor buckets.
#
# Design principle:
# - Factors should be dynamic and holding-aware.
# - If you do not own BTC names, BTC should get zero budget.
# - If you do not own gold / metals names, gold should get zero budget.
#
# Note:
# A symbol should ideally belong to one primary bucket only, to avoid double
# counting. Some names like NVDA can fit both "tech" and "semis"; for now the
# engine should treat the first matching factor as canonical, or you can later
# refine this with correlation-based logic.
# =============================================================================
FACTOR_SYMBOL_MAP: dict[str, set[str]] = {
    # -------------------------------------------------------------------------
    # TECH / GROWTH / QQQ-LIKE RISK
    # -------------------------------------------------------------------------
    "tech": {
        "QQQ",
        "TQQQ",
        "UPRO",
        "SPXL",
        "USD",
        "TECL",
        "SPY",
        "XLK",
        "NVDA",
        "TSLA",
        "AAPL",
        "MSFT",
        "AMZN",
        "META",
        "GOOGL",
        "SOXL",
        "SOXX",
        "SMH",
        "NVDA",
        "AMD",
        "AVGO",
        "TSM",
        "MU",
        "PSQ",
        "SQQQ",
        "SOXS",
        "UVXY",
        "VIXM",
        "EUM",
        "EDZ",
        "SH",
        "VIXY"
        "EDC"


    },

    # -------------------------------------------------------------------------
    # BTC / CRYPTO BETA
    # -------------------------------------------------------------------------
    "btc": {
        "IBIT",
        "FBTC",
        "BITB",
        "ARKB",
        "GBTC",
        "CIFR",
        "BITO",
        "MSTR",
        "MSTX",
        "COIN",
        "COINL",
        "WULF",
        "MARA",
        "RIOT",
        "CLSK",
        "IREN",
    },

    # -------------------------------------------------------------------------
    # GOLD / PRECIOUS METALS / HARD ASSET BETA
    # -------------------------------------------------------------------------
    # Includes silver / levered precious metals proxies for now.
    # You can split silver into its own factor later if needed.
    # -------------------------------------------------------------------------
    "gold": {
        "GLD",
        "IAU",
        "GDX",
        "GDXU",
        "SLV",
        "AGQ",
        "UGL",
        "GDMN",
    },

    # -------------------------------------------------------------------------
    # ENERGY
    # -------------------------------------------------------------------------
    "energy": {
        "XLE",
        "ERX",
        "UCO",
        "FRO",
        "XOP",
        "OIH",
        "XEG",
        "VDE",
    },
}


# =============================================================================
# MINIMUM FACTOR EXPOSURE TO QUALIFY FOR HEDGE BUDGET
# =============================================================================
# Minimum factor exposure as % of total portfolio before that factor is large
# enough to deserve its own hedge budget.
#
# Example:
# - If BTC exposure is only 2% of portfolio and threshold is 3%, BTC gets no
#   dedicated hedge budget.
# =============================================================================
FACTOR_MIN_EXPOSURE_PCT: dict[str, float] = {
    "tech": 0.08,
    "btc": 0.03,
    "gold": 0.03,
    "energy": 0.05,
}


# =============================================================================
# FACTOR BUDGET PRIORITY WEIGHTS
# =============================================================================
# Relative budget tilt applied after exposure is detected.
#
# Interpretation:
# - >1.0 means this factor should get slightly more budget than its raw exposure
# - <1.0 means slightly less
#
# This only affects factors that are actually present and above threshold.
# =============================================================================
FACTOR_BUDGET_PRIORITY: dict[str, float] = {
    "tech": 1.00,
    "btc": 1.25,
    "gold": 0.90,
    "energy": 0.90,
    "residual_beta": 1.00,
}


# =============================================================================
# REGIME-BASED FACTOR MULTIPLIERS
# =============================================================================
# Dynamic regime tilts applied on top of actual holdings exposure.
#
# Important:
# - These DO NOT force a factor allocation if the factor is absent.
# - They only tilt budget among factors that are already present.
#
# Example:
# - In early_breakdown, gold gets a higher multiplier if you own gold-sensitive
#   names, because defensive hard-asset hedging is more attractive.
# - In high_crash_risk, BTC gets reduced because BTC hedges may be more
#   expensive / less reliable at that point.
# =============================================================================
FACTOR_REGIME_MULTIPLIERS: dict[str, dict[str, float]] = {
    "strong_bull": {
        "btc": 0.8,
        "gold": 0.6,
        "tech": 1.0,
        "energy": 1.0,
        "residual_beta": 1.0,
    },
    "extended_bull": {
        "btc": 1.1,
        "gold": 0.7,
        "tech": 1.0,
        "energy": 1.0,
        "residual_beta": 1.0,
    },
    "neutral": {
        "btc": 1.0,
        "gold": 1.0,
        "tech": 1.0,
        "energy": 1.0,
        "residual_beta": 1.0,
    },
    "early_breakdown": {
        "btc": 0.9,
        "gold": 1.2,
        "tech": 1.1,
        "energy": 1.1,
        "residual_beta": 1.0,
    },
    "high_crash_risk": {
        "btc": 0.7,
        "gold": 1.3,
        "tech": 1.15,
        "energy": 1.15,
        "residual_beta": 1.0,
    },
    "localized_bubble": {
        "btc": 1.2,
        "gold": 0.9,
        "tech": 1.0,
        "energy": 1.1,
        "residual_beta": 1.0,
    },
}


# =============================================================================
# FACTOR ALLOCATION ENGINE SETTINGS
# =============================================================================
# Misc settings for the factor-based hedge allocator.
#
# core_factor:
#   Fallback main hedge factor if leftover factor budget needs a home.
#
# min_total_factor_budget_dollars:
#   If total remaining hedge budget is too small, skip factor splitting.
#
# reserve_unassigned_budget_to_core:
#   If a factor does not qualify or some budget is left over, route it back to
#   the core factor rather than leaving it idle.
# =============================================================================
FACTOR_ALLOCATION_SETTINGS = {
    # existing keys...
    "min_total_factor_budget_dollars": 500.0,
    "reserve_unassigned_budget_to_core": True,
    "core_factor": "tech",

    # new keys
    "cash_like_symbols": ["BIL", "BOXX","TBIL","SGOV"],
    "residual_beta_factor_name": "residual_beta",
    "residual_beta_hedge_proxy": "QQQ",
}



# =============================================================================
# FACTOR-SPECIFIC STRUCTURE MULTIPLIERS
# =============================================================================
# Tilts structure mix within a factor after regime weights are applied.
#
# Example:
# - BTC is more jumpy / nonlinear -> tilt away from primary, toward tail/convex
# - Gold often behaves more defensively already -> less convex emphasis
# =============================================================================

FACTOR_STRUCTURE_MULTIPLIERS: dict[str, dict[str, float]] = {
    "tech": {
        "primary": 1.00,
        "tail": 1.00,
        "convex": 1.00,
    },
    "semis": {
        "primary": 0.95,
        "tail": 1.05,
        "convex": 1.10,
    },
    "btc": {
        "primary": 0.75,
        "tail": 1.15,
        "convex": 1.35,
    },
    "gold": {
        "primary": 1.10,
        "tail": 0.95,
        "convex": 0.75,
    },
    "small_caps": {
        "primary": 0.95,
        "tail": 1.05,
        "convex": 1.05,
    },
    "bonds": {
        "primary": 1.10,
        "tail": 0.90,
        "convex": 0.70,
    },
}