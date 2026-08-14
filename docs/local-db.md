# Local TimescaleDB — operator runbook

The platform used to talk to a dedicated AWS DB instance (bare-metal PostgreSQL 16 +
TimescaleDB on its own EC2 box). That box is gone. Everything — engine, dashboard,
database — now runs on the single home server, and the database is a Docker container
defined by `docker-compose.yml` at the repo root.

The container is a **blank slate**: no AWS snapshot was restored, so the schema is
built from zero by Alembic and every table starts empty. That is expected; the paper
engine is designed to run with no history (it logs warnings and selects nothing).

---

## Prerequisites

- Docker Engine + the Compose v2 plugin (`docker compose`, not the old `docker-compose`).
- The Python venv, with requirements installed:

  ```bash
  .venv/bin/python -m pip install -r requirements.txt
  ```

  This is what provides the `alembic` CLI and `psycopg2`. If `.venv/bin/alembic`
  does not exist, the install has not been run — see *Gotchas* below.

- Optionally a repo-root `.env` (gitignored). Compose reads it automatically and
  interpolates `DB_NAME` / `DB_USER` / `DB_PASSWORD` from it. With no `.env` at all
  the compose defaults (`dhan_trading` / `trader` / `trader123`) apply, and those
  match the defaults in `config.py`, so a fresh clone works unconfigured.

---

## 1. Start the database

```bash
docker compose up -d timescaledb
```

## 2. Wait for it to report healthy

First boot runs `initdb` plus the TimescaleDB bootstrap, so it takes appreciably
longer than later starts. Do not run migrations until the status column says
`(healthy)` — Postgres briefly accepts connections on an internal socket during
init, so "the port is open" is not the same as "ready".

```bash
docker compose ps                      # STATUS should read "Up (healthy)"

# or block until healthy:
until [ "$(docker inspect -f '{{.State.Health.Status}}' dhan-timescaledb)" = healthy ]; do
  sleep 2
done
```

## 3. Bootstrap the schema

`alembic/env.py` builds its connection URL from **environment variables**
(`DB_USER`, `DB_PASSWORD`, `DB_HOST`, `DB_PORT`, `DB_NAME`, or a single
`DATABASE_URL`). It does *not* read `.env` on its own — pydantic's `.env` loading
is a `config.py` feature, and Alembic never touches `config.py`. So export the
file first:

```bash
set -a && source .env && set +a          # skip if you are using the defaults
.venv/bin/alembic upgrade head
```

Run this from the **repo root** — `alembic.ini` lives there and points
`script_location` at `alembic/`.

Migrations 001 → 014 form a single linear chain with one head. 001 issues
`CREATE EXTENSION IF NOT EXISTS timescaledb CASCADE` before anything else, so a
virgin container needs no manual preparation. Later migrations create the
hypertables (`ticks`, `bars`, `positions`, `equity_curve`, `futures_bars`,
`option_atm_iv`, `index_bars`, `option_chain_snapshot`) and attach the
compression/retention policies.

## 4. Verify

```bash
# schema version — expect: 014
.venv/bin/alembic current

# extension present, and at the pinned version
docker compose exec timescaledb psql -U trader -d dhan_trading \
  -c "SELECT extname, extversion FROM pg_extension WHERE extname='timescaledb';"

# hypertables registered
docker compose exec timescaledb psql -U trader -d dhan_trading \
  -c "SELECT hypertable_name FROM timescaledb_information.hypertables ORDER BY 1;"

# tables exist and are empty — this is the expected post-bootstrap state
docker compose exec timescaledb psql -U trader -d dhan_trading -c "\dt"
```

Never `COUNT(*)` or `ORDER BY time LIMIT 1` on `bars` once it holds real data —
see the constraint in `CLAUDE.md`. On an empty database it is harmless, but do
not build the habit.

---

## Critical: set `DB_HOST=127.0.0.1`, not `localhost`

This is the sharpest edge in the AWS → local move, and it fails **silently**.

`core/journal.py:56` decides whether to journal at all:

```python
self._enabled = get_config().db_host not in ("", "localhost")
```

On AWS, `DB_HOST` was the database EC2's private IP, and `localhost` could only
mean "a dev box with no database" — so treating it as "journalling off" was
correct. On this machine the database genuinely *is* on localhost, so that same
value now disables journalling on a working install.

With `DB_HOST=localhost` the trader still boots, still paper-trades, and still
writes `bars`, `engine_positions`, `daily_screen` and `api_usage` (those go
through `db.get_session()` directly). But **`runs`, `signals`, `trades`,
`orders`, `fills` and `equity_curve` are never written** — every
`AsyncDBBackend` call short-circuits at `core/journal.py:107`. You get a
half-journalled system with no trade history, and because
`RiskEngine.refresh_pnl()` reads `trades`, the realized-P&L and daily-loss
meters stay pinned at zero.

`127.0.0.1` connects to exactly the same container (the port publishes on
`127.0.0.1:5432`) and passes the check. Use it in `.env`:

```
DB_HOST=127.0.0.1
```

Note `.env.example:19` still ships `DB_HOST=localhost`, inherited from the AWS
layout. Verify journalling is actually live after the first paper session:

```bash
docker compose exec timescaledb psql -U trader -d dhan_trading \
  -c "SELECT count(*) FROM runs;"     # must be > 0 after the trader has run
```

A `0` here with a trader that has been running means `DB_HOST` is still
`localhost`.

---

## Where the data lives

A named Docker volume, pinned to exactly `dhan_pgdata` (not
`<project>_dhan_pgdata`) so the name is stable no matter which directory or git
worktree Compose is invoked from:

```bash
docker volume inspect dhan_pgdata      # .Mountpoint is the host path
```

By default that is under `/var/lib/docker/volumes/dhan_pgdata/_data`, i.e. on the
**256GB system SSD**. Disk is the binding constraint on this box:

- Do not re-run the full NSE_EQ backfill locally. That dataset was ~300M rows on
  AWS and will not fit alongside the OS.
- Do not enable WAL archiving or raise `wal_keep_size`. There is no standby, and
  retained WAL is the fastest way to fill the disk.
- The compression and retention policies attached in migration 002 are what keep
  `ticks` and `bars` bounded. Do not drop them to speed up ingestion.

Watch it with `docker system df -v` and `df -h /`.

---

## Wipe and rebuild from zero

The container holds no irreplaceable state today (no AWS restore, paper trading
only), so a rebuild is cheap. **This destroys all data**:

```bash
docker compose down -v                 # -v also removes the dhan_pgdata volume
docker compose up -d timescaledb
# wait for healthy (step 2), then:
set -a && source .env && set +a
.venv/bin/alembic upgrade head
```

To stop without destroying anything, omit `-v`:

```bash
docker compose down                    # volume survives
docker compose up -d timescaledb       # comes back with data intact
```

Take a logical backup before anything risky:

```bash
docker compose exec timescaledb pg_dump -U trader -Fc dhan_trading > dhan_trading.dump
```

---

## Why the version is pinned

`docker-compose.yml` pins `timescale/timescaledb:2.17.2-pg16`. This is the tag
`CLAUDE.md` records as the repo convention, and it is not cosmetic:

- **TimescaleDB 2.18+ renames the compression DDL.** Migrations 002, 009 and 010
  use `ALTER TABLE ... SET (timescaledb.compress, ...)` plus
  `add_compression_policy` / `add_retention_policy`. 2.18 moved that surface to
  `timescaledb.enable_columnstore`; floating the tag risks a bootstrap that fails
  partway through the chain.
- **It must be the community (TSL) build,** which this tag is. The `-apache`
  variant omits compression and retention policies entirely, so migration 002
  would fail.
- **The `-pg16` half pins the PostgreSQL major.** Changing it makes the existing
  `dhan_pgdata` volume unreadable — a major-version bump needs a dump/restore, not
  a tag edit.

---

## Access surface

The port mapping is `127.0.0.1:5432:5432` — **loopback only, deliberately**. This
host is on a LAN and a tailnet; binding `0.0.0.0` would publish Postgres to both,
and the default password is `trader123`. The engine, dashboard and Alembic all run
on this machine, so loopback covers every real caller. If remote access is ever
genuinely needed, tunnel it over SSH rather than widening the binding.

`config.py` warns when `db_password` is still `trader123` and `paper_trading` is
false, so change the password before any live use.

---

## Gotchas

**`.venv/bin/alembic` missing.** The CLI ships with the `alembic` package; if the
venv predates `requirements.txt` it may not be installed. Install it, then check:

```bash
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/alembic --version
```

**Do not run `python -m alembic` or `import alembic` from the repo root.** The
repo has its own `alembic/` directory, which Python treats as a namespace package
and resolves *before* site-packages when the current directory is on `sys.path`.
The result is a confusing `No module named alembic.config`. Always invoke the
console script (`.venv/bin/alembic`), whose `sys.path[0]` is `.venv/bin` and which
therefore resolves the real package. (`alembic.ini` sets `prepend_sys_path = .`
afterwards, which is what lets `env.py` import repo modules — that ordering is
fine.)

**`alembic.ini` carries a `sqlalchemy.url` line.** It is ignored: `env.py` builds
the engine itself from `_db_url()` to dodge configparser's `%` interpolation. The
credentials there are the same non-secret defaults as `config.py`.

**Connecting from inside the container** uses `-U trader` against the local
socket. Connecting from the host uses `127.0.0.1:5432` with the password.
