# Migration 2026-08 — AWS → single-host T1700

**Date:** 2026-08-14
**PRs:** #100 (baseline repair), #101 (egress proxy), #102 (journal enable flag), #103 (DB
backups), #104 (local DB + Docker Compose), #105 (bootstrap script)
**Status:** Shipped and merged to `main`. Executed and verified on the T1700 box the same day.
**Related:** `docs/local-migration-handoff.md` (the planning brief this executed against),
`docs/local-setup.md`, `docs/local-db.md`, `docs/local-db-backups.md`, `CLAUDE.md`.

This is the decision record for the AWS→home-server port: one section per decision, in the order
they were made, each with the context that forced it, what was decided, and the consequences —
including the places the original plan (`docs/local-migration-handoff.md`) changed once real
constraints showed up.

---

## Why this happened

The AWS bill (~₹25k/month for two EC2 instances plus an EBS volume, running a platform in
`PAPER_TRADING` mode with no live capital deployed and — per the 2026-06-21 research conclusion —
no validated trading edge) stopped being justifiable. The operator owns a Dell Precision T1700
(Xeon E3-1270 v3, 4c/8t, 32GB DDR3, 256GB SSD) sitting idle. The migration is a pure environment
port: the trading engine, `RiskEngine`, the safety invariants, and the rate limiter do not change.
Only the seam between the platform and its infrastructure moves.

---

## Decision 1 — `/opt/dhan-trading` becomes a symlink, not a second clone (#105)

**Context.** The AWS bootstrap (`setup_agent.sh`) ran as EC2 user-data on a blank instance: it
cloned the repo fresh into `/opt/dhan-trading`. That path is hardcoded in the systemd units, in
`config.py` defaults (`BACKFILL_CHECKPOINT_PATH`), in the cron lines, and across the docs. On a
single-host deploy there is exactly one checkout — the one you edit and run tests in — so a second
clone at `/opt/dhan-trading` would immediately diverge from it on the first uncommitted edit or
unmerged branch.

**Decision.** `setup_local.sh` step 3 makes `/opt/dhan-trading` a **symlink** to the repo root it
was invoked from. Every hardcoded reference to that path resolves correctly with zero rewriting.

**Consequences.**
- There is no second `git pull` on a different machine anymore — deploying a merged change is
  `git pull` (if needed) plus a service restart, in the same checkout. See `CLAUDE.md`'s *Dev
  workflow*.
- The script refuses to run from a git worktree (warns and continues only if you insist) — a
  worktree gets deleted independently of the primary checkout, and `/opt/dhan-trading` would then
  point at a tree that can disappear out from under a running service.
- The two hard failure modes are both here: something *other* than a symlink already at
  `/opt/dhan-trading` (an old-style clone), or a symlink pointing somewhere else. Both **abort**
  rather than silently deleting what they find — see `docs/local-setup.md` → *Gotchas*.

---

## Decision 2 — `.env` hand-authored from `.env.example`; the planned `SECRETS_BACKEND` flag was dropped (#105)

**Context.** The original planning brief (`docs/local-migration-handoff.md`) specced a
`SECRETS_BACKEND=env|ssm` toggle, so "both worlds work" — a clone-and-run contributor without AWS
access, and a hypothetical future where SSM Parameter Store still supplied secrets on some box.

**Decision.** No such flag was built. `setup_local.sh` seeds `.env` from `.env.example` and
`chmod 600`s it; `config.py` has never read anything but `.env` via `pydantic-settings`
(`env_file=".env"`). There was never a code path that read SSM directly — `setup_agent.sh` fetched
SSM parameters and **wrote them into a plain `.env` file** on the AWS agent, which `config.py` then
read exactly the same way it does here. SSM was a *provisioning-time* source for `.env`'s contents,
never a runtime secrets backend the application itself branched on. A `SECRETS_BACKEND` flag would
have added a conditional with only one real arm (`env`) and a second arm (`ssm`) that nothing in
this repo would ever exercise again, now that the AWS agent that could have populated it is
terminated. Adding it would have been complexity with no corresponding behavior — dead configuration
is worse than no configuration, because it looks like a decision someone made on purpose.

**Consequences.** `.env` is the only secrets source, everywhere, unconditionally. If a future
secrets manager is ever wanted, it should be scoped as its own decision when there's a second real
backend to abstract over — not spec'd preemptively against a backend (SSM) that no longer has
anything to connect to.

---

## Decision 3 — Egress proxy design: category-scoped, with an inert-config repair (#101)

**Context.** Dhan whitelists exactly one static public IP for order-placement REST calls. AWS gave
that via an Elastic IP. A home line has no stable egress (CGNAT), so orders would be rejected. The
fix is an HTTP proxy on a small always-on VM whose IP *is* the whitelisted one, reached over
Tailscale — but routing *everything* through a free-tier VM would burn its bandwidth on the
comparatively enormous data/quote/historical/WebSocket traffic for zero benefit, since only order
placement is IP-gated.

**Decision.** `DHAN_PROXY_URL` (empty = fully direct, back-compat default) plus
`DHAN_PROXY_CATEGORIES` (default `"orders"`; `"all"` is the escape hatch for a box whose entire
Dhan egress must be pinned) — `core/client.py` only attaches `proxy=` to requests in a category the
set contains; `dhan_proxy_categories_set` lower-cases and dedupes. The WebSocket feed is untouched
by this — it never goes through `core/client.py`'s REST path.

**The inert-proxy trap, and how it's closed.** A proxy URL set with an *empty* category list (blank
value, or a value that parses to nothing — whitespace, `",,"`) would be silently inert: every
request, order placement included, would egress from this box's own un-whitelisted IP while the
`.env` looked fully configured. `Config.dhan_proxy_categories_set` repairs this case by falling back
to `{"orders"}` whenever a URL is set but the parsed category set is empty, and a
`model_validator` (`_warn_inert_proxy_categories`) logs a `WARNING` once at startup so the operator
still notices and fixes the `.env` rather than silently relying on the repair forever. The repair
exists because "routes nothing" should never be spelled by blanking the categories — that's what
leaving `DHAN_PROXY_URL` empty is for.

**Verification, before-the-open.** A dead or re-IP'd proxy does not raise loudly — aiohttp does not
fall back to a direct connection, so the platform looks healthy right up to the first rejected
order. `scripts/egress_check.py` (cron `0 9 * * 1-5` IST) fetches the caller's public IP through the
configured proxy and compares it against `DHAN_WHITELISTED_EGRESS_IP`, exiting 0 (match, or nothing
configured — skip), 1 (wrong IP — orders will be rejected), or 2 (proxy unreachable — orders can't
be placed), alerting via Telegram on 1 or 2. The Telegram message deliberately omits the proxy
host/port from exception text (type name only) so a transport error never pushes the egress VM's
address out over an external channel.

**Consequences.** `tests/test_egress_proxy.py` pins three things independently so they can't drift
apart silently: which rate categories actually carry the `proxy=` kwarg in `core/client.py`, how
`DHAN_PROXY_CATEGORIES` parses in `config.py`, and `egress_check.py`'s exit code per failure mode.

---

## Decision 4 — `JOURNAL_DB_ENABLED` replaces the `DB_HOST == localhost` heuristic (#102)

**Context.** `core/journal.py`'s `AsyncDBBackend` previously inferred whether to journal
(`runs`/`signals`/`trades`/`orders`/`fills`/`equity_curve`) from `db_host not in ("", "localhost")`.
On AWS that was correct: `DB_HOST` was the DB EC2's private IP, and `localhost` could only mean "a
dev box with no real database," so disabling journalling there was the right default. On this box,
**`localhost` is exactly where the production TimescaleDB container listens.** The heuristic would
have silently disabled the trade journal on a fully working install: the trader would still boot,
paper-trade, and write `bars`/`engine_positions` (those go through `db.get_session()` directly,
bypassing the journal), but every `AsyncDBBackend` call — including the ones `RiskEngine.refresh_pnl()`
depends on, which reads `trades` — would short-circuit, pinning realized P&L and the daily-loss
meters at zero forever. A risk engine that can't see its own losses is a silent, dangerous failure
mode, not a cosmetic one.

**Decision.** An explicit `JOURNAL_DB_ENABLED: bool = True` config flag replaces the inference.
`AsyncDBBackend.__init__` now sets `self._enabled = cfg.journal_db_enabled and cfg.db_host != ""` —
an empty `db_host` still disables (there's nothing to connect to), but a non-empty `db_host` no
longer gets second-guessed by its *value*. A DB-less dev box costs one `INFO`-level log line at
startup and a fail-silent `connect()` — never a hang.

**Consequences.** `docs/local-db.md`'s former "set `DB_HOST=127.0.0.1`, not `localhost`" workaround
(a footgun documented rather than fixed) is now obsolete and has been rewritten to describe this
flag instead — see that file. `tests/test_journal_enable.py` pins the new predicate directly.
`.env.example` ships `DB_HOST=localhost` unchanged (correct now — this box's DB really is there) and
documents `JOURNAL_DB_ENABLED` as commented-out-default-true.

---

## Decision 5 — TCP healthcheck for the Compose TimescaleDB container (#104)

**Context.** `docker-compose.yml`'s original healthcheck used `pg_isready` against the Unix socket.
On first boot, `initdb` runs a temporary bootstrap Postgres server that listens **only** on that
socket while it finishes setting up the real instance — a socket-based `pg_isready` reports
`healthy` against that temporary server, and a client that connects during that window gets "the
database system is shutting down." This was observed live on this box, not merely reasoned about:
the first `alembic upgrade head` attempt against a fresh container hit exactly that failure.

**Decision.** The healthcheck forces TCP: `pg_isready -h 127.0.0.1 -U "$${POSTGRES_USER}" -d
"$${POSTGRES_DB}"`. The temporary `initdb` server never binds TCP, so a TCP-based check only reports
healthy once the real, fully-initialized server is accepting connections — which is the actual
precondition `setup_local.sh` and `docs/local-db.md` need before running Alembic.

**Consequences.** `start_period: 30s` and `retries: 12` give first boot (which runs `initdb` plus
the TimescaleDB extension bootstrap, appreciably slower than a warm restart) room to finish before
the healthcheck starts counting failures. `setup_local.sh` step 6 polls
`docker inspect --format '{{.State.Health.Status}}'` and times out at 120s with the exact
`docker compose logs` command to run if it doesn't turn healthy.

---

## Decision 6 — IST-native cron, gated on the system timezone (#105)

**Context.** The AWS agent ran UTC and its crontab carried UTC times (`15 11 * * 1-5` for 16:45
IST calibration, etc.) — correct there, unreadable everywhere else, and silently wrong the moment
anyone assumed the numbers meant what they said.

**Decision.** `setup_local.sh`'s managed cron block is written directly in IST: `*/5 * * * *`
health, `0 9 * * 1-5` egress check, `45 16 * * 1-5` calibration, `0 17 * * 1-5` EOD summary, `30 2
* * *` backup. Installing it is **gated on the box's system timezone actually being
`Asia/Kolkata`** — if it isn't, the script installs nothing, prints the block, and tells the
operator to either `timedatectl set-timezone Asia/Kolkata` and re-run, or install the block by hand
with the times shifted (or a `CRON_TZ=Asia/Kolkata` line, which vixie-cron on Ubuntu honours).
Installing IST-labelled jobs onto a box that isn't actually on IST would silently fire the EOD
square-off summary mid-session.

**Consequences.** Idempotency is a marker-delimited managed block
(`# >>> dhan-local (managed by infra/scripts/setup_local.sh) >>>` … `# <<< dhan-local <<<`);
re-runs strip and rebuild it, so lines are updated, never duplicated, and anything an operator
added outside the markers survives untouched. Stray `/opt/dhan-trading` lines found *outside* the
managed block (leftovers from a hand-copied AWS crontab) trigger a warning, since those would fire
in addition to the managed ones.

---

## Decision 7 — Services installed enabled, not started (#105)

**Context.** `setup_agent.sh` ran once, as root, on a blank EC2 instance whose `.env` was already
real (sourced from SSM) by the time the units existed — `enable --now` was safe because there was
nothing to fail auth against. `setup_local.sh` runs repeatedly, as an operator, possibly against a
freshly-templated `.env` that still reads `your_client_id_here`.

**Decision.** Step 8 runs `systemctl enable dhan-trader dhan-api` — enable only. Starting is a
separate, explicit `--start` flag.

**Consequences.** Two reasons this is deliberate, not an oversight: (1) the first start is a
validation decision — "come back after a reboot" and "trade now" are not the same choice, and the
bootstrap script doesn't get to make the second one for the operator; (2) a template `.env` right
after a fresh bootstrap would make the trader fail auth on every boot, `Restart=on-failure` would
bring it back, `StartLimitBurst=5` means five failed authentication attempts against a **production**
API nobody asked for, and `OnFailure=dhan-alert@` would fire a Telegram alert for a machine that was
merely being set up. `dhan-alert@.service` itself is installed but never `enable`d — it's a
`OnFailure=`-triggered template, not something that runs on its own.

---

## Decision 8 — Backups: nightly `pg_dump`, integrity-gated before publish (#103)

**Context.** AWS backup was EBS + DLM daily snapshots — block-level, infrastructure-owned, and gone
with the EC2 instances. Owned hardware with a single consumer SSD snapshots nothing by itself.

**Decision.** `scripts/backup_db.sh`: `pg_dump -Fc` via `docker compose exec`, written to a
`.part` file, gated on `pg_restore --list` succeeding against it (piped back in over stdin — the
archive TOC sits at the head of the file, so a non-seekable stream is fine) before the `.part` is
renamed to the final `.dump` name. A dump that fails the integrity check is deleted; a dump that
passes but whose rename fails (disk full, read-only remount) is **kept**, not cleaned up — a
verified backup is real even if its filename is still `.part`. Optional best-effort S3 push; a
missing CLI, bad credentials, or a dead network downgrades to a `WARN` and the run still exits 0,
because the local dump is already written and verified. Rotation keeps `BACKUP_KEEP` (default 14)
dumps *per database*, matched against an anchored glob so it can never delete a hand-made file that
doesn't fit the naming pattern.

**Consequences.** `$BACKUP_DIR` gets `chmod 700`, dumps get `chmod 600` (a dump is the entire
trading database — positions, orders, `features_snapshot`, PnL — in one file, and the only gate
protecting it on this box is the filesystem, unlike the IAM/encryption gate EBS snapshots had on
AWS). Restoring TimescaleDB is not a plain `pg_restore` — the extension needs
`timescaledb_pre_restore()` / `timescaledb_post_restore()` bracketing the restore, documented in
full in `docs/local-db-backups.md`.

---

## Verification evidence (executed on the T1700 box, 2026-08-14)

- **Alembic bootstrap, virgin container:** `docker compose up -d timescaledb` →
  TCP-healthchecked → `alembic upgrade head` completed clean on a container with no prior state.
  `alembic current` reported `014`; the database held **8 hypertables** and **31 tables** total.
- **Zero-history paper path:** traced and confirmed graceful — the paper engine runs and logs
  warnings rather than failing when the tables it queries are empty.
- **`setup_local.sh` idempotency:** first run against the box made **14 changes**. An immediate
  re-run with no intervening edits reported **0 mutations** (all steps `[skip]`) — genuine
  idempotency, not "runs again without erroring."
- **Test suite:** `pytest -q` — 1691 passed (includes the new `test_egress_proxy.py`,
  `test_empty_db_bootstrap.py`, `test_journal_enable.py`, `test_setup_local_parser.py` added by
  this migration). `ruff check` clean.

---

## What this migration deliberately did not touch

- The trading engine, `RiskEngine`, `strategies/orb.py`, the Kronos gate, and the backtester are
  byte-for-byte unchanged — this was an environment port, not a strategy or risk change.
- The 2026-06-21 research conclusion (no validated edge on any tested strategy) is untouched by any
  of this and has not been re-run on this box's own data — see `CLAUDE.md`'s CONCLUSION block.
- `infra/*.tf` (Terraform) was left in place, dormant, rather than deleted — the AWS
  S3/SSM/EBS-snapshot salvage question is still an open operator decision, and deleting the
  Terraform config would foreclose auditing what it used to describe.
- Dhan credential rotation (SEC-2) was already pending before this migration and remains pending
  after it — this migration did not touch Dhan-side credentials at all.
