#!/bin/bash
# Seed 30-day hedge history snapshots
# Run this once — idempotent (safe to run again, won't duplicate)
# Each call uses today's portfolio composition but correct market signals for that date
# Takes ~2 min to complete (20 weekdays × 2s each)

echo "Seeding hedge history — 20 weekdays (Feb 16 to Mar 15)..."
echo ""

for date in \
  2026-02-16 2026-02-17 2026-02-18 2026-02-19 2026-02-20 \
  2026-02-23 2026-02-24 2026-02-25 2026-02-26 2026-02-27 \
  2026-03-02 2026-03-03 2026-03-04 2026-03-05 2026-03-06 \
  2026-03-09 2026-03-10 2026-03-11 2026-03-12 2026-03-13
do
  result=$(curl -s -X POST "http://localhost:8000/api/hedge/history/snapshot?account_id=all&target_date=$date")
  success=$(echo "$result" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('success','?'))" 2>/dev/null)
  regime=$(echo "$result" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('market_regime','?'))" 2>/dev/null)
  hedge=$(echo "$result" | python3 -c "import sys,json; d=json.load(sys.stdin); v=d.get('current_hedge_pct',0); print(f'{v*100:.1f}%')" 2>/dev/null)
  echo "$date  success=$success  regime=$regime  hedge=$hedge"
  sleep 2
done

echo ""
echo "Done. Reload the dashboard — the 30-day chart should now render."
