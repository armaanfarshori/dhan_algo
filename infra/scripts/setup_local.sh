#!/usr/bin/env bash
#
# setup_local.sh — single-host bootstrap for DhanAIBot on owned hardware.
#
# Replaces infra/scripts/setup_agent.sh, which was EC2 user-data: it ran once as
# root on a fresh instance, pulled secrets from SSM, cloned the repo into
# /opt/dhan-trading and installed systemd units + cron. None of that exists any
# more — there is no SSM, no clone step (the repo is already here), and the box
# is a long-lived machine that gets re-run against, not a disposable instance.
#
# Two decisions carry the migration:
#
#   1. /opt/dhan-trading becomes a SYMLINK to this checkout. The systemd units,
#      config defaults (BACKFILL_CHECKPOINT_PATH), docs and cron lines all
#      hardcode that path; a symlink keeps every one of them correct without
#      editing anything, and `sudo git pull` still happens in one real place.
#   2. Credentials come from a hand-edited .env instead of SSM. The script
#      seeds it from .env.example and tells the operator exactly which keys to
#      fill; it never invents, fetches or rewrites a credential.
#
# Idempotent by construction: every step detects "already done" and says so.
# Safe to re-run forever — after a `git pull`, after a partial failure, or just
# to re-assert the machine state.
#
# What it does NOT do: start the trader. Units are enabled, not started (see
# --start). The first start is a validation decision, and against a template
# .env the trader would churn-restart on Dhan auth failures.
#
# Usage:
#   infra/scripts/setup_local.sh            # bootstrap, leave services stopped
#   infra/scripts/setup_local.sh --start    # …and start dhan-trader + dhan-api
#   infra/scripts/setup_local.sh --help
#
# Run as the normal login user (NOT root, NOT sudo) — it escalates per step.
#
set -euo pipefail

# ── Constants ────────────────────────────────────────────────────────────────
APP_LINK=/opt/dhan-trading                 # the path every unit/doc/cron hardcodes
LOG_DIR=/var/log/dhan
LOGROTATE_CONF=/etc/logrotate.d/dhan-trading
SYSTEMD_DIR=/etc/systemd/system
DB_CONTAINER=dhan-timescaledb              # docker-compose.yml: container_name
DB_SERVICE=timescaledb                     # docker-compose.yml: service name
DB_HEALTH_TIMEOUT=120                      # seconds to wait for "healthy"
EXPECTED_TZ=Asia/Kolkata                   # cron times below are IST-native
UNITS=(dhan-trader.service dhan-api.service "dhan-alert@.service")
ENABLE_UNITS=(dhan-trader dhan-api)        # dhan-alert@ is template-only

CRON_BEGIN='# >>> dhan-local (managed by infra/scripts/setup_local.sh) >>>'
CRON_END='# <<< dhan-local <<<'

START_SERVICES=0

# ── Logging ──────────────────────────────────────────────────────────────────
log()  { printf '%s  %s\n' "$(date '+%H:%M:%S')" "$*"; }
warn() { printf '%s  WARN  %s\n' "$(date '+%H:%M:%S')" "$*" >&2; }
die()  { printf '%s  ERROR %s\n' "$(date '+%H:%M:%S')" "$*" >&2; exit 1; }

DID=()
SKIPPED=()
FOLLOWUPS=()
did()      { DID+=("$1");      log "  [done] $1"; }
skipped()  { SKIPPED+=("$1");  log "  [skip] $1"; }
followup() { FOLLOWUPS+=("$1"); }

banner() {
    printf '\n════════════════════════════════════════════════════════════════════\n'
    printf '  %s\n' "$1"
    printf '════════════════════════════════════════════════════════════════════\n'
}

# ── Arguments ────────────────────────────────────────────────────────────────
while [ $# -gt 0 ]; do
    case "$1" in
        --start) START_SERVICES=1 ;;
        -h|--help)
            sed -n '2,/^set -euo/p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//; $d'
            exit 0
            ;;
        *) die "unknown argument: $1 (see --help)" ;;
    esac
    shift
done

# ═════════════════════════════════════════════════════════════════════════════
# 1. Preflight
# ═════════════════════════════════════════════════════════════════════════════
step_preflight() {
    banner "1/10  Preflight"

    [ "$(id -u)" -ne 0 ] || die "do not run this as root/sudo — run it as your login user; it escalates per step (files would end up root-owned otherwise)"

    if [ -r /etc/os-release ]; then
        # shellcheck disable=SC1091  # /etc/os-release is generated, not shipped
        . /etc/os-release
        if [ "${ID:-}" != "ubuntu" ]; then
            warn "expected Ubuntu, found '${PRETTY_NAME:-unknown}' — apt/systemd steps may not fit"
        else
            log "OS: ${PRETTY_NAME:-Ubuntu}"
        fi
    else
        warn "no /etc/os-release — cannot confirm the distro"
    fi

    REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
    TARGET_USER="${USER:-$(id -un)}"
    ENV_FILE="$REPO_ROOT/.env"

    # The username is spliced into a sed replacement and a systemd User= line.
    [[ "$TARGET_USER" =~ ^[a-z_][a-z0-9_-]*$ ]] \
        || die "refusing to template unit files with an unexpected username: '$TARGET_USER'"

    [ -f "$REPO_ROOT/requirements.txt" ] || die "no requirements.txt under $REPO_ROOT — is this the repo?"
    [ -f "$REPO_ROOT/docker-compose.yml" ] || die "no docker-compose.yml under $REPO_ROOT — is this the repo?"

    # A git worktree (agent sandbox, review checkout) is a real directory that
    # looks like the repo but disappears. Symlinking /opt at one would point the
    # live platform at a throwaway tree.
    if [ -f "$REPO_ROOT/.git" ]; then
        warn "$REPO_ROOT looks like a git WORKTREE, not the main checkout — /opt/dhan-trading would follow it. Re-run from the primary clone unless this is deliberate."
    fi

    command -v sudo >/dev/null 2>&1 || die "sudo is required (this script never runs wholly as root)"
    log "checking sudo access (may prompt) …"
    sudo -v || die "sudo authentication failed"

    log "repo root:   $REPO_ROOT"
    log "target user: $TARGET_USER"
}

# ═════════════════════════════════════════════════════════════════════════════
# 2. Packages — Python 3.12 toolchain + Docker
# ═════════════════════════════════════════════════════════════════════════════
pkg_installed() { dpkg-query -W -f='${Status}' "$1" 2>/dev/null | grep -q '^install ok installed$'; }

step_packages() {
    banner "2/10  Packages"

    local want=(python3.12 python3.12-venv python3.12-dev) missing=() p
    for p in "${want[@]}"; do
        pkg_installed "$p" || missing+=("$p")
    done

    if [ ${#missing[@]} -eq 0 ]; then
        skipped "apt packages ${want[*]}"
    else
        log "installing: ${missing[*]}"
        # `sudo env VAR=…` rather than `sudo VAR=… `: Ubuntu's sudoers runs with
        # env_reset and no SETENV, which rejects the bare-assignment form.
        sudo env DEBIAN_FRONTEND=noninteractive apt-get update -y
        sudo env DEBIAN_FRONTEND=noninteractive apt-get install -y "${missing[@]}"
        did "installed apt packages: ${missing[*]}"
    fi

    if command -v docker >/dev/null 2>&1; then
        skipped "docker ($(docker --version 2>/dev/null || echo 'version unknown'))"
    else
        # The convenience script is Docker's own; it adds the apt repo, installs
        # the engine + compose plugin and enables the daemon. It is the only
        # network-sourced installer here, and it runs ONLY when docker is absent.
        log "docker not found — installing via https://get.docker.com"
        command -v curl >/dev/null 2>&1 \
            || die "curl is needed to fetch the Docker installer (sudo apt-get install -y curl)"
        local tmp
        tmp="$(mktemp)"
        curl -fsSL https://get.docker.com -o "$tmp" || { rm -f "$tmp"; die "could not download the Docker install script"; }
        sudo sh "$tmp" || { rm -f "$tmp"; die "Docker installation failed"; }
        rm -f "$tmp"
        did "installed Docker Engine + Compose plugin"
    fi

    if id -nG "$TARGET_USER" 2>/dev/null | tr ' ' '\n' | grep -qx docker; then
        skipped "$TARGET_USER is in the docker group"
    else
        sudo usermod -aG docker "$TARGET_USER"
        did "added $TARGET_USER to the docker group"
        # Group membership is granted at login: this shell (and anything it
        # spawns) still lacks it, so the rest of the run uses `sudo docker`.
        warn "docker group takes effect at your NEXT login — this run keeps using 'sudo docker'"
        followup "log out and back in (or run 'newgrp docker') so cron's backup job can reach the Docker socket without sudo"
    fi
}

# Docker without sudo where possible, with sudo where necessary. Resolved once,
# after installation, so the group change above is honoured on later runs.
DOCKER_PREFIX=()
resolve_docker() {
    command -v docker >/dev/null 2>&1 || die "docker is not on PATH"
    if docker info >/dev/null 2>&1; then
        DOCKER_PREFIX=()
    elif sudo docker info >/dev/null 2>&1; then
        DOCKER_PREFIX=(sudo)
    else
        die "the Docker daemon is not responding (tried with and without sudo) — try 'sudo systemctl start docker'"
    fi
}
dk() { "${DOCKER_PREFIX[@]+"${DOCKER_PREFIX[@]}"}" docker "$@"; }

# ═════════════════════════════════════════════════════════════════════════════
# 3. /opt/dhan-trading symlink
# ═════════════════════════════════════════════════════════════════════════════
step_symlink() {
    banner "3/10  $APP_LINK symlink"

    if [ -L "$APP_LINK" ]; then
        local current
        current="$(readlink -f "$APP_LINK" || true)"
        if [ "$current" = "$(readlink -f "$REPO_ROOT")" ]; then
            skipped "$APP_LINK → $REPO_ROOT"
            return
        fi
        die "$APP_LINK is a symlink to '$current', not to '$REPO_ROOT'. Refusing to touch it — remove it yourself (sudo rm $APP_LINK) once you are sure nothing runs from there."
    fi

    if [ -e "$APP_LINK" ]; then
        die "$APP_LINK already exists and is NOT a symlink (probably the old EC2-style clone). Refusing to delete it. Move it aside (sudo mv $APP_LINK ${APP_LINK}.old) and re-run."
    fi

    sudo ln -s "$REPO_ROOT" "$APP_LINK"
    did "created $APP_LINK → $REPO_ROOT"
}

# ═════════════════════════════════════════════════════════════════════════════
# 4. Python virtualenv
# ═════════════════════════════════════════════════════════════════════════════
step_venv() {
    banner "4/10  Python virtualenv"

    local venv="$REPO_ROOT/.venv"
    if [ -x "$venv/bin/python" ]; then
        skipped "venv at $venv ($("$venv/bin/python" --version 2>&1))"
    else
        python3.12 -m venv "$venv" || die "could not create the venv (is python3.12-venv installed?)"
        "$venv/bin/python" -m pip install --upgrade --disable-pip-version-check pip
        did "created venv at $venv"
    fi

    # Always run: pip is the idempotent part, and a `git pull` may have moved
    # requirements.txt since the last bootstrap.
    log "installing requirements (no-op when already satisfied) …"
    "$venv/bin/python" -m pip install --disable-pip-version-check -r "$REPO_ROOT/requirements.txt" \
        || die "pip install -r requirements.txt failed"
    did "requirements.txt installed/verified"
}

# ═════════════════════════════════════════════════════════════════════════════
# 5. .env — the replacement for the SSM parameter fetch
# ═════════════════════════════════════════════════════════════════════════════
step_env() {
    banner "5/10  .env"

    if [ -f "$ENV_FILE" ]; then
        chmod 600 "$ENV_FILE" || warn "could not chmod 600 $ENV_FILE"
        skipped ".env exists (permissions re-asserted to 600)"

        # Never changed by this script — but the operator should know.
        if grep -Eq '^[[:space:]]*(export[[:space:]]+)?PAPER_TRADING=[[:space:]]*false' "$ENV_FILE"; then
            warn "$ENV_FILE has PAPER_TRADING=false — setup does not touch it, but this box is armed for LIVE order flow"
        fi
        return
    fi

    [ -f "$REPO_ROOT/.env.example" ] || die "no .env and no .env.example to seed it from"
    cp "$REPO_ROOT/.env.example" "$ENV_FILE"
    chmod 600 "$ENV_FILE"
    did "seeded .env from .env.example (mode 600)"

    cat <<BANNER

  ┌──────────────────────────────────────────────────────────────────────┐
  │  ACTION REQUIRED — .env is a TEMPLATE, not a configuration           │
  └──────────────────────────────────────────────────────────────────────┘

  On AWS these values came from SSM Parameter Store. There is no SSM here:
  edit $ENV_FILE by hand before starting anything.

  Fill in:
    DHAN_CLIENT_ID              your Dhan client id
    DHAN_ACCESS_TOKEN           seed/bootstrap token from dhanhq.co/developer
    DHAN_PIN                    enables automatic token refresh
    DHAN_TOTP_SECRET            base32 TOTP secret (with PIN, tokens self-rotate)
    TELEGRAM_BOT_TOKEN          alerts (health monitor, EOD, backup failures)
    TELEGRAM_CHAT_ID            alert destination

  Uncomment + fill ONLY if this box egresses through the Dhan proxy VM
  (i.e. its public IP is not the one whitelisted at Dhan):
    DHAN_PROXY_URL              http://<tailscale-ip-of-egress-vm>:8888
    DHAN_WHITELISTED_EGRESS_IP  the public IP Dhan has whitelisted
                                (read by scripts/egress_check.py only)

  Left alone deliberately:
    PAPER_TRADING=true          this script NEVER flips it; going live is an
                                explicit .env edit + ALLOW_LIVE_TOGGLE + restart
    DB_* defaults               they already match docker-compose.yml

BANNER
    followup "fill the credentials in $ENV_FILE (it is still the .env.example template)"
}

# ═════════════════════════════════════════════════════════════════════════════
# 6. Database — Compose container + Alembic schema
# ═════════════════════════════════════════════════════════════════════════════

# One KEY=VALUE lookup out of .env. Deliberately NOT `source`: .env is operator
# input containing secrets and shell metacharacters, and sourcing it would run
# whatever is in there and pollute this shell's environment wholesale.
# Matches python-dotenv/compose semantics closely enough for the DB_* keys:
# last assignment wins, optional `export `, quotes stripped, and for unquoted
# values a trailing ` #…` is a comment (as .env.example itself writes them).
env_get() {
    local key="$1" raw
    [ -f "$ENV_FILE" ] || return 1
    raw="$(sed -n -E "s/^[[:space:]]*(export[[:space:]]+)?${key}=(.*)\$/\2/p" "$ENV_FILE" | tail -n 1)"
    raw="${raw%$'\r'}"
    case "$raw" in
        '"'*'"') raw="${raw#\"}"; raw="${raw%\"}" ;;
        "'"*"'") raw="${raw#\'}"; raw="${raw%\'}" ;;
        *)       raw="$(printf '%s' "$raw" | sed -E 's/[[:space:]]+#.*$//; s/[[:space:]]+$//')" ;;
    esac
    [ -n "$raw" ] || return 1
    printf '%s' "$raw"
}

step_database() {
    banner "6/10  TimescaleDB + schema"

    resolve_docker
    cd "$REPO_ROOT"   # compose resolves docker-compose.yml relative to cwd

    local running
    running="$(dk inspect --format '{{.State.Running}}' "$DB_CONTAINER" 2>/dev/null || echo missing)"
    if [ "$running" = "true" ]; then
        skipped "container $DB_CONTAINER is running"
    else
        log "starting the $DB_SERVICE container …"
        dk compose up -d "$DB_SERVICE" || die "docker compose up -d $DB_SERVICE failed"
        did "started $DB_SERVICE"
    fi

    # First boot runs initdb + the TimescaleDB bootstrap; the healthcheck is TCP
    # so it only passes once the real server (not initdb's socket-only temporary
    # one) is accepting connections. Never migrate before this.
    log "waiting for $DB_CONTAINER to report healthy (timeout ${DB_HEALTH_TIMEOUT}s) …"
    local waited=0 status=""
    while [ "$waited" -lt "$DB_HEALTH_TIMEOUT" ]; do
        status="$(dk inspect --format '{{.State.Health.Status}}' "$DB_CONTAINER" 2>/dev/null || echo unknown)"
        [ "$status" = "healthy" ] && break
        sleep 3
        waited=$((waited + 3))
    done
    if [ "$status" != "healthy" ]; then
        warn "last health status: ${status:-unknown} after ${waited}s"
        die "the database never became healthy. Inspect it with: (cd $REPO_ROOT && docker compose logs --tail=50 $DB_SERVICE)"
    fi
    log "database healthy after ${waited}s"

    # alembic/env.py reads DB_* from the ENVIRONMENT (it never loads .env — that
    # is a config.py/pydantic feature and Alembic does not import config.py).
    # Defaults below mirror config.py's Config fields and the compose defaults.
    DB_HOST="$(env_get DB_HOST || true)";         export DB_HOST="${DB_HOST:-localhost}"
    DB_PORT="$(env_get DB_PORT || true)";         export DB_PORT="${DB_PORT:-5432}"
    DB_NAME="$(env_get DB_NAME || true)";         export DB_NAME="${DB_NAME:-dhan_trading}"
    DB_USER="$(env_get DB_USER || true)";         export DB_USER="${DB_USER:-trader}"
    DB_PASSWORD="$(env_get DB_PASSWORD || true)"; export DB_PASSWORD="${DB_PASSWORD:-trader123}"
    log "alembic target: ${DB_USER}@${DB_HOST}:${DB_PORT}/${DB_NAME}"

    local alembic="$REPO_ROOT/.venv/bin/alembic" current
    [ -x "$alembic" ] || die "$alembic missing — the venv step did not complete"

    # Invoke the console script, never `python -m alembic`: the repo has its own
    # alembic/ directory which shadows the installed package from the repo root.
    current="$("$alembic" current 2>/dev/null | tail -n 1 || true)"
    if [[ "$current" == *"(head)"* ]]; then
        skipped "schema already at head ($current)"
    else
        log "applying migrations (from: ${current:-empty database}) …"
        "$alembic" upgrade head || die "alembic upgrade head failed"
        did "schema migrated to head ($("$alembic" current 2>/dev/null | tail -n 1 || echo unknown))"
    fi
}

# ═════════════════════════════════════════════════════════════════════════════
# 7. Log directory + rotation
# ═════════════════════════════════════════════════════════════════════════════
step_logging() {
    banner "7/10  Logs + rotation"

    # The units write with StandardOutput=append:, so the directory must exist
    # and be writable by the service user before the first start.
    if [ -d "$LOG_DIR" ] && [ "$(stat -c '%U' "$LOG_DIR")" = "$TARGET_USER" ]; then
        skipped "$LOG_DIR exists and is owned by $TARGET_USER"
    else
        local group
        group="$(id -gn "$TARGET_USER")"   # not assumed equal to the username
        sudo mkdir -p "$LOG_DIR"
        sudo chown "$TARGET_USER:$group" "$LOG_DIR"
        did "created $LOG_DIR owned by $TARGET_USER:$group"
    fi

    # copytruncate: the trader/api hold their log FDs open for the life of the
    # process, so rotation must not rename out from under them.
    local tmp
    tmp="$(mktemp)"
    cat > "$tmp" <<'LOGROTATE_EOF'
/var/log/dhan/*.log {
    daily
    rotate 14
    compress
    delaycompress
    missingok
    notifempty
    copytruncate
}
LOGROTATE_EOF

    if sudo cmp -s "$tmp" "$LOGROTATE_CONF" 2>/dev/null; then
        skipped "logrotate config $LOGROTATE_CONF"
    else
        sudo install -m 644 -o root -g root "$tmp" "$LOGROTATE_CONF"
        did "wrote $LOGROTATE_CONF"
    fi
    rm -f "$tmp"
}

# ═════════════════════════════════════════════════════════════════════════════
# 8. systemd units
# ═════════════════════════════════════════════════════════════════════════════
step_systemd() {
    banner "8/10  systemd units"

    local changed=0 unit src dst tmp
    for unit in "${UNITS[@]}"; do
        src="$REPO_ROOT/infra/systemd/$unit"
        dst="$SYSTEMD_DIR/$unit"
        [ -f "$src" ] || die "missing canonical unit $src"

        # The ONLY edit at install time. The canonical units in the repo keep
        # User=ubuntu (their EC2 origin) and /opt/dhan-trading paths; the symlink
        # from step 3 makes the paths correct here, and this makes the user
        # correct. The repo copies stay untouched, so a `git pull` never fights
        # a local edit.
        tmp="$(mktemp)"
        sed "s/^User=ubuntu\$/User=${TARGET_USER}/" "$src" > "$tmp"

        if sudo cmp -s "$tmp" "$dst" 2>/dev/null; then
            skipped "$dst"
        else
            # A running service keeps its OLD unit definition until restarted —
            # daemon-reload alone does not re-exec it.
            if systemctl is-active --quiet "$unit" 2>/dev/null; then
                followup "restart ${unit%.service} — its unit file changed under a running process (sudo systemctl restart ${unit%.service})"
            fi
            sudo install -m 644 -o root -g root "$tmp" "$dst"
            did "installed $dst (User=$TARGET_USER)"
            changed=1
        fi
        rm -f "$tmp"
    done

    if [ "$changed" -eq 1 ]; then
        sudo systemctl daemon-reload
        did "systemctl daemon-reload"
    else
        skipped "daemon-reload (no unit changed)"
    fi

    local svc
    for svc in "${ENABLE_UNITS[@]}"; do
        if [ "$(systemctl is-enabled "$svc" 2>/dev/null || true)" = "enabled" ]; then
            skipped "$svc enabled"
        else
            sudo systemctl enable "$svc"
            did "enabled $svc (start on boot)"
        fi
    done

    # Deliberately NOT started by default:
    #   • the first start is a validation decision, made once the operator has
    #     read the .env banner and filled in real credentials;
    #   • with the template .env the trader fails Dhan auth on boot and
    #     Restart=on-failure churns it against the API until StartLimitBurst
    #     trips — five failed auth attempts nobody asked for.
    if [ "$START_SERVICES" -eq 1 ]; then
        for svc in "${ENABLE_UNITS[@]}"; do
            if systemctl is-active --quiet "$svc"; then
                skipped "$svc already running"
            else
                sudo systemctl start "$svc"
                did "started $svc"
            fi
        done
    else
        log "  [note] services enabled but NOT started (pass --start to start them now)"
        followup "start the services when the .env is real: sudo systemctl start ${ENABLE_UNITS[*]}"
    fi
}

# ═════════════════════════════════════════════════════════════════════════════
# 9. cron
# ═════════════════════════════════════════════════════════════════════════════
step_cron() {
    banner "9/10  cron"

    # All times below are LOCAL to this machine. The retired AWS agent ran UTC
    # and its crontab carried UTC times (11:15 UTC = 16:45 IST); these are the
    # IST-native equivalents, which are only correct if the box really is IST.
    local block
    block="$(cat <<CRON_EOF
$CRON_BEGIN
# Times are IST (Asia/Kolkata) — this box's timezone. The retired AWS agent ran
# UTC, so its equivalents were 5h30m earlier (16:45 IST was 11:15 UTC).
# Regenerated by setup_local.sh on every run; edit the script, not this block.
*/5 * * * * cd $APP_LINK && .venv/bin/python scripts/health_alert.py >> $LOG_DIR/health_alert.log 2>&1
45 16 * * 1-5 cd $APP_LINK && .venv/bin/python -m ml.calibration fill >> $LOG_DIR/calibration.log 2>&1 && .venv/bin/python -m ml.calibration report >> $LOG_DIR/calibration.log 2>&1
0 17 * * 1-5 cd $APP_LINK && .venv/bin/python scripts/eod_summary.py >> $LOG_DIR/eod_summary.log 2>&1
0 9 * * 1-5 cd $APP_LINK && .venv/bin/python scripts/egress_check.py >> $LOG_DIR/egress_check.log 2>&1
30 2 * * * cd $APP_LINK && scripts/backup_db.sh >> $LOG_DIR/backup.log 2>&1
$CRON_END
CRON_EOF
)"

    local tz
    tz="$(timedatectl show --property=Timezone --value 2>/dev/null || true)"
    [ -n "$tz" ] || tz="$(cat /etc/timezone 2>/dev/null || true)"
    [ -n "$tz" ] || tz="unknown"

    if [ "$tz" != "$EXPECTED_TZ" ]; then
        warn "system timezone is '$tz', not $EXPECTED_TZ — cron would fire these at the wrong market times"
        cat <<NOTE

  NOTHING was installed into the crontab. The intended schedule (IST) is:

$block

  Either set the box to IST:   sudo timedatectl set-timezone $EXPECTED_TZ
  then re-run this script; or install the block yourself with the times shifted
  into $tz (or with a CRON_TZ=$EXPECTED_TZ line above it, which vixie-cron honours).

NOTE
        skipped "cron install (timezone is $tz, not $EXPECTED_TZ)"
        followup "install the cron block by hand, or set the timezone to $EXPECTED_TZ and re-run"
        return
    fi

    command -v crontab >/dev/null 2>&1 \
        || die "crontab not found — install cron first (sudo apt-get install -y cron)"

    local current remainder new
    current="$(crontab -l 2>/dev/null || true)"

    # Drop the previously managed block (if any) and re-append a fresh one:
    # re-runs update the lines instead of duplicating them, and anything the
    # operator added outside the markers is preserved untouched.
    remainder="$(printf '%s\n' "$current" | awk -v s="$CRON_BEGIN" -v e="$CRON_END" '
        $0 == s { inblock = 1; next }
        $0 == e { inblock = 0; next }
        !inblock { print }
    ')"

    if printf '%s\n' "$remainder" | grep -qF -- "$APP_LINK"; then
        warn "crontab has dhan lines OUTSIDE the managed block (likely from the AWS crontab) — review 'crontab -l' for duplicates"
        followup "remove leftover pre-migration cron lines referencing $APP_LINK (they run in addition to the managed block)"
    fi

    if [ -n "$remainder" ]; then
        new="$remainder"$'\n'"$block"
    else
        new="$block"
    fi

    if [ "$new" = "$current" ]; then
        skipped "crontab already carries the managed block"
    else
        printf '%s\n' "$new" | crontab - || die "could not install the crontab"
        did "installed 5 managed cron jobs for $TARGET_USER (IST)"
    fi
}

# ═════════════════════════════════════════════════════════════════════════════
# 10. Summary
# ═════════════════════════════════════════════════════════════════════════════
step_summary() {
    banner "10/10  Summary"

    local item n
    printf '\nChanged (%d):\n' "${#DID[@]}"
    if [ "${#DID[@]}" -eq 0 ]; then
        printf '  (nothing — the machine was already bootstrapped)\n'
    else
        for item in "${DID[@]}"; do printf '  [done] %s\n' "$item"; done
    fi

    printf '\nAlready in place (%d):\n' "${#SKIPPED[@]}"
    for item in ${SKIPPED[@]+"${SKIPPED[@]}"}; do printf '  [skip] %s\n' "$item"; done

    printf '\nNEXT STEPS (manual, in order):\n'
    n=1
    for item in ${FOLLOWUPS[@]+"${FOLLOWUPS[@]}"}; do
        printf '  %d. %s\n' "$n" "$item"
        n=$((n + 1))
    done
    printf '  %d. verify:\n' "$n"
    cat <<SUMMARY
       systemctl status dhan-trader dhan-api
       curl -s localhost:8765/api/status | head
       (cd $REPO_ROOT && docker compose ps)
       $REPO_ROOT/.venv/bin/alembic current
       tail -f $LOG_DIR/trader.log

  The dashboard listens on :8765 (API_BIND_HOST defaults to 0.0.0.0, i.e. also
  reachable over the tailnet). PAPER_TRADING is untouched by this script.
  Re-running setup_local.sh is safe at any time.

SUMMARY
}

# ═════════════════════════════════════════════════════════════════════════════
main() {
    step_preflight
    step_packages
    step_symlink
    step_venv
    step_env
    step_database
    step_logging
    step_systemd
    step_cron
    step_summary
}

main "$@"
