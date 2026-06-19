# Contributing to DhanAIBot

This is a live trading platform (currently in paper mode). Contributions carry real financial risk when the platform goes live — please read these rules before sending a PR.

## Dev environment

Clone the repo and work locally on the Mac. The live platform runs on AWS and is not touched by local development. All dev runs happen in `PAPER_TRADING=true` mode (the default). Do not attempt to run order-placement code locally — Dhan's order API is only whitelisted to the agent's Elastic IP.

```bash
pip install -r requirements.txt
pytest -q          # must be green before opening a PR
```

Real infrastructure values (IPs, account IDs, tokens) live in `~/Desktop/dhan_aws_access/` outside this repo and are never committed here.

## Safety rules — non-negotiable

1. **Never commit with `PAPER_TRADING=false`.** That flag is a live-trading gate. Any PR that sets it to `false` in any config, `.env`, or test fixture will be rejected.
2. **RiskEngine owns the kill-switch.** Do not add order-placement calls that bypass `RiskEngine`. All orders must route through it.
3. **No live trading before the 2-year backtest passes.** Do not change the platform to live mode or propose doing so before M3 is complete.
4. **Kronos is fail-open.** Model errors must never block trades. Do not make Kronos gate failures fatal.
5. **EOD square-off is unconditional.** Never add a dependency on strategy state to the EOD square-off path.
6. **No static watchlists.** The screener + `instruments` table validation is the only allowed source of candidates.

## What "mode-blind" means

Changes to the execution path (`engine/execution.py`, `engine/portfolio.py`, `engine/risk.py`, `strategies/`) must behave identically in paper and live modes. The only difference between modes is the executor class (`PaperExecutor` vs `LiveExecutor`). Do not add `if cfg.paper_trading:` branches in shared logic.

## PR checklist

Before opening a pull request:

- [ ] `pytest -q` passes locally (71 tests; CI runs the same suite)
- [ ] No secrets, tokens, real IPs, or account IDs anywhere in the diff (private repo — never rely on that)
- [ ] No `PAPER_TRADING=false` anywhere in the diff
- [ ] Changes to the execution/risk path are mode-blind
- [ ] New env vars added to `config.py` (the single env reader) with safe defaults
- [ ] If you touched `apps/api.py` or `apps/trader.py`, update `docs/API-Reference.md` or `docs/Architecture.md` accordingly

## Branch naming

```
feat/<short-description>     # new capability
fix/<short-description>      # bug fix
docs/<short-description>     # docs only
test/<short-description>     # tests only
refactor/<short-description> # no behaviour change
```

Target `main`. Squash-merge preferred for small changes; merge commit for multi-commit features.

## Deploying changes

This repo is the Mac working copy. To deploy to AWS:

```bash
git push                                        # push from Mac
# then on the agent EC2:
sudo git pull
sudo systemctl restart dhan-trader              # if engine code changed
# rebuild dashboard if frontend changed:
cd dashboard && npm run build
sudo systemctl restart dhan-api
```

See `docs/Operations-Runbook.md` for the full ops workflow and SSH helper scripts.
