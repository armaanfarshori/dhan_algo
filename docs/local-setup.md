# Local setup — single-host bootstrap

The platform used to be two EC2 instances brought up by `infra/scripts/setup_agent.sh`
running as user-data: root on a blank machine, secrets out of SSM Parameter Store, the
repo cloned fresh into `/opt/dhan-trading`. That box is gone. Everything — trader,
dashboard, database, cron — now runs on one home server, from a checkout that already
exists and that you `git pull` like any other working copy.

`infra/scripts/setup_local.sh` is the replacement. It is not user-data: it runs as your
login user, escalates per step, and is **idempotent** — every step detects "already
done", says so, and moves on. Re-run it after a pull, after a partial failure, or just
to re-assert the machine state.

Two decisions shape everything below.

**`/opt/dhan-trading` becomes a symlink to the checkout.** That path is hardcoded in the
systemd units, in `config.py` defaults (`BACKFILL_CHECKPOINT_PATH`), in the cron lines
and across the docs. Rewriting all of it would mean a permanent local diff fighting
every merge. A symlink makes every one of those paths correct as written, and leaves one
real directory to pull into.

**Credentials come from `.env`, filled by hand.** There is no SSM here and nothing to
replace it with. The script seeds `.env` from `.env.example`, `chmod 600`s it, and prints
exactly which keys you must fill. It never invents, fetches or rewrites a credential, and
it never touches `PAPER_TRADING`.

---

## Prerequisites

- Ubuntu 24.04 (the script warns and continues on anything else — apt and systemd steps
  assume Debian-family).
- The repo cloned somewhere you own, e.g. `/home/<you>/dhan_algo`. Run the script from
  the **primary checkout**, not a git worktree; it warns if it detects one, because
  `/opt/dhan-trading` would then point at a tree that gets deleted.
- `sudo` rights. The script refuses to run *as* root — it escalates only where it must,
  so that the venv, `.env` and the repo stay owned by you.
- Network access (apt, PyPI, Docker Hub, and `get.docker.com` if Docker is absent).
- Nothing else. Python 3.12 and Docker are installed for you if missing.

---

## Bootstrap

```bash
cd ~/dhan_algo
infra/scripts/setup_local.sh
```

Then fill in `.env` (the script prints the list) and start the services:

```bash
sudo systemctl start dhan-trader dhan-api
```

---

## What the script does

Ten steps, in order. Each prints `[done]` or `[skip]`, and the run ends with a summary
plus the manual follow-ups it could not do for you.

**1. Preflight.** Refuses to run as root. Checks the distro (warn-only). Resolves the
repo root from its own location and the target user from `$USER`, validating the username
before it is spliced into a systemd `User=` line. Warns if the checkout is a git worktree.
Verifies sudo works (may prompt) before doing anything.

**2. Packages.** Installs `python3.12`, `python3.12-venv`, `python3.12-dev` if `dpkg`
says they are missing — and only then does it run `apt-get update`. Installs Docker via
the official `get.docker.com` convenience script **only if `docker` is absent**; that is
the one network-sourced installer here. Adds you to the `docker` group if you are not in
it. Group membership only takes effect at your next login, so the rest of the run falls
back to `sudo docker`.

**3. Symlink.** Creates `/opt/dhan-trading -> <repo root>`. If it already points there,
skip. If it exists as *anything else* — a real directory from an old-style clone, or a
symlink somewhere else — the script **aborts and tells you**. It never deletes what it
finds at that path.

**4. venv.** Creates `<repo>/.venv` with `python3.12` if it is missing, then always runs
`pip install -r requirements.txt` (idempotent by nature, and a `git pull` may have moved
requirements since the last bootstrap).

**5. `.env`.** If absent: copy `.env.example`, `chmod 600`, and print a loud banner
naming the keys you must fill —

| Key | Why |
|---|---|
| `DHAN_CLIENT_ID` | Dhan account identity |
| `DHAN_ACCESS_TOKEN` | seed/bootstrap token |
| `DHAN_PIN` | enables automatic token refresh |
| `DHAN_TOTP_SECRET` | with the PIN, tokens self-rotate into `dhan_token.json` |
| `TELEGRAM_BOT_TOKEN` | alerts — health monitor, EOD summary, backup failures |
| `TELEGRAM_CHAT_ID` | alert destination |
| `DHAN_PROXY_URL` | **only** if this box's public IP is not the whitelisted one |
| `DHAN_WHITELISTED_EGRESS_IP` | the IP Dhan whitelisted; read by `scripts/egress_check.py` |

If `.env` already exists it is left completely alone, apart from re-asserting mode 600
(cheap repair, and the file holds live credentials). `PAPER_TRADING` is never written or
flipped by this script in either path; if it finds `PAPER_TRADING=false` it warns, so a
live-armed box cannot be mistaken for a paper one.

**6. Database.** `docker compose up -d timescaledb` from the repo root (skipped if the
container is already running), then polls
`docker inspect --format '{{.State.Health.Status}}' dhan-timescaledb` until `healthy`,
timing out at 120s with the `docker compose logs` command you need. First boot is slow —
`initdb` plus the TimescaleDB bootstrap — and the healthcheck is deliberately TCP-based,
so "healthy" really does mean the real server is accepting connections. Only then does it
run `.venv/bin/alembic upgrade head` (skipped when `alembic current` already reports
`(head)`).

Alembic reads `DB_*` from the **environment**, not from `.env` — dotenv loading is a
`config.py`/pydantic feature and Alembic never imports `config.py`. So the script reads
those five keys out of `.env` itself with a targeted `KEY=VALUE` parser and exports them,
falling back to the `config.py` defaults. It deliberately does **not** `source .env`:
that file is operator input full of secrets and shell metacharacters, and sourcing it
would execute whatever is in there and dump the whole file into the environment.

**7. Logs.** `mkdir -p /var/log/dhan` owned by you (the units write there via
`StandardOutput=append:`, which needs the directory to exist first), and
`/etc/logrotate.d/dhan-trading` — daily, 14 rotations, compressed, `copytruncate`.
Byte-identical content to what `setup_agent.sh` installed, and skipped if already
identical. `copytruncate` matters: the trader and API hold their log FDs open for the
life of the process, so rotation must not rename out from under them.

**8. systemd.** Installs `dhan-trader.service`, `dhan-api.service` and
`dhan-alert@.service` from `infra/systemd/`, applying exactly one edit at install time:

```
sed "s/^User=ubuntu$/User=$TARGET_USER/"
```

Everything else is verbatim — the `/opt/dhan-trading` paths inside the units are correct
because of step 3. The canonical units in the repo keep `User=ubuntu` and are never
modified, so a `git pull` never collides with a local edit. Files identical to what is
already installed are skipped; `daemon-reload` only runs if something actually changed;
if a unit file changed under a *running* service you are told to restart it.

Then `systemctl enable dhan-trader dhan-api` — **enable only, not start**. `dhan-alert@`
is a template, triggered by `OnFailure=`, and is installed but not enabled.

**9. cron.** Five jobs in a marker-delimited managed block in *your* crontab.

**10. Summary.** What changed, what was already in place, and the numbered manual steps
left for you.

---

## Why services are enabled but not started

`--start` additionally starts `dhan-trader` and `dhan-api` (skipping any already active).
The default — enable, don't start — is deliberate, for two reasons:

- **The first start is a validation decision.** Enabling means "come back after a
  reboot"; starting means "trade now". Those are not the same choice and the script does
  not get to make the second one.
- **A template `.env` would churn against Dhan.** Right after a fresh bootstrap the
  credentials are literally `your_client_id_here`. The trader fails auth on boot,
  `Restart=on-failure` brings it back, and `StartLimitBurst=5` means five failed
  authentication attempts against a production API nobody asked for — plus an
  `OnFailure=` Telegram alert for a machine that was merely being set up.

Use `--start` on a re-run, once `.env` is real.

---

## Cron and the timezone

This box runs `Asia/Kolkata`; the retired AWS agent ran UTC and its crontab carried UTC
times (`15 11 * * 1-5` was 16:45 IST). The managed block is IST-native:

| Schedule (IST) | Job | Was on AWS |
|---|---|---|
| `*/5 * * * *` | `scripts/health_alert.py` | same (24/7; market-hours gating is inside the script) |
| `0 9 * * 1-5` | `scripts/egress_check.py` | new — pre-open proxy identity check |
| `45 16 * * 1-5` | `ml.calibration fill` + `report` | `15 11` UTC |
| `0 17 * * 1-5` | `scripts/eod_summary.py` | `30 11` UTC |
| `30 2 * * *` | `scripts/backup_db.sh` | EBS/DLM snapshots |

All of them run as `cd /opt/dhan-trading && .venv/bin/python …`, exactly as on the agent —
which is precisely why the symlink in step 3 matters.

Those times are only correct if the machine's clock agrees. **The cron step is gated on
the system timezone actually being `Asia/Kolkata`**: if it is not, the script installs
nothing, prints the block, and tells you to either
`sudo timedatectl set-timezone Asia/Kolkata` and re-run, or install it yourself with the
times shifted (or with a `CRON_TZ=Asia/Kolkata` line above the block, which vixie-cron
honours). Silently installing IST-labelled jobs onto a UTC box would fire the EOD summary
mid-session.

Idempotency here is a managed block:

```
# >>> dhan-local (managed by infra/scripts/setup_local.sh) >>>
…
# <<< dhan-local <<<
```

Re-runs strip the old block and re-append a fresh one, so lines are *updated*, never
duplicated, and anything you added outside the markers is preserved untouched. If the
crontab has `/opt/dhan-trading` lines *outside* the block — leftovers from the AWS
crontab, say — you get a warning, because those would run in addition to the managed
ones.

One caveat: `scripts/backup_db.sh` talks to Docker. If the script had to add you to the
`docker` group, that membership reaches cron only after you have logged out and back in.

---

## Verify

```bash
# services
systemctl status dhan-trader dhan-api
journalctl -u dhan-trader -n 50

# dashboard (API_BIND_HOST defaults to 0.0.0.0 — also reachable over the tailnet)
curl -s localhost:8765/api/status

# database container + schema
docker compose ps                    # from the repo root; STATUS "Up (healthy)"
.venv/bin/alembic current            # expect the head revision

# the symlink and the units
readlink -f /opt/dhan-trading
systemctl cat dhan-trader | grep User=

# cron
crontab -l

# logs
tail -f /var/log/dhan/trader.log     # also api.log, health_alert.log, backup.log
```

A dashboard that answers on `:8765` and a trader heartbeat under
`/opt/dhan-trading/run/trader_heartbeat.json` mean the bootstrap took.

---

## Re-run safety

Safe, always. Each step checks before it acts:

| Step | "Already done" test |
|---|---|
| Packages | `dpkg-query` per package; `command -v docker`; group membership |
| Symlink | resolves to the same target → skip; anything else → **abort**, never delete |
| venv | `.venv/bin/python` exists; pip install is idempotent regardless |
| `.env` | file exists → only re-assert mode 600 |
| DB | container running → no `up`; `alembic current` at head → no migration |
| Logs | directory ownership; `cmp` against the logrotate config |
| systemd | `cmp` rendered unit vs installed; `is-enabled`; `is-active` before `--start` |
| cron | managed block stripped and rebuilt; identical result → no write |

The two hard stops are both at the symlink: an existing non-symlink at
`/opt/dhan-trading`, or a symlink pointing elsewhere. Both abort with the exact command
to resolve them. Everything else either proceeds or skips.

---

## Differences from the AWS deploy

| Concern | AWS (`setup_agent.sh`) | Here (`setup_local.sh`) |
|---|---|---|
| Secrets | SSM Parameter Store, fetched at boot | hand-edited `.env` (mode 600), seeded from `.env.example` |
| Code location | `git clone` into `/opt/dhan-trading` | existing checkout; `/opt/dhan-trading` is a **symlink** to it |
| Service user | `ubuntu` (baked into the units) | your login user, templated in at install time by `sed` |
| Database | dedicated EC2, bare-metal PG16 + TimescaleDB | Docker Compose container on loopback (`docs/local-db.md`) |
| Backups | EBS snapshots via DLM | nightly `scripts/backup_db.sh` (`docs/local-db-backups.md`) |
| Order-path IP | Elastic IP whitelisted at Dhan | egress proxy over Tailscale (`DHAN_PROXY_URL`), checked pre-open by `scripts/egress_check.py` |
| Cron timezone | UTC | IST (`Asia/Kolkata`), guarded on the system timezone |
| Run model | once, as root, on a blank instance | repeatedly, as you, on a long-lived box |
| First start | `systemctl enable --now` | `enable` only; starting is a deliberate step (`--start`) |

---

## Gotchas

**"do not run this as root".** Correct. `sudo infra/scripts/setup_local.sh` would leave
the venv, `.env` and any pip cache root-owned, and would template the units with
`User=root`. Run it as yourself.

**`/opt/dhan-trading already exists and is NOT a symlink.`** Almost certainly the old
EC2-style clone. The script will not delete it — check nothing is running out of it, then
`sudo mv /opt/dhan-trading /opt/dhan-trading.old` and re-run.

**The database never became healthy.** First boot can be slow, but 120s is generous.
Check `docker compose logs --tail=50 timescaledb` from the repo root; the usual causes
are port 5432 already bound on the host, or a pre-existing `dhan_pgdata` volume from a
different PostgreSQL major.

**Docker permission denied after the first run.** Expected — the `docker` group applies
at your next login. The script itself works around it with `sudo docker`; your own shell
and cron do not.

**Nothing at `:8765`.** The API is enabled, not started, unless you passed `--start`.

See also: `docs/local-db.md` (database runbook), `docs/local-db-backups.md` (backup and
restore), `docs/Operations-Runbook.md` (day-to-day operation).
