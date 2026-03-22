from __future__ import annotations

from typing import Any

from app.services.hedge_config import (
    STRUCTURE_ALLOCATION_BY_REGIME,
    FACTOR_STRUCTURE_MULTIPLIERS,
)


def allocate_structure_budgets(
    *,
    factor_budget_allocations: list[dict[str, Any]],
    regime: str,
) -> list[dict[str, Any]]:
    """
    Split each factor hedge budget into primary / tail / convex sleeves.

    Flow:
      total hedge premium budget
      -> factor hedge budgets
      -> structure budgets within each factor

    Output rows preserve the original factor allocation fields and add:
      structure_budgets = {
          "primary": ...,
          "tail": ...,
          "convex": ...,
      }
    """
    regime_mix = STRUCTURE_ALLOCATION_BY_REGIME.get(
        regime,
        {"primary": 0.60, "tail": 0.30, "convex": 0.10},
    )

    out: list[dict[str, Any]] = []

    for row in factor_budget_allocations:
        factor = str(row.get("factor", "tech"))
        factor_budget = float(row.get("allocated_budget_dollars", 0.0) or 0.0)

        if factor_budget <= 0:
            continue

        factor_mults = FACTOR_STRUCTURE_MULTIPLIERS.get(
            factor,
            {"primary": 1.0, "tail": 1.0, "convex": 1.0},
        )

        raw_primary = regime_mix["primary"] * float(factor_mults.get("primary", 1.0))
        raw_tail = regime_mix["tail"] * float(factor_mults.get("tail", 1.0))
        raw_convex = regime_mix["convex"] * float(factor_mults.get("convex", 1.0))

        total_raw = raw_primary + raw_tail + raw_convex
        if total_raw <= 0:
            continue

        primary_weight = raw_primary / total_raw
        tail_weight = raw_tail / total_raw
        convex_weight = raw_convex / total_raw

        enriched = dict(row)
        enriched["structure_budgets"] = {
            "primary": factor_budget * primary_weight,
            "tail": factor_budget * tail_weight,
            "convex": factor_budget * convex_weight,
        }
        enriched["structure_weights"] = {
            "primary": primary_weight,
            "tail": tail_weight,
            "convex": convex_weight,
        }

        out.append(enriched)

    return out