# Local Migration Handoff — AWS → Home Server (T1700)

> **Executed 2026-08-14.** This handoff was the planning brief written before the migration; the
> work it describes is done (PRs #100–#105). It is kept as-is for the historical record of what
> was asked for and why — the only edit is the box's LAN address, redacted per the repo's
> never-commit-real-IPs rule. For what actually shipped, the decisions made along the way, and the
> verification evidence, see **`docs/migration-2026-08.md`**. For day-to-day operation of the
> result, see `docs/local-setup.md`, `docs/local-db.md`, and `docs/local-db-backups.md`.

**Date:** 2026-08-14
**From:** Research/planning session (Claude chat)
**For:** Claude Code on the T1700 (trusted machine — executes, tests, deploys)
**Status:** AWS EC2 instances TERMINATED. Salvage of S3/SSM/EBS-snapshots pending.

## Read first
- Read CLAUDE.md in full — source of truth, hard rules unchanged:
  PAPER_TRADING=true stays true; feature branches only, never commit to main;
  no live/infra actions during market hours (09:15–15:30 IST);
  never hardcode real IPs / account IDs / tokens.
- This is an ENVIRONMENT port. The trading engine, RiskEngine, safety
  invariants (NFR-01..06), and rate limiting do not change.

## Context
- AWS bill (~₹25k) forced migration to owned hardware.
- New host: Dell Precision T1700 — Xeon E3-1270 v3 (4c/8t, AVX2), 32GB DDR3,
  256GB SSD (SMALL — watch disk), Quadro K600 (ignore, CPU-only box),
  Ubuntu kernel 6.14, LAN IP `<redacted>` (get a DHCP reservation for it).
- Mac is gone (company-issued, laid off). This box is editor + executor now.
- EC2 terminated → old .env, dhan_token.json, and the DB EBS volume are gone.
  S3 bucket, SSM parameters, and EBS snapshots MAY survive — verify before
  assuming a restore path exists.

## Task: branch `feat/local-host-support`
Principle: only the environment seam changes. Four workstreams:

1. **Secrets** — replace SSM-sourced .env with hand-authored .env from a
   committed `.env.example` (mode 600). Add SECRETS_BACKEND=env|ssm toggle
   so both worlds work (clone-and-run users won't have AWS).
2. **Database** — Docker Compose service pinning
   timescale/timescaledb:2.17.2-pg16. Fresh empty DB must bootstrap via
   Alembic cleanly. Paper path must degrade gracefully with zero history.
3. **Bootstrap** — `setup_local.sh` sibling to `setup_agent.sh`: venv
   (Python 3.12), Docker, systemd units, cron, logrotate. No SSM, no EC2
   user-data assumptions.
4. **Backups** — nightly pg_dump to local path + optional S3 push if
   credentials present (replaces EBS/DLM snapshots).

## Sequencing
0. Baseline first: ruff clean + pytest -q on fresh clone. Report failures
   before changing anything.
1. AWS salvage (operator task, in progress): S3 listing, SSM parameter
   export, EBS snapshot check. DB restore scope decided AFTER this.
2. Do NOT backfill the full NSE_EQ universe (300M rows won't respect a
   256GB shared SSD). Backfill scope = decision gate: F&O minimum set
   (NIFTY futures 1d, India VIX, ATM IV) + possibly NIFTY-constituent
   equity subset. Propose sizes before pulling.
3. PRs small and single-purpose, per repo convention.

## Open questions (resolve with operator, don't assume)
- Did S3 backups / SSM params / EBS snapshots survive termination?
- Is the Dhan TOTP secret recoverable from SSM, or does Dhan-side TOTP
  need re-enrollment?
- ECC check: E3-1270 v3 supports ECC — are the DIMMs ECC UDIMMs? Run
  memtest86+ before trusting the DB on this box (DIMM4 is a mismatched
  replacement stick).
