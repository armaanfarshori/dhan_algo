---
name: position_reconcile
description: Compare open positions in the TimescaleDB positions table against live Dhan API positions. Alert on any mismatch — catches ghost positions from network failures during order placement.
version: 0.1.0
author: DhanAIBot
platforms: [linux]
metadata:
  hermes:
    tags: [Risk, Positions, Reconciliation, Audit]
    category: dhan-trading
---

# Position Reconcile Skill

Queries both the `positions` hypertable (latest snapshot) and the live Dhan API, then compares qty and avg_price per security. Any mismatch is flagged as a reconciliation break.

## When to Use

- After any order placement to verify DB reflects reality
- Morning pre-market (after overnight positions settle)
- If `position_reconcile` discrepancy alert fires
- User asks "are my positions correct?" or "reconcile positions"

## How to Run

```bash
cd /opt/dhan-trading && set -a && source .env && set +a
python3 ~/.hermes/skills/dhan/position_reconcile/scripts/reconcile.py
```

## Output

```
=== Position Reconcile ===
DB snapshot: 2 open positions (last updated 14:32 IST)
Dhan live:   2 open positions

RELIANCE (2885): DB qty=1  Dhan qty=1  avg ₹1,340.50 ✅
HDFCBANK (1333): DB qty=0  Dhan qty=1  avg ₹750.20   ❌ MISMATCH

1 reconciliation break — manual review required.
```
