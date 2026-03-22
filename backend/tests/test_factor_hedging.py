from app.services.factor_exposure_engine import (
    compute_factor_exposures,
    allocate_factor_hedge_budget,
)
from app.services.hedge_budget_allocator import allocate_structure_budgets


def make_pos(symbol: str, market_value: float):
    return {"symbol": symbol, "market_value": market_value}


def test_tech_only_portfolio():
    positions = [
        make_pos("QQQ", 300000),
        make_pos("NVDA", 200000),
        make_pos("AAPL", 100000),
    ]
    portfolio_value = 600000

    factor_rows = compute_factor_exposures(
        positions=positions,
        portfolio_value=portfolio_value,
    )

    assert any(r.factor == "tech" for r in factor_rows)
    assert not any(r.factor == "btc" for r in factor_rows)
    assert not any(r.factor == "gold" for r in factor_rows)

    budgets = allocate_factor_hedge_budget(
        factor_rows=factor_rows,
        total_budget_dollars=10000,
        regime="neutral",
    )

    assert len(budgets) >= 1
    assert all(b["factor"] != "btc" for b in budgets)
    assert all(b["factor"] != "gold" for b in budgets)

    structure_budgets = allocate_structure_budgets(
        factor_budget_allocations=budgets,
        regime="neutral",
    )

    assert len(structure_budgets) >= 1
    for row in structure_budgets:
        total = sum(row["structure_budgets"].values())
        assert abs(total - row["allocated_budget_dollars"]) < 0.01


def test_tech_plus_btc_portfolio():
    positions = [
        make_pos("QQQ", 300000),
        make_pos("NVDA", 150000),
        make_pos("IBIT", 100000),
        make_pos("WULF", 50000),
    ]
    portfolio_value = 600000

    factor_rows = compute_factor_exposures(
        positions=positions,
        portfolio_value=portfolio_value,
    )

    assert any(r.factor == "tech" for r in factor_rows)
    assert any(r.factor == "btc" for r in factor_rows)

    budgets = allocate_factor_hedge_budget(
        factor_rows=factor_rows,
        total_budget_dollars=12000,
        regime="neutral",
    )

    factors = {b["factor"] for b in budgets}
    assert "tech" in factors
    assert "btc" in factors

    structure_budgets = allocate_structure_budgets(
        factor_budget_allocations=budgets,
        regime="neutral",
    )

    btc_row = next(r for r in structure_budgets if r["factor"] == "btc")
    tech_row = next(r for r in structure_budgets if r["factor"] == "tech")

    btc_convex_weight = btc_row["structure_weights"]["convex"]
    tech_convex_weight = tech_row["structure_weights"]["convex"]

    assert btc_convex_weight >= tech_convex_weight


def test_tech_plus_gold_portfolio():
    positions = [
        make_pos("QQQ", 350000),
        make_pos("AAPL", 100000),
        make_pos("GLD", 150000),
    ]
    portfolio_value = 600000

    factor_rows = compute_factor_exposures(
        positions=positions,
        portfolio_value=portfolio_value,
    )

    assert any(r.factor == "tech" for r in factor_rows)
    assert any(r.factor == "gold" for r in factor_rows)
    assert not any(r.factor == "btc" for r in factor_rows)

    budgets = allocate_factor_hedge_budget(
        factor_rows=factor_rows,
        total_budget_dollars=9000,
        regime="early_breakdown",
    )

    factors = {b["factor"] for b in budgets}
    assert "tech" in factors
    assert "gold" in factors
    assert "btc" not in factors