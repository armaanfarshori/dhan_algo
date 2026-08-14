#!/usr/bin/env bash
#
# backup_db.sh — nightly logical backup of the TimescaleDB databases that live
# in the local Docker Compose stack. Replaces the AWS EBS/DLM snapshot schedule
# that died with the EC2 migration: on owned hardware nothing takes a snapshot
# for us, so a checked pg_dump on a rotation is the durability floor.
#
# What it does, per database:
#   1. pg_dump -Fc  through `docker compose exec -T` into $BACKUP_DIR
#   2. integrity-gates the fresh dump with `pg_restore --list` (a dump that
#      cannot be listed is not a backup) — a failed gate deletes the file
#   3. rotates, keeping the newest $BACKUP_KEEP dumps of that database
#   4. optionally copies the new dump to S3 (best effort — never fatal)
# Any fatal step fires a Telegram alert via the repo's core.notify and exits
# non-zero so cron/systemd surfaces the failure.
#
# Configuration — all environment, all defaulted:
#   BACKUP_DIR       /var/backups/dhan   where dumps land (must be writable)
#   BACKUP_KEEP      14                  dumps kept per database
#   DB_NAME          dhan_trading        primary database
#   EXTRA_DBS        (empty)             extra DBs, space/comma separated
#                                        (e.g. "dhan_clean")
#   DB_USER          trader              postgres role used for the dump
#   COMPOSE_SERVICE  timescaledb         compose service running postgres
#   S3_BUCKET        (empty)             empty = no S3 push
#   COMPOSE_FILE     (unset)             honoured natively by docker compose
#
# Usage:
#   scripts/backup_db.sh
#   BACKUP_KEEP=30 EXTRA_DBS=dhan_clean S3_BUCKET=my-bucket scripts/backup_db.sh
#
set -euo pipefail
set -E          # ERR trap inherits into functions (line numbers in alerts)

# Deterministic collation (the rotation relies on glob ordering) and stable
# numeric date output.
export LC_ALL=C

# Empty globs must expand to nothing, not to the literal pattern — the
# rotation feeds glob results straight to rm.
shopt -s nullglob

# ── Repo root from this script's own location — never a hardcoded /opt path ──
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# ── Config ───────────────────────────────────────────────────────────────────
BACKUP_DIR="${BACKUP_DIR:-/var/backups/dhan}"
BACKUP_KEEP="${BACKUP_KEEP:-14}"
DB_NAME="${DB_NAME:-dhan_trading}"
EXTRA_DBS="${EXTRA_DBS:-}"
DB_USER="${DB_USER:-trader}"
COMPOSE_SERVICE="${COMPOSE_SERVICE:-timescaledb}"
S3_BUCKET="${S3_BUCKET:-}"

# ── Logging ──────────────────────────────────────────────────────────────────
log()  { printf '%s  %s\n' "$(date '+%Y-%m-%d %H:%M:%S %z')" "$*"; }
warn() { printf '%s  WARN  %s\n' "$(date '+%Y-%m-%d %H:%M:%S %z')" "$*" >&2; }

# ── Failure handling ─────────────────────────────────────────────────────────
FAIL_MSG=""
ERR_LINE=""
PARTIAL=""

# Fatal: record a human message, then exit — the EXIT trap does the alerting so
# there is exactly one alert per run no matter which path failed.
die() {
    FAIL_MSG="$1"
    printf '%s  ERROR %s\n' "$(date '+%Y-%m-%d %H:%M:%S %z')" "$1" >&2
    exit "${2:-1}"
}

# Best-effort Telegram alert through the repo venv. Never raises, never changes
# the exit status of the caller — the original failure must not be masked by a
# missing venv or a dead network.
notify() {
    local msg="$1" py="$REPO_ROOT/.venv/bin/python"
    if [ ! -x "$py" ]; then
        py="$(command -v python3 2>/dev/null || true)"
    fi
    if [ -z "$py" ]; then
        warn "notify: no python interpreter found — alert dropped: $msg"
        return 0
    fi
    # core.notify loads the repo .env itself; run from REPO_ROOT so `-m` resolves.
    if ! (cd "$REPO_ROOT" && "$py" -m core.notify "$msg") >/dev/null 2>&1; then
        warn "notify: telegram alert failed — original error stands"
    fi
    return 0
}

trap 'ERR_LINE=$LINENO' ERR

on_exit() {
    local rc=$?
    # Drop any half-written dump so a truncated file can never be mistaken for
    # a backup or survive rotation.
    if [ -n "$PARTIAL" ] && [ -f "$PARTIAL" ]; then
        rm -f -- "$PARTIAL" || true
        warn "removed partial dump $PARTIAL"
    fi
    if [ "$rc" -ne 0 ]; then
        local msg="${FAIL_MSG:-}"
        if [ -z "$msg" ]; then
            msg="unexpected failure (exit $rc${ERR_LINE:+, line $ERR_LINE})"
        fi
        notify "DB BACKUP FAILED on $(hostname): ${msg}"
    fi
    exit "$rc"
}
trap on_exit EXIT

# ── Help ─────────────────────────────────────────────────────────────────────
case "${1:-}" in
    -h|--help)
        sed -n '2,/^set -euo/p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//; $d'
        trap - EXIT
        exit 0
        ;;
    "") : ;;
    *) die "unknown argument: $1 (see --help)" ;;
esac

# ── Validation ───────────────────────────────────────────────────────────────
[ -n "$BACKUP_DIR" ] || die "BACKUP_DIR must not be empty"
case "$BACKUP_DIR" in
    /*) : ;;
    *)  die "BACKUP_DIR must be an absolute path (got: $BACKUP_DIR)" ;;
esac
[[ "$BACKUP_KEEP" =~ ^[0-9]+$ ]] && [ "$BACKUP_KEEP" -ge 1 ] \
    || die "BACKUP_KEEP must be a positive integer (got: $BACKUP_KEEP)"

# Build the database list: primary first, then EXTRA_DBS (space or comma
# separated), de-duplicated.
DATABASES=()
add_db() {
    local candidate="$1" existing
    [ -n "$candidate" ] || return 0
    # Identifiers are restricted: the rotation builds a shell glob from the
    # database name, so anything but [A-Za-z0-9_] is refused outright.
    [[ "$candidate" =~ ^[A-Za-z0-9_]+$ ]] \
        || die "invalid database name: '$candidate' (expected [A-Za-z0-9_]+)"
    for existing in ${DATABASES[@]+"${DATABASES[@]}"}; do
        [ "$existing" = "$candidate" ] && return 0
    done
    DATABASES+=("$candidate")
}
add_db "$DB_NAME"
IFS=', ' read -r -a _extra <<< "$EXTRA_DBS"
for _db in ${_extra[@]+"${_extra[@]}"}; do
    add_db "$_db"
done

# ── Docker Compose discovery ─────────────────────────────────────────────────
command -v docker >/dev/null 2>&1 || die "docker not found on PATH"
if docker compose version >/dev/null 2>&1; then
    COMPOSE=(docker compose)
elif command -v docker-compose >/dev/null 2>&1; then
    COMPOSE=(docker-compose)
else
    die "neither 'docker compose' nor 'docker-compose' is available"
fi

# docker compose resolves its project file relative to the working directory
# (or from $COMPOSE_FILE); the stack lives at the repo root.
cd "$REPO_ROOT"

# ── Preflight ────────────────────────────────────────────────────────────────
mkdir -p "$BACKUP_DIR" || die "cannot create BACKUP_DIR $BACKUP_DIR"
[ -w "$BACKUP_DIR" ] || die "BACKUP_DIR $BACKUP_DIR is not writable by $(id -un)"

log "backup start — dir=$BACKUP_DIR keep=$BACKUP_KEEP service=$COMPOSE_SERVICE databases=${DATABASES[*]}"

if ! "${COMPOSE[@]}" exec -T "$COMPOSE_SERVICE" \
        pg_isready -U "$DB_USER" -d "$DB_NAME" >/dev/null 2>&1; then
    die "postgres in compose service '$COMPOSE_SERVICE' is not accepting connections (pg_isready failed)"
fi

# One timestamp for the whole run so a multi-database backup is one set.
STAMP="$(date '+%Y-%m-%d_%H%M')"

# ── Rotation ─────────────────────────────────────────────────────────────────
# Filenames are <db>_YYYY-MM-DD_HHMM.dump, so lexical order (LC_ALL=C) is
# chronological order — no ls parsing, no mtime races. The glob is anchored to
# $BACKUP_DIR and the digit classes keep one database from matching another's
# files (or any stray file dropped in the directory).
rotate() {
    local db="$1" total remove idx file
    local files=("$BACKUP_DIR/${db}_"[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]_[0-9][0-9][0-9][0-9].dump)
    total=${#files[@]}
    if [ "$total" -le "$BACKUP_KEEP" ]; then
        log "$db: rotation — $total dump(s) on disk, keep=$BACKUP_KEEP, nothing to remove"
        return 0
    fi
    remove=$(( total - BACKUP_KEEP ))
    log "$db: rotation — $total dump(s) on disk, removing $remove oldest"
    for (( idx = 0; idx < remove; idx++ )); do
        file="${files[$idx]}"
        # Belt and braces: never delete anything outside BACKUP_DIR.
        case "$file" in
            "$BACKUP_DIR"/*)
                if rm -f -- "$file"; then
                    log "$db: rotated out $(basename -- "$file")"
                else
                    warn "$db: failed to remove $file"
                fi
                ;;
            *) warn "$db: refusing to delete outside BACKUP_DIR: $file" ;;
        esac
    done
}

# ── Optional S3 push (best effort — never fails the local backup) ────────────
push_s3() {
    local file="$1"
    [ -n "$S3_BUCKET" ] || return 0
    if ! command -v aws >/dev/null 2>&1; then
        warn "S3_BUCKET set but the aws CLI is not installed — skipping upload"
        return 0
    fi
    local base dest
    base="$(basename -- "$file")"
    dest="s3://${S3_BUCKET}/backups/${base}"
    if aws s3 cp "$file" "$dest" --only-show-errors; then
        log "uploaded ${base} → ${dest}"
    else
        warn "S3 upload of ${base} failed — local backup is unaffected"
    fi
    return 0
}

# ── Per-database backup ──────────────────────────────────────────────────────
backup_db() {
    local db="$1"
    local dest="$BACKUP_DIR/${db}_${STAMP}.dump"
    local tmp="${dest}.part"

    # Dump into a .part file: it is outside the rotation glob, so a crashed or
    # failed run leaves nothing that later looks like a valid backup.
    PARTIAL="$tmp"
    rm -f -- "$tmp"

    log "$db: dumping (pg_dump -Fc) …"
    if ! "${COMPOSE[@]}" exec -T "$COMPOSE_SERVICE" \
            pg_dump -U "$DB_USER" -Fc "$db" > "$tmp"; then
        die "$db: pg_dump failed"
    fi
    [ -s "$tmp" ] || die "$db: pg_dump produced an empty file"

    # Integrity gate. The dump lives on the host and pg_restore lives in the
    # container, so feed the file back in over stdin — `exec -T` wires stdin
    # through, and `pg_restore --list` only needs the archive TOC at the head,
    # which is readable from a non-seekable stream.
    log "$db: verifying archive (pg_restore --list) …"
    if ! "${COMPOSE[@]}" exec -T "$COMPOSE_SERVICE" \
            pg_restore --list < "$tmp" > /dev/null; then
        die "$db: integrity check failed — pg_restore could not read the dump"
    fi

    mv -f -- "$tmp" "$dest"
    PARTIAL=""
    log "$db: ok → $dest ($(du -h -- "$dest" | cut -f1))"

    push_s3 "$dest"
    rotate "$db"
}

for _db in "${DATABASES[@]}"; do
    backup_db "$_db"
done

log "backup complete — ${#DATABASES[@]} database(s)"
