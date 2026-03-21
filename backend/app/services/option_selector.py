from __future__ import annotations

from datetime import date, timedelta
from typing import Dict, List, Optional

from app.schemas import (
    HedgeSpreadSelectionResponse,
    OptionContractCandidate,
    OptionSpreadSelection,
    HedgeStyleType,
    MarketRegimeType,
)
from app.services.finnhub_market_data import get_latest_price
from app.services.option_chain_read import get_live_option_chain


PRIMARY_DTE_RANGE = (60, 90)
TAIL_DTE_RANGE = (90, 120)
TOP_N_LEG_CANDIDATES = 6


def _safe_date_diff(expiry_str: str, as_of_date: str) -> Optional[int]:
    try:
        expiry = date.fromisoformat(expiry_str)
        asof = date.fromisoformat(as_of_date)
        return (expiry - asof).days
    except Exception:
        return None


def _round_strike_score(strike: float) -> float:
    if strike <= 0:
        return 0.0
    if abs(strike % 5) < 1e-9:
        return 1.0
    if abs((strike * 2) % 5) < 1e-9:
        return 0.6
    return 0.2


def _quote_is_usable(bid: Optional[float], ask: Optional[float], mark: Optional[float]) -> bool:
    if mark is not None and mark > 0:
        return True
    if bid is None or ask is None:
        return False
    if bid < 0 or ask <= 0 or ask < bid:
        return False
    return True


def _quote_quality_score(
    bid: Optional[float],
    ask: Optional[float],
    mark: Optional[float],
) -> float:
    if not _quote_is_usable(bid, ask, mark):
        return 0.0

    if bid is not None and ask is not None and ask > 0:
        spread_pct = (ask - bid) / ask
        if spread_pct <= 0.01:
            return 1.0
        if spread_pct <= 0.03:
            return 0.8
        if spread_pct <= 0.05:
            return 0.6
        if spread_pct <= 0.10:
            return 0.3
        return 0.1

    return 0.5


def _liquidity_score(
    open_interest: Optional[int],
    volume: Optional[int],
    bid: Optional[float],
    ask: Optional[float],
    mark: Optional[float],
) -> float:
    oi = float(open_interest or 0)
    vol = float(volume or 0)

    depth_component = min(oi / 1000.0, 1.0) * 0.5 + min(vol / 500.0, 1.0) * 0.3
    quote_component = 0.2 * _quote_quality_score(bid, ask, mark)

    return min(depth_component + quote_component, 1.0)


def _delta_score(actual_delta: Optional[float], target_delta: float) -> float:
    if actual_delta is None:
        return 0.0
    dist = abs(abs(actual_delta) - abs(target_delta))
    return max(1.0 - dist / 0.25, 0.0)


def _moneyness_proxy_score(
    strike: float,
    underlying_price: Optional[float],
    target_delta: float,
) -> float:
    if underlying_price is None or underlying_price <= 0 or strike <= 0:
        return 0.0

    if target_delta >= 0.35:
        target_strike = underlying_price * 0.95
    elif target_delta >= 0.18:
        target_strike = underlying_price * 0.88
    elif target_delta >= 0.08:
        target_strike = underlying_price * 0.80
    else:
        target_strike = underlying_price * 0.72

    dist_pct = abs(strike - target_strike) / underlying_price
    return max(1.0 - dist_pct / 0.20, 0.0)


def _candidate_score(
    actual_delta: Optional[float],
    target_delta: float,
    strike: float,
    underlying_price: Optional[float],
    open_interest: Optional[int],
    volume: Optional[int],
    bid: Optional[float],
    ask: Optional[float],
    mark: Optional[float],
) -> float:
    quote_quality = _quote_quality_score(bid, ask, mark)
    if quote_quality <= 0:
        return 0.0

    if actual_delta is not None:
        fit_component = _delta_score(actual_delta, target_delta)
    else:
        fit_component = _moneyness_proxy_score(strike, underlying_price, target_delta)

    delta_or_moneyness_component = 0.55 * fit_component
    round_component = 0.20 * _round_strike_score(strike)
    liq_component = 0.15 * _liquidity_score(open_interest, volume, bid, ask, mark)
    quote_component = 0.10 * quote_quality

    return delta_or_moneyness_component + round_component + liq_component + quote_component


def _filter_candidates(
    chain: List[Dict],
    *,
    as_of_date: str,
    option_type: str,
    dte_min: int,
    dte_max: int,
) -> List[Dict]:
    out = []

    for row in chain:
        if str(row.get("option_type", "")).upper() != option_type.upper():
            continue

        expiry = row.get("expiry")
        if not expiry:
            continue

        dte = _safe_date_diff(str(expiry), as_of_date)
        if dte is None or dte < dte_min or dte > dte_max:
            continue

        bid = row.get("bid")
        ask = row.get("ask")
        mark = row.get("mark")

        if not _quote_is_usable(bid, ask, mark):
            continue

        cloned = dict(row)
        cloned["_dte"] = dte
        out.append(cloned)

    return out


def _rank_legs(
    candidates: List[Dict],
    target_delta: float,
    underlying_price: Optional[float],
    top_n: int = TOP_N_LEG_CANDIDATES,
) -> List[Dict]:
    scored: List[Dict] = []

    for row in candidates:
        cloned = dict(row)
        cloned["_score"] = _candidate_score(
            actual_delta=cloned.get("delta"),
            target_delta=target_delta,
            strike=float(cloned.get("strike", 0.0) or 0.0),
            underlying_price=underlying_price,
            open_interest=cloned.get("open_interest"),
            volume=cloned.get("volume"),
            bid=cloned.get("bid"),
            ask=cloned.get("ask"),
            mark=cloned.get("mark"),
        )
        scored.append(cloned)

    scored.sort(key=lambda x: float(x.get("_score", 0.0) or 0.0), reverse=True)
    return scored[:top_n]


def _leg_mark(row: Dict) -> float:
    mark = row.get("mark")
    if mark is not None and float(mark) > 0:
        return float(mark)

    bid = row.get("bid")
    ask = row.get("ask")
    if bid is not None and ask is not None and float(ask) >= float(bid):
        return (float(bid) + float(ask)) / 2.0

    if ask is not None:
        return float(ask)
    if bid is not None:
        return float(bid)
    return 0.0


def _expected_move_pct_from_chain(
    expiry_rows: List[Dict],
    underlying_price: Optional[float],
) -> float:
    """
    Estimate expected move using ATM straddle:
        (ATM call mark + ATM put mark) / spot
    """

    if underlying_price is None or underlying_price <= 0:
        return 0.0

    calls = []
    puts = []

    for row in expiry_rows:
        opt_type = str(row.get("option_type", "")).upper()
        strike = float(row.get("strike", 0.0) or 0.0)

        if strike <= 0:
            continue

        if opt_type == "CALL":
            calls.append(row)

        elif opt_type == "PUT":
            puts.append(row)

    if not calls or not puts:
        return 0.0

    atm_call = min(
        calls,
        key=lambda r: abs(float(r.get("strike", 0.0) or 0.0) - underlying_price),
    )

    atm_put = min(
        puts,
        key=lambda r: abs(float(r.get("strike", 0.0) or 0.0) - underlying_price),
    )

    call_mark = _leg_mark(atm_call)
    put_mark = _leg_mark(atm_put)

    if call_mark <= 0 or put_mark <= 0:
        return 0.0

    expected_move_pct = (call_mark + put_mark) / underlying_price

    return min(expected_move_pct, 1.0)


def _spread_efficiency_score(long_leg: Dict, short_leg: Dict) -> float:
    long_mark = _leg_mark(long_leg)
    short_mark = _leg_mark(short_leg)

    long_strike = float(long_leg.get("strike", 0.0) or 0.0)
    short_strike = float(short_leg.get("strike", 0.0) or 0.0)

    width = max(long_strike - short_strike, 0.0)
    debit = max(long_mark - short_mark, 0.0)

    if width <= 0 or debit <= 0 or debit >= width:
        return 0.0

    max_payoff = width - debit
    return max_payoff / debit


def _spread_quote_score(long_leg: Dict, short_leg: Dict) -> float:
    lq = _quote_quality_score(long_leg.get("bid"), long_leg.get("ask"), long_leg.get("mark"))
    sq = _quote_quality_score(short_leg.get("bid"), short_leg.get("ask"), short_leg.get("mark"))
    return (lq + sq) / 2.0


def _width_score(
    width: float,
    *,
    underlying_price: Optional[float],
    expected_move_pct: float,
    long_target_delta: float,
) -> float:
    if width <= 0 or underlying_price is None or underlying_price <= 0 or expected_move_pct <= 0:
        return 0.0

    expected_move_dollars = underlying_price * expected_move_pct

    if long_target_delta >= 0.30:
        # primary hedge: moderate width
        target_width = expected_move_dollars * 0.45
    else:
        # tail hedge: wider, more convex
        target_width = expected_move_dollars * 0.55

    # keep target width inside a sane range
    target_width = max(min(target_width, 80.0), 20.0)

    dist = abs(width - target_width)

    # tolerance scales with target width
    tolerance = max(target_width * 0.60, 10.0)

    return max(1.0 - dist / tolerance, 0.0)


def _expiry_preference_score(
    dte: int,
    dte_min: int,
    dte_max: int,
    long_target_delta: float,
) -> float:
    if dte < dte_min or dte > dte_max:
        return 0.0

    if long_target_delta >= 0.30:
        target_dte = dte_min + 0.35 * (dte_max - dte_min)
    else:
        target_dte = dte_min + 0.50 * (dte_max - dte_min)

    dist = abs(dte - target_dte)
    half_window = max((dte_max - dte_min) / 2.0, 1.0)
    return max(1.0 - dist / half_window, 0.0)


def _expected_move_alignment_score(
    *,
    long_strike: float,
    short_strike: float,
    underlying_price: Optional[float],
    expected_move_pct: float,
    long_target_delta: float,
) -> float:
    if underlying_price is None or underlying_price <= 0 or expected_move_pct <= 0:
        return 0.5

    long_distance_pct = max((underlying_price - long_strike) / underlying_price, 0.0)
    short_distance_pct = max((underlying_price - short_strike) / underlying_price, 0.0)

    if long_target_delta >= 0.30:
        # primary: live near the correction zone around expected move
        short_target = expected_move_pct * 1.00
        long_target = expected_move_pct * 0.55
    else:
        # tail: farther outside expected move
        short_target = expected_move_pct * 1.80
        long_target = expected_move_pct * 1.15

    short_score = max(
        1.0 - abs(short_distance_pct - short_target) / max(expected_move_pct, 0.01),
        0.0,
    )
    long_score = max(
        1.0 - abs(long_distance_pct - long_target) / max(expected_move_pct, 0.01),
        0.0,
    )

    return (short_score + long_score) / 2.0


def _pair_score(
    long_leg: Dict,
    short_leg: Dict,
    *,
    long_target_delta: float,
    short_target_delta: float,
    dte_min: int,
    dte_max: int,
    underlying_price: Optional[float],
    expected_move_pct: float,
) -> float:
    long_strike = float(long_leg.get("strike", 0.0) or 0.0)
    short_strike = float(short_leg.get("strike", 0.0) or 0.0)

    if long_strike <= short_strike:
        return 0.0

    long_mark = _leg_mark(long_leg)
    short_mark = _leg_mark(short_leg)
    debit = max(long_mark - short_mark, 0.0)
    width = max(long_strike - short_strike, 0.0)

    if debit <= 0 or width <= 0 or debit >= width:
        return 0.0

    dte = int(long_leg.get("_dte", 0) or 0)

    long_leg_score = float(long_leg.get("_score", 0.0) or 0.0)
    short_leg_score = float(short_leg.get("_score", 0.0) or 0.0)
    pair_fit_score = (long_leg_score + short_leg_score) / 2.0

    efficiency_score = _spread_efficiency_score(long_leg, short_leg)
    quote_score = _spread_quote_score(long_leg, short_leg)
    width_pref_score = _width_score(
        width,
        underlying_price=underlying_price,
        expected_move_pct=expected_move_pct,
        long_target_delta=long_target_delta,
    )
    expiry_pref_score = _expiry_preference_score(dte, dte_min, dte_max, long_target_delta)
    expected_move_score = _expected_move_alignment_score(
        long_strike=long_strike,
        short_strike=short_strike,
        underlying_price=underlying_price,
        expected_move_pct=expected_move_pct,
        long_target_delta=long_target_delta,
    )

    return (
        0.28 * pair_fit_score
        + 0.24 * min(efficiency_score / 4.0, 1.0)
        + 0.12 * quote_score
        + 0.10 * width_pref_score
        + 0.10 * expiry_pref_score
        + 0.16 * expected_move_score
    )


def _to_candidate(row: Optional[Dict]) -> OptionContractCandidate | None:
    if not row:
        return None

    return OptionContractCandidate(
        symbol=str(row.get("symbol") or ""),
        underlying=str(row.get("underlying") or "QQQ"),
        expiry=str(row.get("expiry") or ""),
        strike=float(row.get("strike", 0.0) or 0.0),
        option_type=str(row.get("option_type") or ""),
        delta=float(row["delta"]) if row.get("delta") is not None else None,
        bid=float(row["bid"]) if row.get("bid") is not None else None,
        ask=float(row["ask"]) if row.get("ask") is not None else None,
        mark=float(row["mark"]) if row.get("mark") is not None else None,
        open_interest=int(row["open_interest"]) if row.get("open_interest") is not None else None,
        volume=int(row["volume"]) if row.get("volume") is not None else None,
        score=float(row["_score"]) if row.get("_score") is not None else None,
    )


def _pick_best_spread(
    put_chain: List[Dict],
    *,
    as_of_date: str,
    structure_name: str,
    underlying: str,
    underlying_price: Optional[float],
    dte_min: int,
    dte_max: int,
    long_target_delta: float,
    short_target_delta: float,
    call_chain: Optional[List[Dict]] = None,
) -> OptionSpreadSelection:
    """
    Select the best PUT debit spread.

    Important:
    - put_chain is used for actual spread construction
    - call_chain is only used to improve expected-move estimation
    """
    call_chain = call_chain or []

    # PUT-only candidates for actual spread construction
    candidates = _filter_candidates(
        put_chain,
        as_of_date=as_of_date,
        option_type="PUT",
        dte_min=dte_min,
        dte_max=dte_max,
    )

    expiries = sorted({str(row.get("expiry")) for row in candidates if row.get("expiry")})

    best_selection = OptionSpreadSelection(
        structure_name=structure_name,
        underlying=underlying,
        target_dte_min=dte_min,
        target_dte_max=dte_max,
        target_long_delta=long_target_delta,
        target_short_delta=short_target_delta,
        notes=[],
    )

    best_total_score = -1.0

    for expiry in expiries:
        put_expiry_rows = [r for r in candidates if str(r.get("expiry")) == expiry]
        call_expiry_rows = [r for r in call_chain if str(r.get("expiry")) == expiry]

        if not put_expiry_rows:
            continue

        # Build rows for expected move only:
        # puts from put_chain + calls from call_chain
        full_expiry_rows = list(put_expiry_rows) + list(call_expiry_rows)

        expected_move_pct = _expected_move_pct_from_chain(full_expiry_rows, underlying_price)

        long_candidates = _rank_legs(
            put_expiry_rows,
            long_target_delta,
            underlying_price,
            TOP_N_LEG_CANDIDATES,
        )
        short_candidates = _rank_legs(
            put_expiry_rows,
            short_target_delta,
            underlying_price,
            TOP_N_LEG_CANDIDATES,
        )

        print(
            "DEBUG expiry=", expiry,
            "put_rows=", len(put_expiry_rows),
            "call_rows=", len(call_expiry_rows),
            "expected_move_pct=", expected_move_pct,
            "long_candidates=", len(long_candidates),
            "short_candidates=", len(short_candidates),
        )

        for long_leg in long_candidates:
            for short_leg in short_candidates:
                long_strike = float(long_leg.get("strike", 0.0) or 0.0)
                short_strike = float(short_leg.get("strike", 0.0) or 0.0)

                # PUT debit spread: long strike must be above short strike
                if long_strike <= short_strike:
                    continue

                total_score = _pair_score(
                    long_leg,
                    short_leg,
                    long_target_delta=long_target_delta,
                    short_target_delta=short_target_delta,
                    dte_min=dte_min,
                    dte_max=dte_max,
                    underlying_price=underlying_price,
                    expected_move_pct=expected_move_pct,
                )

                if total_score <= 0:
                    print(
                        "DEBUG rejected pair",
                        expiry,
                        long_leg.get("symbol"),
                        short_leg.get("symbol"),
                        "long_strike=", long_strike,
                        "short_strike=", short_strike,
                        "expected_move_pct=", expected_move_pct,
                        "long_mark=", _leg_mark(long_leg),
                        "short_mark=", _leg_mark(short_leg),
                    )
                    continue

                if total_score > best_total_score:
                    dte_val = int(long_leg.get("_dte", 0) or 0)
                    eff_score = _spread_efficiency_score(long_leg, short_leg)
                    quote_score = _spread_quote_score(long_leg, short_leg)
                    width_score = _width_score(
                        max(
                            float(long_leg.get("strike", 0.0) or 0.0)
                            - float(short_leg.get("strike", 0.0) or 0.0),
                            0.0,
                        ),
                        underlying_price=underlying_price,
                        expected_move_pct=expected_move_pct,
                        long_target_delta=long_target_delta,
                    )
                    expiry_score = _expiry_preference_score(
                        dte_val,
                        dte_min,
                        dte_max,
                        long_target_delta,
                    )
                    expected_move_score = _expected_move_alignment_score(
                        long_strike=long_strike,
                        short_strike=short_strike,
                        underlying_price=underlying_price,
                        expected_move_pct=expected_move_pct,
                        long_target_delta=long_target_delta,
                    )
                    long_leg_score = float(long_leg.get("_score", 0.0) or 0.0)
                    short_leg_score = float(short_leg.get("_score", 0.0) or 0.0)

                    best_total_score = total_score
                    best_selection = OptionSpreadSelection(
                        structure_name=structure_name,
                        underlying=underlying,
                        target_dte_min=dte_min,
                        target_dte_max=dte_max,
                        target_long_delta=long_target_delta,
                        target_short_delta=short_target_delta,
                        selected_expiry=expiry,
                        long_leg=_to_candidate(long_leg),
                        short_leg=_to_candidate(short_leg),
                        selection_score=total_score,
                        notes=[
                            f"pair_score={total_score:.4f}",
                            f"long_leg_score={long_leg_score:.4f}",
                            f"short_leg_score={short_leg_score:.4f}",
                            f"spread_efficiency={eff_score:.4f}",
                            f"quote_score={quote_score:.4f}",
                            f"width_score={width_score:.4f}",
                            f"expiry_pref={expiry_score:.4f}",
                            f"expected_move_pct={expected_move_pct:.4f}",
                            f"expected_move_align={expected_move_score:.4f}",
                        ],
                    )

    if best_selection.selected_expiry is None:
        best_selection.notes.append("No suitable spread found in target DTE/delta/expected-move range.")

    return best_selection


def select_hedge_spreads(
    *,
    as_of_date: str,
    underlying: str = "QQQ",
    market_regime: MarketRegimeType,
    hedge_style: HedgeStyleType,
    underlying_price: float | None = None,
) -> HedgeSpreadSelectionResponse:
    asof = date.fromisoformat(as_of_date)
    if underlying_price is None and underlying == "QQQ":
        underlying_price = get_latest_price(underlying)

    combined_dte_min = PRIMARY_DTE_RANGE[0]
    combined_dte_max = TAIL_DTE_RANGE[1]

    put_chain = get_live_option_chain(
        underlying=underlying,
        expiry_gte=(asof + timedelta(days=combined_dte_min)).isoformat(),
        expiry_lte=(asof + timedelta(days=combined_dte_max)).isoformat(),
        option_type="PUT",
    )

    call_chain = get_live_option_chain(
        underlying=underlying,
        expiry_gte=(asof + timedelta(days=combined_dte_min)).isoformat(),
        expiry_lte=(asof + timedelta(days=combined_dte_max)).isoformat(),
        option_type="CALL",
    )

    primary = _pick_best_spread(
        put_chain,
        as_of_date=as_of_date,
        structure_name=f"{underlying} primary put spread",
        underlying=underlying,
        underlying_price=underlying_price,
        dte_min=PRIMARY_DTE_RANGE[0],
        dte_max=PRIMARY_DTE_RANGE[1],
        long_target_delta=0.40,
        short_target_delta=0.20,
        call_chain=call_chain,
    )

    tail = _pick_best_spread(
        put_chain,
        as_of_date=as_of_date,
        structure_name=f"{underlying} tail put spread",
        underlying=underlying,
        underlying_price=underlying_price,
        dte_min=TAIL_DTE_RANGE[0],
        dte_max=TAIL_DTE_RANGE[1],
        long_target_delta=0.10,
        short_target_delta=0.05,
        call_chain=call_chain,
    )

    return HedgeSpreadSelectionResponse(
        as_of_date=as_of_date,
        underlying=underlying,
        market_regime=market_regime,
        hedge_style=hedge_style,
        primary_spread=primary,
        tail_spread=tail,
    )