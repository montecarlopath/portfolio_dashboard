from __future__ import annotations

from typing import Dict, List, Any

from app.schemas import MarketRegimeResponse, RegimeSignalSnapshot


REGIME_THRESHOLDS = {
    "extended_distance_from_200dma_pct": 12.0,
    "very_extended_distance_from_200dma_pct": 15.0,
    "high_rsi": 70.0,
    "bullish_breadth": 65.0,
    "weak_breadth": 45.0,
    "very_weak_breadth": 35.0,
    "low_vix": 18.0,
    "moderate_vix": 24.0,
    "high_vix": 30.0,
}


def _clamp(v: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, v))


def _coerce_signal_snapshot(signals: Dict[str, Any]) -> RegimeSignalSnapshot:
    """
    Build a schema-safe RegimeSignalSnapshot from a possibly incomplete dict.
    Missing optional fields are allowed; missing required fields get sane defaults.
    """
    return RegimeSignalSnapshot(
        spy_above_50dma=bool(signals.get("spy_above_50dma", False)),
        spy_above_200dma=bool(signals.get("spy_above_200dma", False)),
        spy_distance_from_200dma_pct=float(signals.get("spy_distance_from_200dma_pct", 0.0) or 0.0),
        spy_rsi_14=float(signals.get("spy_rsi_14", 50.0) or 50.0),
        breadth_pct_above_200dma=float(signals.get("breadth_pct_above_200dma", 50.0) or 50.0),
        vix_level=float(signals.get("vix_level", 20.0) or 20.0),
        vix_term_structure_ratio=(
            float(signals["vix_term_structure_ratio"])
            if signals.get("vix_term_structure_ratio") is not None
            else None
        ),
        credit_stress_score=(
            float(signals["credit_stress_score"])
            if signals.get("credit_stress_score") is not None
            else None
        ),
        liquidity_stress_score=(
            float(signals["liquidity_stress_score"])
            if signals.get("liquidity_stress_score") is not None
            else None
        ),
        localized_bubble_score=(
            float(signals["localized_bubble_score"])
            if signals.get("localized_bubble_score") is not None
            else None
        ),
    )


def classify_market_regime(signals: Dict[str, Any]) -> MarketRegimeResponse:
    """
    Expected signal input shape:

    signals = {
        "spy_above_50dma": True,
        "spy_above_200dma": True,
        "spy_distance_from_200dma_pct": 13.4,
        "spy_rsi_14": 72.1,
        "breadth_pct_above_200dma": 68.0,
        "vix_level": 17.2,
        "vix_term_structure_ratio": 1.08,   # optional
        "credit_stress_score": 0.25,        # optional, normalized 0..1 preferred
        "liquidity_stress_score": 0.30,     # optional, normalized 0..1 preferred
        "localized_bubble_score": 0.40,     # optional, normalized 0..1 preferred
    }
    """
    reasons: List[str] = []

    spy_above_50dma = bool(signals.get("spy_above_50dma", False))
    spy_above_200dma = bool(signals.get("spy_above_200dma", False))
    dist_200 = float(signals.get("spy_distance_from_200dma_pct", 0.0) or 0.0)
    rsi = float(signals.get("spy_rsi_14", 50.0) or 50.0)
    breadth = float(signals.get("breadth_pct_above_200dma", 50.0) or 50.0)
    vix = float(signals.get("vix_level", 20.0) or 20.0)

    vix_term = (
        float(signals["vix_term_structure_ratio"])
        if signals.get("vix_term_structure_ratio") is not None
        else None
    )
    credit = float(signals.get("credit_stress_score", 0.0) or 0.0)
    liquidity = float(signals.get("liquidity_stress_score", 0.0) or 0.0)
    bubble = float(signals.get("localized_bubble_score", 0.0) or 0.0)

    snapshot = _coerce_signal_snapshot(signals)

    # ------------------------------------------------------------------
    # 1) HIGH CRASH RISK
    # ------------------------------------------------------------------
    # Priority regime. If macro / breadth / volatility conditions are bad enough,
    # override everything else.
    high_crash_conditions = [
        (not spy_above_200dma and breadth <= REGIME_THRESHOLDS["weak_breadth"]),
        vix >= REGIME_THRESHOLDS["high_vix"],
        credit >= 0.75,
        liquidity >= 0.75,
        (vix_term is not None and vix_term < 1.0),  # backwardation / stress
    ]

    if any(high_crash_conditions):
        reasons.append("Broad market trend and/or macro stress indicates elevated crash risk.")

        if (
            signals.get("spy_above_200dma") is False
            and signals.get("spy_distance_from_200dma_pct") is not None
        ):
            reasons.append("SPY is below the 200-DMA.")
        if breadth <= REGIME_THRESHOLDS["weak_breadth"]:
            reasons.append("Breadth is weak.")
        if breadth <= REGIME_THRESHOLDS["very_weak_breadth"]:
            reasons.append("Breadth is severely weak.")
        if vix >= REGIME_THRESHOLDS["high_vix"]:
            reasons.append("VIX is elevated.")
        if vix_term is not None and vix_term < 1.0:
            reasons.append("VIX term structure is inverted/backwardated.")
        if credit >= 0.75:
            reasons.append("Credit stress is high.")
        if liquidity >= 0.75:
            reasons.append("Liquidity stress is high.")

        market_risk_score = _clamp(
            0.75
            + 0.10 * float(vix >= REGIME_THRESHOLDS["high_vix"])
            + 0.05 * float(not spy_above_200dma)
            + 0.05 * float(breadth <= REGIME_THRESHOLDS["very_weak_breadth"])
            + 0.05 * float(credit >= 0.75)
            + 0.05 * float(liquidity >= 0.75)
        )

        # In full panic, target hedge can be high, but NEW hedge adding should be conservative.
        aggressiveness = "low" if vix >= REGIME_THRESHOLDS["high_vix"] else "medium"

        return MarketRegimeResponse(
            regime="high_crash_risk",
            market_risk_score=market_risk_score,
            new_hedge_aggressiveness=aggressiveness,
            signals=snapshot,
            reasons=reasons,
        )

    # ------------------------------------------------------------------
    # 2) EARLY BREAKDOWN
    # ------------------------------------------------------------------
    early_breakdown = (
        (not spy_above_50dma and spy_above_200dma)
        or breadth < REGIME_THRESHOLDS["weak_breadth"]
        or (
            spy_above_200dma
            and vix >= REGIME_THRESHOLDS["moderate_vix"]
            and not spy_above_50dma
        )
    )

    if early_breakdown:
        reasons.append("Market is losing momentum and breadth is weakening.")

        if not spy_above_50dma:
            reasons.append("SPY is below the 50-DMA.")
        if breadth < REGIME_THRESHOLDS["weak_breadth"]:
            reasons.append("Breadth has weakened materially.")
        if vix >= REGIME_THRESHOLDS["moderate_vix"]:
            reasons.append("Volatility is rising.")

        market_risk_score = _clamp(
            0.60
            + 0.05 * float(not spy_above_50dma)
            + 0.05 * float(breadth < REGIME_THRESHOLDS["weak_breadth"])
            + 0.05 * float(vix >= REGIME_THRESHOLDS["moderate_vix"])
        )

        return MarketRegimeResponse(
            regime="early_breakdown",
            market_risk_score=market_risk_score,
            new_hedge_aggressiveness="medium",
            signals=snapshot,
            reasons=reasons,
        )

    # ------------------------------------------------------------------
    # 3) LOCALIZED BUBBLE
    # ------------------------------------------------------------------
    # Keep this ahead of extended bull so concentrated overheated sleeves can be flagged.
    if bubble >= 0.75:
        reasons.append("A specific sector or sleeve appears overheated even if the broad market is not.")
        if dist_200 >= REGIME_THRESHOLDS["extended_distance_from_200dma_pct"]:
            reasons.append("Broad market is also somewhat extended.")
        if vix <= REGIME_THRESHOLDS["moderate_vix"]:
            reasons.append("Volatility is not yet pricing systemic stress.")

        return MarketRegimeResponse(
            regime="localized_bubble",
            market_risk_score=_clamp(0.50 + 0.10 * float(bubble >= 0.90)),
            new_hedge_aggressiveness="medium",
            signals=snapshot,
            reasons=reasons,
        )

    # ------------------------------------------------------------------
    # 4) EXTENDED BULL
    # ------------------------------------------------------------------
    extended_bull = (
        spy_above_50dma
        and spy_above_200dma
        and breadth >= REGIME_THRESHOLDS["bullish_breadth"]
        and (
            dist_200 >= REGIME_THRESHOLDS["extended_distance_from_200dma_pct"]
            or rsi >= REGIME_THRESHOLDS["high_rsi"]
        )
    )

    if extended_bull:
        reasons.append("Trend is strong, but the market is becoming extended.")

        if dist_200 >= REGIME_THRESHOLDS["extended_distance_from_200dma_pct"]:
            reasons.append("SPY is meaningfully above the 200-DMA.")
        if dist_200 >= REGIME_THRESHOLDS["very_extended_distance_from_200dma_pct"]:
            reasons.append("SPY is very extended above the 200-DMA.")
        if rsi >= REGIME_THRESHOLDS["high_rsi"]:
            reasons.append("RSI is elevated.")
        if breadth >= REGIME_THRESHOLDS["bullish_breadth"]:
            reasons.append("Breadth remains strong.")
        if vix <= REGIME_THRESHOLDS["moderate_vix"]:
            reasons.append("Volatility is not yet in panic territory.")

        market_risk_score = _clamp(
            0.40
            + 0.10 * float(dist_200 >= REGIME_THRESHOLDS["very_extended_distance_from_200dma_pct"])
            + 0.10 * float(rsi >= 75.0)
            + 0.05 * float(vix <= REGIME_THRESHOLDS["low_vix"])
        )

        # In low vol + extended market, it's a good time to add hedges.
        aggressiveness = "high" if vix < REGIME_THRESHOLDS["low_vix"] else "medium"

        return MarketRegimeResponse(
            regime="extended_bull",
            market_risk_score=market_risk_score,
            new_hedge_aggressiveness=aggressiveness,
            signals=snapshot,
            reasons=reasons,
        )

    # ------------------------------------------------------------------
    # 5) STRONG BULL
    # ------------------------------------------------------------------
    strong_bull = (
        spy_above_50dma
        and spy_above_200dma
        and breadth >= REGIME_THRESHOLDS["bullish_breadth"]
        and dist_200 < REGIME_THRESHOLDS["extended_distance_from_200dma_pct"]
        and rsi < REGIME_THRESHOLDS["high_rsi"]
    )

    if strong_bull:
        reasons.append("Trend and breadth are strong without major overheating.")
        if vix <= REGIME_THRESHOLDS["low_vix"]:
            reasons.append("Volatility remains subdued.")
        if dist_200 > 0:
            reasons.append("SPY remains above the 200-DMA.")

        return MarketRegimeResponse(
            regime="strong_bull",
            market_risk_score=0.25,
            new_hedge_aggressiveness="medium" if vix < REGIME_THRESHOLDS["low_vix"] else "low",
            signals=snapshot,
            reasons=reasons,
        )

    # ------------------------------------------------------------------
    # 6) FALLBACK NEUTRAL
    # ------------------------------------------------------------------
    reasons.append("Signals are mixed and do not cleanly map to a stronger regime label.")
    if spy_above_200dma:
        reasons.append("Long-term trend is still constructive.")
    if vix >= REGIME_THRESHOLDS["moderate_vix"]:
        reasons.append("Volatility is elevated enough to justify caution.")
    if breadth < REGIME_THRESHOLDS["bullish_breadth"]:
        reasons.append("Breadth is not fully supportive.")

    return MarketRegimeResponse(
        regime="neutral",
        market_risk_score=0.40,
        new_hedge_aggressiveness="low",
        signals=snapshot,
        reasons=reasons,
    )