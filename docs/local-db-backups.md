# Local DB Backups — `scripts/backup_db.sh`

Nightly logical backups of the TimescaleDB databases running in the local
Docker Compose stack on the home server.

**Why this exists:** on AWS the EBS + DLM daily-snapshot policy was the backup.
That died with the EC2 migration — owned hardware snapshots nothing by itself,
and this box runs a single consumer SSD. A checked `pg_dump` on a rotation is
the durability floor that replaces it.

> A dump on the same disk as the database survives *software* loss (bad
> migration, dropped table, corrupt table) but not *hardware* loss. For the
> second class of failure, either turn on the S3 leg (below) or copy
> `$BACKUP_DIR` to a second physical device.

---

## What it does

For each database, in order:

1. **Dump** — `docker compose exec -T <service> pg_dump -U <user> -Fc <db>`,
   redirected on the host into `$BACKUP_DIR/<db>_YYYY-MM-DD_HHMM.dump.part`.
   Custom format (`-Fc`) so `pg_restore` can do selective/parallel restores.
2. **Integrity-gate** — `docker compose exec -T <service> pg_restore --list < <file>`.
   The dump lives on the host and `pg_restore` lives in the container, so the
   file is piped *back in* over stdin (`exec -T` wires stdin through, and the
   archive TOC sits at the head of the file, so a non-seekable stream is fine).
   A dump that cannot be listed is not a backup: the file is deleted and the
   run fails.
3. **Publish** — only after the gate passes is `.part` renamed to the final
   `.dump` name. A crashed or failed run therefore never leaves anything that
   later looks like a valid backup. If the rename itself fails (read-only
   remount, quota, ENOSPC) the already-verified `.part` file is **kept**, not
   cleaned up, and the alert names its path — a dump that passed the gate is a
   real backup and is never deleted by the cleanup path.
4. **S3 push** (optional) — best effort, see below.
5. **Rotate** — keep the newest `$BACKUP_KEEP` dumps *of that database*.

Every step logs to stdout with a timestamp. Any fatal step fires a Telegram
alert through `core/notify.py` and exits non-zero, so cron surfaces the failure.

### Permissions

A dump is the entire trading database in one file — positions, orders, signals,
`features_snapshot`, PnL. On AWS the equivalent (EBS snapshots) was IAM- and
encryption-gated; on this box the only gate is the filesystem. So the script
sets `umask 077` before it creates anything and additionally `chmod 700`s
`$BACKUP_DIR` on every run:

- `$BACKUP_DIR` → `0700`, dumps → `0600`, owned by the cron user.
- A pre-existing directory the script cannot `chmod` (owned by someone else)
  produces a `WARN` rather than a failure — but fix it, because the dumps in it
  are then readable by every local account for the full `BACKUP_KEEP` window.

---

## Configuration

All environment variables, all defaulted — the script runs with no arguments.

| Variable | Default | Meaning |
|---|---|---|
| `BACKUP_DIR` | `/var/backups/dhan` | Where dumps land. Must be an absolute path and writable by the cron user. |
| `BACKUP_KEEP` | `14` | Dumps kept **per database**. |
| `DB_NAME` | `dhan_trading` | Primary database. |
| `EXTRA_DBS` | *(empty)* | Additional databases, space- or comma-separated (e.g. `dhan_clean`). Default is `dhan_trading` only. |
| `DB_USER` | `trader` | Postgres role used for the dump. |
| `COMPOSE_SERVICE` | `timescaledb` | Compose service name running Postgres. |
| `S3_BUCKET` | *(empty)* | Empty = no upload. |
| `COMPOSE_FILE` | *(unset)* | Honoured natively by `docker compose`; otherwise the project file is resolved from the repo root. |

The script derives the repo root from its own location (`$(dirname "$0")/..`),
so it works from any clone path — nothing is hardcoded to `/opt`.

```bash
scripts/backup_db.sh                                    # defaults
BACKUP_KEEP=30 EXTRA_DBS=dhan_clean scripts/backup_db.sh
S3_BUCKET=my-bucket scripts/backup_db.sh
scripts/backup_db.sh --help
```

Database names are validated against `[A-Za-z0-9_]+` and refused otherwise —
the rotation builds a shell glob out of the name, so nothing exotic gets in.

---

## Install

```bash
sudo mkdir -p /var/backups/dhan
sudo chown "$USER":"$USER" /var/backups/dhan     # cron user must be able to write
sudo chmod 700 /var/backups/dhan                 # dumps are full DB copies — owner only
chmod +x scripts/backup_db.sh
scripts/backup_db.sh                             # one manual run to prove it works
```

The cron user must also be able to talk to the Docker daemon (member of the
`docker` group, or run the job as root with `BACKUP_DIR` writable by root).

### Cron — 02:30 IST nightly

```cron
# m h  dom mon dow  command
30 2 * * * cd "$HOME/dhan_algo" && ./scripts/backup_db.sh >> /var/log/dhan/backup.log 2>&1
```

02:30 is well clear of the trading session and of the 17:00 IST EOD jobs.
If the host clock is **not** on IST, pin the schedule explicitly at the top of
the crontab:

```cron
CRON_TZ=Asia/Kolkata
```

Add `/var/log/dhan/backup.log` to the existing logrotate config so it does not
grow unbounded.

---

## Rotation

Dump filenames are `<db>_YYYY-MM-DD_HHMM.dump`, which sorts lexically in
chronological order (the script forces `LC_ALL=C`), so rotation needs no `ls`
parsing and no mtime comparison: it globs, counts, and removes the oldest
`total - BACKUP_KEEP` entries.

The glob is anchored to `$BACKUP_DIR` and spells the timestamp out in digit
classes:

```
$BACKUP_DIR/<db>_[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]_[0-9][0-9][0-9][0-9].dump
```

Consequences worth knowing:

- One database never rotates another's files, and each database keeps its own
  `BACKUP_KEEP` dumps.
- Hand-made files that do not match the pattern (`dhan_trading_manual.dump`,
  notes, subdirectories) are **never** deleted — but they are also never
  counted, so they accumulate on their own.
- Every deletion is additionally checked to be a path under `$BACKUP_DIR`.

At 14 daily dumps you hold two weeks of history. Watch the disk: this box has a
small SSD, and a compressed custom-format dump of a bar-heavy database is not
small. `du -sh /var/backups/dhan` before raising `BACKUP_KEEP`.

---

## Optional S3 push

Off unless `S3_BUCKET` is set. When set **and** the `aws` CLI is installed, the
fresh dump is copied to `s3://$S3_BUCKET/backups/<file>`; credentials come from
the ambient AWS config (profile/env/instance role).

**The S3 leg can never fail the backup.** A missing CLI, bad credentials, or a
dead network produces a `WARN` line on stderr; the local dump has already been
written and verified, and the script still exits 0. If `S3_BUCKET` is empty the
whole step is skipped silently.

---

## Restore

TimescaleDB restores are **not** a plain `pg_restore` — the extension needs to
be put in restore mode first, or the hypertable catalog and the restored chunks
disagree. Full procedure, restoring into the existing database:

```bash
# 0. Stop anything writing to the DB first.
sudo systemctl stop dhan-trader dhan-api

# 1. Copy the dump into the container. A custom-format archive can be restored
#    from stdin sequentially, but a real file is seekable — which is what
#    parallel (-j) and selective (-L/-t) restores require. Copy it in.
docker compose cp /var/backups/dhan/dhan_trading_2026-08-14_0230.dump \
    timescaledb:/tmp/restore.dump

# 2. Put TimescaleDB into restore mode.
docker compose exec -T timescaledb \
    psql -U trader -d dhan_trading -c "SELECT timescaledb_pre_restore();"

# 3. Restore.
docker compose exec -T timescaledb \
    pg_restore -U trader -d dhan_trading --clean --if-exists /tmp/restore.dump

# 4. Leave restore mode (REQUIRED — the DB stays crippled otherwise).
docker compose exec -T timescaledb \
    psql -U trader -d dhan_trading -c "SELECT timescaledb_post_restore();"

docker compose exec -T timescaledb rm -f /tmp/restore.dump
sudo systemctl start dhan-trader dhan-api
```

Notes:

- `--clean --if-exists` drops existing objects before recreating them, so this
  overwrites the live database. Restoring into a **new** database
  (`createdb dhan_restore_test`, then `CREATE EXTENSION timescaledb;` and the
  same pre/post-restore dance) is the safe way to inspect a backup or to do a
  partial recovery.
- Add `--no-owner` if the target role differs from `trader`.
- `pg_restore -j 4` parallelises a large restore (custom format only, and only
  from a real file, not stdin).
- After a restore, run `alembic current` and confirm it matches the schema head
  the code expects.

### Inspect a dump without restoring

```bash
docker compose exec -T timescaledb pg_restore --list \
    < /var/backups/dhan/dhan_trading_2026-08-14_0230.dump | head -40
```

This is exactly the integrity gate the script runs after every dump.

---

## Alerting

On any fatal error the script calls `python -m core.notify "<message>"` using
`$REPO_ROOT/.venv/bin/python` (falling back to `python3` on `PATH`), from the
repo root so the module resolves and `load_dotenv()` picks up the repo `.env`
for `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID`.

The alert is strictly best effort: if it fails, the script logs a `WARN` and
still exits with the **original** failure status. Notification never masks the
error, and it never rescues a failed backup.

---

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `docker not found on PATH` | Cron's `PATH` is minimal — use an absolute path to the script and, if needed, set `PATH=` at the top of the crontab. |
| `postgres in compose service 'timescaledb' is not accepting connections` | Stack is down (`docker compose ps`), or `COMPOSE_SERVICE` / `DB_USER` do not match the compose file. |
| `BACKUP_DIR ... is not writable` | The cron user does not own `$BACKUP_DIR` — see Install. |
| `integrity check failed` | The archive is unreadable; the file is deleted deliberately. Check free disk space first (a truncated write is the usual cause). |
| `could not publish the verified dump ... PRESERVED at <file>.part` | The dump is **good** — only the rename failed (disk full / read-only mount). Free space, then `mv` the `.part` file to the same name without the suffix. |
| `could not chmod 700 <dir> — dumps may be readable by other local users` | `$BACKUP_DIR` is owned by another account. `sudo chown "$USER":"$USER"` it and re-run — see Install. |
| `permission denied while trying to connect to the Docker daemon` | Add the cron user to the `docker` group and re-login. |
| Dumps accumulate past `BACKUP_KEEP` | Filenames were renamed by hand and no longer match the rotation glob. |
