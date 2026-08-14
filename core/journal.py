"""
Journal — unified logging layer for DhanAIBot
==============================================
Consolidates previous modules into one clear API:

  AsyncDBBackend— async TimescaleDB sink for signals + trades
  LogBuffer     — rolling in-memory buffer for /api/logs endpoint

Usage:
    # Singletons
    from core.journal import get_log_buffer, install_log_buffer

    install_log_buffer()   # call once at startup to capture all logs
    logs = get_log_buffer().get_logs(50)
"""

import asyncio
import json
import logging
from collections import deque
from datetime import datetime, timezone
from typing import Optional

_log = logging.getLogger("dhan.journal")


# ══════════════════════════════════════════════════════════════════════════════
# AsyncDBBackend — TimescaleDB sink for signals + trades
# ══════════════════════════════════════════════════════════════════════════════

class AsyncDBBackend:
    """
    Optional async companion to TradeLogger.
    Writes signals and trades to TimescaleDB alongside the JSON-lines file.
    Fails silently if DB is unavailable — never blocks the trading loop.

    Engine ownership: AsyncDBBackend reuses db.py's shared engine/pool
    (pool_size=5, max_overflow=10).  It never creates its own engine in
    production — connect() calls db.init_db() if needed, then piggybacks on
    db.get_session() / db.get_engine().  This caps the trader's total
    connections to the shared pool rather than opening a second pool (~3+5=8
    extra connections against the 2 GB DB box).

    Test-override: tests may set _engine / _Session directly after construction
    (e.g. to inject a SQLite engine).  _exec() and _ping() honour those
    overrides when present, falling back to db.get_session()/get_engine()
    otherwise.
    """

    def __init__(self):
        from config import get_config
        # Explicit switch (JOURNAL_DB_ENABLED) — the old "localhost means dev,
        # disable" heuristic silently killed journalling on a single-host deploy
        # where localhost IS the production DB, starving RiskEngine's realized-
        # loss meters (refresh_pnl reads trades). An empty DB_HOST still
        # disables: there is nothing to connect to. Boxes without a DB rely on
        # connect()'s fail-silent path, not on this predicate.
        cfg = get_config()
        self._enabled = cfg.journal_db_enabled and cfg.db_host != ""
        # _engine / _Session: None in production (db.py's shared pool is used);
        # may be set by tests to inject an alternate engine.
        self._engine  = None
        self._Session = None

    async def connect(self):
        if not self._enabled:
            _log.info(
                "AsyncDBBackend: journalling disabled "
                "(JOURNAL_DB_ENABLED=false or DB_HOST empty)"
            )
            return
        try:
            import db as _db
            from config import get_config
            # Ensure the shared engine is initialised (idempotent if already done
            # by apps/trader.py's startup sequence).
            if _db._engine is None:
                _db.init_db(get_config().db_url)
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, self._ping)
            _log.info("AsyncDBBackend: connected to TimescaleDB (shared engine)")
        except Exception as exc:
            _log.warning("AsyncDBBackend: connect failed (%s) — DB journalling disabled", exc)
            self._enabled = False

    def _ping(self):
        from sqlalchemy import text
        import db as _db
        engine = self._engine if self._engine is not None else _db.get_engine()
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))

    def _exec(self, sql: str, params: dict):
        from sqlalchemy import text
        if self._Session is not None:
            # Test-injected session factory — use manual lifecycle to match
            # the original behaviour expected by the test harness.
            session = self._Session()
            try:
                session.execute(text(sql), params)
                session.commit()
            except Exception:
                session.rollback()
                raise
            finally:
                session.close()
        else:
            import db as _db
            with _db.get_session() as session:
                session.execute(text(sql), params)

    async def _run(self, sql: str, params: dict):
        if not self._enabled:
            return
        try:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, self._exec, sql, params)
        except Exception as exc:
            _log.warning("AsyncDBBackend write error: %s", exc)

    async def log_signal(self, security_id: str, side: str, strategy: str,
                         score: float = 0.0, confidence: float = 0.0,
                         features: Optional[dict] = None) -> None:
        await self._run("""
            INSERT INTO signals (ts, security_id, side, score, confidence, strategy, features_snapshot)
            VALUES (:ts, :security_id, :side, :score, :confidence, :strategy, :features)
        """, {
            "ts": datetime.now(timezone.utc), "security_id": security_id,
            "side": side, "score": score, "confidence": confidence,
            "strategy": strategy, "features": json.dumps(features or {}),
        })

    async def log_trade_entry(self, security_id: str, side: str, qty: int,
                              entry_price: float, strategy: str,
                              signal_id: Optional[int] = None,
                              dhan_order_id: Optional[str] = None) -> Optional[int]:
        """Insert a new OPEN trade row and return its id, or None if disabled/error."""
        if not self._enabled:
            return None
        trade_id: list[Optional[int]] = [None]

        def _insert():
            from sqlalchemy import text as _text
            params = {
                "signal_id": signal_id, "security_id": security_id, "side": side,
                "qty": qty, "entry_ts": datetime.now(timezone.utc),
                "entry_price": entry_price, "strategy": strategy, "order_id": dhan_order_id,
            }
            sql = """
                INSERT INTO trades (signal_id, security_id, side, qty, entry_ts, entry_price, strategy, dhan_order_id, status)
                VALUES (:signal_id, :security_id, :side, :qty, :entry_ts, :entry_price, :strategy, :order_id, 'OPEN')
                RETURNING id
            """
            if self._Session is not None:
                session = self._Session()
                try:
                    result = session.execute(_text(sql), params)
                    trade_id[0] = result.scalar()
                    session.commit()
                except Exception:
                    session.rollback()
                    raise
                finally:
                    session.close()
            else:
                import db as _db
                with _db.get_session() as session:
                    result = session.execute(_text(sql), params)
                    trade_id[0] = result.scalar()

        try:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, _insert)
        except Exception as exc:
            _log.warning("AsyncDBBackend log_trade_entry error: %s", exc)
            return None
        return trade_id[0]

    async def log_trade_exit(self, security_id: str, exit_price: float, pnl: float,
                             dhan_order_id: Optional[str] = None,
                             trade_id: Optional[int] = None) -> None:
        # When trade_id is provided (the normal path after DATA-03), close that
        # exact row — no ambiguity even if two entries for the same security are
        # open concurrently (e.g. during a flip).  When trade_id is absent (boot
        # reconcile or legacy callers), fall back to the ORDER BY heuristic that
        # closes the most-recent matching open row.
        if trade_id is not None:
            sql = """
                UPDATE trades SET exit_ts=:exit_ts, exit_price=:exit_price, pnl=:pnl, status='CLOSED'
                WHERE id = :trade_id AND status='OPEN'
            """
            params: dict = {
                "exit_ts": datetime.now(timezone.utc), "exit_price": exit_price,
                "pnl": pnl, "trade_id": trade_id,
            }
        else:
            # Fallback heuristic: close the most recent open row for this
            # security (optionally scoped to a specific order_id).  Kept for
            # back-compat with reconcile paths that have no in-memory trade_id.
            sql = """
                UPDATE trades SET exit_ts=:exit_ts, exit_price=:exit_price, pnl=:pnl, status='CLOSED'
                WHERE id = (
                    SELECT id FROM trades
                    WHERE security_id=:security_id AND status='OPEN'
                      AND (:order_id IS NULL OR dhan_order_id=:order_id)
                    ORDER BY entry_ts DESC, id DESC
                    LIMIT 1
                )
            """
            params = {
                "exit_ts": datetime.now(timezone.utc), "exit_price": exit_price,
                "pnl": pnl, "security_id": security_id, "order_id": dhan_order_id,
            }
        await self._run(sql, params)
        today = datetime.now(timezone.utc).date()
        await self._run("""
            INSERT INTO daily_pnl (date, realized_pnl, trades_count, wins, losses)
            VALUES (:date, :pnl, 1, :win, :loss)
            ON CONFLICT (date) DO UPDATE SET
                realized_pnl = daily_pnl.realized_pnl + EXCLUDED.realized_pnl,
                trades_count = daily_pnl.trades_count + 1,
                wins         = daily_pnl.wins  + EXCLUDED.wins,
                losses       = daily_pnl.losses + EXCLUDED.losses
        """, {"date": today, "pnl": pnl, "win": 1 if pnl >= 0 else 0, "loss": 1 if pnl < 0 else 0})

    # ── Orders + fills ─────────────────────────────────────────────────────────

    async def log_order(
        self,
        security_id: str,
        exchange_segment: str,
        side: str,
        qty: int,
        order_type: str,
        product_type: str,
        mode: str,
        price: float = 0.0,
        dhan_order_id: Optional[str] = None,
        client_order_id: Optional[str] = None,
        signal_id: Optional[int] = None,
        run_id: Optional[int] = None,
        status: str = "PENDING",
    ) -> None:
        await self._run("""
            INSERT INTO orders
              (run_id, signal_id, dhan_order_id, client_order_id, created_at,
               security_id, exchange_segment, side, qty,
               order_type, product_type, price, mode, status)
            VALUES
              (:run_id, :signal_id, :dhan_order_id, :client_order_id, :ts,
               :security_id, :exchange_segment, :side, :qty,
               :order_type, :product_type, :price, :mode, :status)
        """, {
            "run_id": run_id, "signal_id": signal_id,
            "dhan_order_id": dhan_order_id, "client_order_id": client_order_id,
            "ts": datetime.now(timezone.utc),
            "security_id": security_id, "exchange_segment": exchange_segment,
            "side": side, "qty": qty, "order_type": order_type,
            "product_type": product_type, "price": price,
            "mode": mode, "status": status,
        })

    async def log_order_event(
        self,
        dhan_order_id: str,
        status: str,
        raw_payload: Optional[dict] = None,
    ) -> None:
        """Append a lifecycle event to order_events (append-only)."""
        await self._run("""
            INSERT INTO order_events (order_id, time, status, raw_payload)
            SELECT id, :ts, :status, :payload
            FROM orders WHERE dhan_order_id = :oid
            LIMIT 1
        """, {
            "ts": datetime.now(timezone.utc), "status": status,
            "payload": json.dumps(raw_payload or {}), "oid": dhan_order_id,
        })

    async def log_fill(
        self,
        dhan_order_id: str,
        qty: int,
        price: float,
        fees: float = 0.0,
        taxes: float = 0.0,
    ) -> None:
        await self._run("""
            INSERT INTO fills (order_id, time, qty, price, fees, taxes)
            SELECT id, :ts, :qty, :price, :fees, :taxes
            FROM orders WHERE dhan_order_id = :oid
            LIMIT 1
        """, {
            "ts": datetime.now(timezone.utc), "qty": qty, "price": price,
            "fees": fees, "taxes": taxes, "oid": dhan_order_id,
        })

    # ── Position + equity snapshots ────────────────────────────────────────────

    async def snapshot_positions(self, positions: list[dict]) -> None:
        """Write a periodic snapshot of open positions to the positions hypertable."""
        ts = datetime.now(timezone.utc)
        for pos in positions:
            qty = pos.get("netQty", 0)
            if qty == 0:
                continue
            await self._run("""
                INSERT INTO positions
                  (time, security_id, qty, avg_price, ltp, unrealized_pnl, product_type)
                VALUES
                  (:ts, :sid, :qty, :avg, :ltp, :upnl, :prod)
                ON CONFLICT (security_id, time) DO NOTHING
            """, {
                "ts":   ts,
                "sid":  str(pos.get("securityId", pos.get("security_id", ""))),
                "qty":  qty,
                "avg":  pos.get("buyAvg", pos.get("avgPrice", 0)),
                "ltp":  pos.get("lastTradedPrice", pos.get("ltp", 0)),
                "upnl": pos.get("unrealisedProfit", pos.get("unrealizedProfit", 0)),
                "prod": pos.get("productType", "INTRADAY"),
            })

    async def snapshot_equity(
        self,
        cash: float,
        holdings_value: float,
        realized_pnl: float,
        unrealized_pnl: float,
        drawdown: float = 0.0,
    ) -> None:
        total = cash + holdings_value
        await self._run("""
            INSERT INTO equity_curve
              (time, cash, holdings_value, total_equity,
               realized_pnl, unrealized_pnl, drawdown)
            VALUES
              (:ts, :cash, :hv, :total, :rpnl, :upnl, :dd)
            ON CONFLICT (time) DO NOTHING
        """, {
            "ts": datetime.now(timezone.utc), "cash": cash, "hv": holdings_value,
            "total": total, "rpnl": realized_pnl, "upnl": unrealized_pnl, "dd": drawdown,
        })

    # ── Session runs ───────────────────────────────────────────────────────────

    async def log_run_start(self, mode: str, strategy: str) -> Optional[int]:
        """Create a new run record. Returns run_id for linking orders/signals."""
        if not self._enabled:
            return None
        import subprocess
        try:
            git_sha = subprocess.check_output(
                ["git", "rev-parse", "--short", "HEAD"], stderr=subprocess.DEVNULL
            ).decode().strip()
        except Exception:
            git_sha = "unknown"
        run_id = [None]

        def _insert():
            from sqlalchemy import text
            if self._Session is not None:
                # Test-injected session factory.
                session = self._Session()
                try:
                    result = session.execute(text("""
                        INSERT INTO runs (started_at, mode, strategy, version, outcome)
                        VALUES (:ts, :mode, :strategy, :version, 'running')
                        RETURNING id
                    """), {
                        "ts": datetime.now(timezone.utc),
                        "mode": mode, "strategy": strategy, "version": git_sha,
                    })
                    run_id[0] = result.scalar()
                    session.commit()
                except Exception:
                    session.rollback()
                finally:
                    session.close()
            else:
                import db as _db
                with _db.get_session() as session:
                    result = session.execute(text("""
                        INSERT INTO runs (started_at, mode, strategy, version, outcome)
                        VALUES (:ts, :mode, :strategy, :version, 'running')
                        RETURNING id
                    """), {
                        "ts": datetime.now(timezone.utc),
                        "mode": mode, "strategy": strategy, "version": git_sha,
                    })
                    run_id[0] = result.scalar()

        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, _insert)
        _log.info("Run %s started — mode=%s strategy=%s version=%s", run_id[0], mode, strategy, git_sha)
        return run_id[0]

    async def log_run_stop(self, run_id: Optional[int], outcome: str = "stopped") -> None:
        if run_id is None:
            return
        await self._run("""
            UPDATE runs SET stopped_at = :ts, outcome = :outcome WHERE id = :id
        """, {"ts": datetime.now(timezone.utc), "outcome": outcome, "id": run_id})


# ══════════════════════════════════════════════════════════════════════════════
# LogBuffer — rolling in-memory buffer for /api/logs
# ══════════════════════════════════════════════════════════════════════════════

class LogBuffer:
    """Captures Python logging output into a deque for /api/logs."""

    _ICONS = {"INFO": "·", "WARNING": "⚠", "ERROR": "✗", "CRITICAL": "⛔", "DEBUG": "·"}
    _SKIP  = {"aiohttp.access", "dhan.client"}

    def __init__(self, maxlen: int = 100):
        self._buf: deque = deque(maxlen=maxlen)

    def get_logs(self, limit: int = 50) -> list:
        return list(self._buf)[-limit:]

    def _handler(self) -> logging.Handler:
        buf = self._buf
        icons = self._ICONS
        skip = self._SKIP

        class _H(logging.Handler):
            def emit(self, record):
                if record.name in skip:
                    return
                buf.append({
                    "ts":    datetime.now(timezone.utc).isoformat(),
                    "level": record.levelname,
                    "icon":  icons.get(record.levelname, "·"),
                    "name":  record.name.replace("dhan.", ""),
                    "msg":   self.format(record),
                })

        h = _H()
        h.setFormatter(logging.Formatter("%(message)s"))
        h.setLevel(logging.DEBUG)
        return h

    def install(self):
        logging.getLogger().addHandler(self._handler())


# ── Singletons ─────────────────────────────────────────────────────────────────

_db_backend:   Optional[AsyncDBBackend] = None
_log_buffer:   Optional[LogBuffer]    = None


def get_db_backend() -> AsyncDBBackend:
    global _db_backend
    if _db_backend is None:
        _db_backend = AsyncDBBackend()
    return _db_backend


def get_log_buffer() -> LogBuffer:
    global _log_buffer
    if _log_buffer is None:
        _log_buffer = LogBuffer()
    return _log_buffer


def install_log_buffer():
    """Call once at startup to capture all log output."""
    get_log_buffer().install()


# ── Backwards-compat shims (so existing imports don't break) ──────────────────

# core.log_buffer compatibility
def install():
    install_log_buffer()

def get_logs(limit: int = 50) -> list:
    return get_log_buffer().get_logs(limit)
