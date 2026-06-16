---
name: Bug report
about: Report a bug in the trading platform or dashboard
title: '[BUG] '
labels: bug
assignees: ''

---

> **Safety reminder:** If real capital is at risk right now, use the kill-switch first (`POST /api/killswitch` or write the file `run/killswitch`) and deal with the issue before filing a bug report.

## Description

A clear and concise description of what the bug is.

## Trading context

- **Trading mode:** PAPER / LIVE
- **`PAPER_TRADING` flag in `.env`:** `true` / `false`
- **Real capital at risk right now:** Yes / No
- **Kill-switch already activated:** Yes / No / N/A

## Component

Which part of the platform is affected?

- [ ] `dhan-trader` (order flow / strategy / feed)
- [ ] `dhan-api` (dashboard / REST endpoints)
- [ ] Kronos gate / calibration
- [ ] Backfill
- [ ] Database / schema
- [ ] Dashboard (React frontend)
- [ ] Other: ___

## Steps to reproduce

1. ...
2. ...
3. ...

## Expected behaviour

What you expected to happen.

## Actual behaviour

What actually happened.

## Relevant log excerpt

Paste from `/var/log/dhan/trader.log` or `/var/log/dhan/api.log`. Include the timestamp range around the event.

```
# paste log lines here
```

## Heartbeat state

If the trader is running, paste the output of:
```bash
cat /opt/dhan-trading/run/trader_heartbeat.json | python3 -m json.tool
```

```json
# paste heartbeat here
```

## Additional context

Any other relevant information (DB state, recent deployments, config changes, etc.).
