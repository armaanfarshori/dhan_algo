# M6 — Auth Layer Design

**Status:** Design only — no code yet. Implement before M8 (tiny live).
**Author:** 2026-06-16
**Supersedes:** SEC-04 stopgap (`DASHBOARD_TOKEN` shared-secret, `_check_auth` in `apps/api.py`)

---

## 1. Threat Model

### What we are protecting

The dashboard exposes a control surface reachable over Tailscale (and currently bound to `0.0.0.0:8765`). Two endpoints mutate live state:

| Endpoint | Effect | Current protection |
|---|---|---|
| `POST /api/killswitch` | Writes `run/killswitch`, halts trading within ~10 s | SEC-04 `DASHBOARD_TOKEN` (fail-open if unset) |
| `POST /api/watchlist/refresh` | Replaces the live screener candidates | SEC-04 `DASHBOARD_TOKEN` (fail-open if unset) |
| `POST /api/mode` | Returns 409 unconditionally (disabled until M6) | Structural (no-op) |
| `POST /postback` | Dhan order-status webhook | SEC-09 HMAC signature on body |

Read endpoints leak operational data (positions, P&L, trade history, Dhan funds, account ID suffix). They do not directly move money but would assist targeted attacks or front-running if exposed to the internet.

### Attacker profiles

1. **Unauthenticated internet scanner** — the API is bound to `0.0.0.0`; if the Tailscale firewall misconfigures or the instance gets a public IP, the dashboard is exposed. Without auth, anyone who finds port 8765 can trigger the kill-switch or drain position data.
2. **Tailscale network participant** — any device added to the Tailscale network can reach the dashboard. The tailnet is controlled, but a compromised device inside it has full dashboard access today.
3. **Repo reader** — the repo is public. The SEC-04 `DASHBOARD_TOKEN` is in `.env` (not committed), but its role and header name (`X-Dashboard-Token`) are visible in the code. An attacker who obtains the token value (e.g., from a leaked `.env`) can impersonate the operator.
4. **Self (accidental)** — the operator triggers the kill-switch from the wrong browser tab, or a script sends a stale token. The fail-open default (token unset → allow) was chosen so a misconfigured secret never locks the operator out of the kill-switch; M6 must preserve this escape hatch explicitly.

### What M6 is NOT defending against

- A compromised agent EC2 host (OS-level attacker) — that is an infra/IAM problem.
- Dhan API abuse from a stolen Dhan token — that is Dhan's problem.
- Loss of `.env` credentials to the public repo — git-guardian + repo scanning is the control; never the auth layer.

---

## 2. What Migration 003 Already Provides

`alembic/versions/003_auth_tables.py` (created 2026-06-03) creates five tables:

| Table | Purpose |
|---|---|
| `users` | username, Argon2id password hash, role (`viewer`/`operator`), lockout state |
| `sessions` | server-side session store; `session_token_hash` (SHA-256 of cookie value), expiry, device fingerprint, IP, `elevated` flag for step-up re-auth |
| `mfa_credentials` | WebAuthn passkey or TOTP secret (AES-256-GCM encrypted, key in SSM) |
| `recovery_codes` | Single-use, SHA-256-hashed recovery codes |
| `auth_events` | Append-only audit log: login_ok/fail, mfa_ok/fail, lockout, kill_switch_on/off, mode_change |

The schema is genuinely well-designed for a multi-user, MFA-enabled system. It is significantly more than needed for a solo operator. However, it is already in the DB and migration 003 is applied; we should use the `users`, `sessions`, and `auth_events` tables and simply ignore `mfa_credentials` and `recovery_codes` in Phase 1.

---

## 3. Recommended Auth Model

### Decision: server-side session with a signed Bearer token; single operator account; no MFA in Phase 1

**Rationale for "simple over full":** This is a single-operator platform. There will never be a second user. The migration-003 schema is capable of MFA + WebAuthn, but implementing WebAuthn browser flows, passkey enrollment, TOTP, and recovery codes before M8 would take weeks and add surface area. The threat model for a Tailscale-only dashboard with one operator is materially different from a public SaaS.

**What "simple" means concretely:**

1. **One operator account** seeded via a `python -m scripts.create_operator` CLI (not an open registration endpoint). Argon2id hash stored in `users`. Role fixed at `operator`.
2. **Login: `POST /api/auth/login`** — username + password → on success, generate a 256-bit random session token, store SHA-256 in `sessions.session_token_hash`, return the raw token in a `Set-Cookie: session=<token>; HttpOnly; SameSite=Strict; Secure` (when behind TLS) or as a JSON bearer value the dashboard caches in memory (not localStorage).
3. **Auth check on every request:** extract the raw token from the `Authorization: Bearer <token>` header or `session` cookie → SHA-256 → look up in `sessions` (not revoked, not expired) → populate `request["user"]`. Fail with 401 otherwise.
4. **Token lifetime:** 8 hours (a single trading day). The dashboard re-prompts on 401 (no background refresh needed).
5. **Audit every mutating action** in `auth_events`: `kill_switch_on`, `watchlist_refresh`. This uses the existing table already in the DB.
6. **Kill-switch escape hatch preserved:** if `DASHBOARD_TOKEN` is still set (non-empty), the old `_check_auth` path continues to work as a bypass. This means an operator locked out of the session store can still hit the kill-switch with the shared secret. Remove the bypass in Phase 2 once sessions are confirmed stable.

**Why not JWT:** A server-side session is stored in the DB so it can be revoked immediately (critical for kill-switch access — you want to be able to invalidate a stolen credential). JWTs without a revocation list cannot do this. The DB is already there and the `sessions` table exists.

**Why not WebAuthn/TOTP in Phase 1:** The schema supports it, but the platform is Tailscale-only today. If the dashboard stays behind Tailscale (recommended, see §6), the network provides a second factor by itself. Add TOTP in Phase 2 if internet exposure is chosen.

---

## 4. Endpoint AuthN/AuthZ Map

### After M6

| Endpoint | Method | AuthN required | AuthZ | Notes |
|---|---|---|---|---|
| `POST /api/auth/login` | POST | No | — | Returns session token; rate-limited (5 attempts/15 min/IP) |
| `POST /api/auth/logout` | POST | Yes | any | Revokes session in DB |
| `GET /health` | GET | No | — | Liveness probe; no sensitive data |
| `GET /api/snapshot` | GET | Yes | viewer | File read only but exposes positions/mode |
| `GET /api/status` | GET | Yes | viewer | Heartbeat + strategy state |
| `GET /api/risk` | GET | Yes | viewer | P&L, halt state |
| `GET /api/trades` | GET | Yes | viewer | Trade history with P&L |
| `GET /api/signals` | GET | Yes | viewer | Entry/exit signal log |
| `GET /api/funds` | GET | Yes | operator | Calls Dhan funds API — account data |
| `GET /api/positions` | GET | Yes | operator | Calls Dhan positions API |
| `GET /api/equity` | GET | Yes | viewer | Intraday P&L curve |
| `GET /api/kronos/*` | GET | Yes | viewer | Gate verdicts + calibration |
| `GET /api/logs` | GET | Yes | operator | Raw trader log lines |
| `GET /api/db/stats` | GET | Yes | operator | DB internals |
| `GET /api/config` | GET | Yes | viewer | Watchlist + gate mode |
| `GET /api/mode` | GET | Yes | viewer | Paper/live mode |
| `POST /api/mode` | POST | Yes (operator) | operator | Still returns 409 (structural); auth added for correctness |
| `POST /api/killswitch` | POST | Yes (operator) | operator | Also accepts `DASHBOARD_TOKEN` bypass (§3) |
| `POST /api/watchlist/refresh` | POST | Yes (operator) | operator | — |
| `GET /api/watchlist` | GET | Yes | viewer | — |
| `GET /api/market` | GET | No | — | No sensitive data |
| `GET /api/backfill/status` | GET | Yes | operator | Checkpoint + log path exposed |
| `GET /api/system/health` | GET | Yes | viewer | Cron schedule + error count |
| `POST /postback` | POST | HMAC (SEC-09) | — | Dhan webhook; separate auth path |
| `GET /` (dashboard SPA) | GET | No | — | React bundle is public; gated in JS |
| `GET /assets/*` | GET | No | — | Content-hashed static files |

**Role definitions:**
- `viewer` — read any endpoint marked viewer; cannot mutate state or call Dhan write APIs.
- `operator` — all endpoints. Only role seeded in Phase 1 (no viewer accounts exist yet).

---

## 5. How the Dashboard Stores and Sends the Credential

The dashboard (React 18, Vite) currently sends `X-Dashboard-Token` from an env-baked value. After M6:

1. **Login screen:** rendered by the SPA when `GET /api/snapshot` returns 401. Simple username + password form, `POST /api/auth/login`, receives `{ token: "..." }`.
2. **Token storage:** stored in memory (React state / React context). NOT `localStorage` (survives XSS persistence) and NOT `sessionStorage` (clears on tab close but still accessible to XSS). In-memory means a page refresh requires re-login — acceptable for an operator dashboard. The 8-hour window covers a trading session.
3. **Sent on every request:** as `Authorization: Bearer <token>` header via the existing `fetch` calls. The `X-Dashboard-Token` header is kept as an alias for backward compatibility during the transition (removed in Phase 2).
4. **401 handling:** any endpoint returning 401 clears the in-memory token and shows the login screen. No retry loops.

---

## 6. Network Exposure and TLS

### Recommendation: keep Tailscale-only, add TLS via Tailscale HTTPS

The current `api_bind_host = "0.0.0.0"` binds on all interfaces including any public ENI. This is mitigated by AWS Security Group rules, but is a misconfiguration risk.

**Phase 1 change:** Set `API_BIND_HOST=127.0.0.1` in agent `.env`, rely on `~/Desktop/dhan_aws_access/connect.sh dashboard` SSH tunnel for operator access. This is the lowest-risk option and costs zero.

**Alternative (recommended for M8+):** Leave bound to `0.0.0.0` but restrict the AWS Security Group to only the Tailscale CIDR (`100.64.0.0/10`). Enable Tailscale HTTPS (MagicDNS cert) so the session cookie can carry `Secure`. No certificate management needed — Tailscale handles it.

**Not recommended for Phase 1:** Exposing the dashboard to the public internet, even with auth, adds unnecessary attack surface before live trading is active.

**TLS implication for cookies:** The `Secure` cookie flag requires HTTPS. Without Tailscale HTTPS, use the JSON bearer token approach (§5) instead of `HttpOnly` cookies — bearer tokens work over plain HTTP but are more exposed to JS. Over Tailscale's encrypted transport, either is acceptable.

---

## 7. How M6 Supersedes the SEC-04 Stopgap

Current `_check_auth(request)` in `apps/api.py`:
- Checks `X-Dashboard-Token` or `Authorization: Bearer <token>` against `cfg.dashboard_token`.
- Fail-open: if `dashboard_token` is empty, allows all requests.

After M6:
- Replace `_check_auth` with a session middleware that validates against `sessions` table.
- Keep `DASHBOARD_TOKEN` bypass as an explicit escape hatch for kill-switch (§3, Phase 1 only).
- Remove the fail-open path for all endpoints except killswitch (which retains the bypass).
- Log every auth failure to `auth_events`.

The `cors_middleware` in `apps/api.py` must keep `Authorization` in `Access-Control-Allow-Headers` (already present, line 77).

---

## 8. Phased Implementation Plan

### Phase 1 — Minimum viable auth before M8 (implement now)

1. **Bind to localhost:** Set `API_BIND_HOST=127.0.0.1` in agent `.env`, restart `dhan-api`. Operators access via the existing SSH tunnel. Eliminates the `0.0.0.0` exposure immediately with zero code change.
2. **Seed the operator account:** `python -m scripts.create_operator` — reads `OPERATOR_USERNAME` and `OPERATOR_PASSWORD` from `.env`, inserts one `users` row with Argon2id hash. No endpoint to create users.
3. **Add `POST /api/auth/login` and `POST /api/auth/logout`:** Session creation (generate 256-bit token, store SHA-256 in `sessions`), session revocation.
4. **Add session middleware:** Replace `_check_auth` with a DB-backed session lookup. Protect all endpoints in the map above. Keep `DASHBOARD_TOKEN` bypass for killswitch only.
5. **Dashboard login screen:** Minimal React login form, in-memory token storage, 401 redirect.
6. **Audit logging:** Write `login_ok`, `login_fail`, `kill_switch_on` events to `auth_events`.
7. **Rate-limit `/api/auth/login`:** 5 failures per 15 minutes per IP → 429. Prevents brute-force. Use the existing `RateLimiter` pattern or a simple in-memory counter (acceptable for one IP: the operator).

### Phase 2 — Hardening (before meaningful capital)

1. **Remove `DASHBOARD_TOKEN` bypass.** By this point sessions are confirmed stable.
2. **Add TOTP** using `mfa_credentials` table (already in DB). Use `pyotp`. Present TOTP on login after password. Recovery via `recovery_codes`. This brings the schema to full utilization.
3. **Enable Tailscale HTTPS** — MagicDNS cert, `Secure` cookie flag, rebind to Tailscale interface.
4. **Session expiry sweep:** Background task or on-login cleanup removes expired rows from `sessions`.
5. **Viewer role:** Create a second account with `role=viewer` for read-only dashboard access from non-operator devices.

### Phase 3 — Internet exposure (if ever needed)

- Reverse proxy (nginx/caddy) with Let's Encrypt TLS in front of `dhan-api`.
- Add WebAuthn (`mfa_credentials` table already has the columns) using `py_webauthn`.
- Move secrets (Argon2 pepper, session HMAC key) to AWS Secrets Manager instead of `.env`.

---

## 9. Key Decisions Summary

| Decision | Choice | Rationale |
|---|---|---|
| Auth model | Server-side session (DB-stored SHA-256 of token) | Revocable; DB already exists; simpler than JWT |
| Credential storage (dashboard) | In-memory React state | No XSS persistence; acceptable UX for a trading session |
| MFA in Phase 1 | No | Tailscale provides network-layer second factor; TOTP in Phase 2 |
| Network exposure | Tailscale-only (localhost bind + SSH tunnel) | Eliminates public exposure with zero code |
| Kill-switch escape hatch | `DASHBOARD_TOKEN` bypass retained in Phase 1 | Operator must never be locked out of halt |
| Unused schema tables | `mfa_credentials`, `recovery_codes` ignored Phase 1 | Used in Phase 2 (TOTP) and Phase 3 (WebAuthn) |
| Role model | One operator role in Phase 1 | Single operator; viewer accounts deferred |
