"""NSE stock-F&O universe — the distinct stock derivatives underlyings.

Phase-0 of the EQUITY (stock) F&O data foundation. Parses Dhan's DETAILED scrip
master for the ~180-190 NSE single-stock derivatives underlyings and projects, per
underlying:

  • underlying_symbol        — e.g. "RELIANCE" (UNDERLYING_SYMBOL)
  • underlying_security_id   — the FUTSTK row's UNDERLYING_SECURITY_ID — this is the
                               id passed as ``UnderlyingScrip`` to the option-chain /
                               expiry-list endpoints with ``UnderlyingSeg=NSE_FNO``.
  • future_security_id       — the NEAR-MONTH FUTSTK contract's SECURITY_ID (for the
                               historical charts endpoint, NSE_FNO / FUTSTK).
  • near_expiry              — that near-month future's expiry (front month).
  • lot_size                 — contract lot size [verify-me: exchange revises these].

Capture-everything rule (see memory `capture-full-payload-use-subset`): the full
detailed scrip master is already persisted verbatim by core/fno_instruments into the
``fno_instruments`` table; THIS module projects only the small subset above and
materialises it as a generated JSON (mirroring how the NIFTY ids are hard-pinned in
core/fno_backfill.SYMBOL_SCRIP) so the futures-backfill + equity-collector drivers
have a stable, reviewable universe without a DB round-trip.

Pure parse helpers are DB-free and network-free so they unit-test on a fixture CSV
slice. The CSV download is reused from core.fno_instruments (cached).

Usage::

    python -m core.fno_equity_universe --refresh            # write the JSON
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import logging
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("dhan.fno_equity_universe")

# Generated artefact — the projected stock-F&O universe (small, reviewable).
# Consumers may fall back to a DB read if absent (not implemented here — Phase-0
# generates the file off the scrip master).
UNIVERSE_FILE = Path(__file__).parent.parent / "data" / "fno_equity_universe.json"

# Single-stock derivative instrument codes in the detailed master (EXCH_ID=NSE).
STOCK_FUT_INSTRUMENT = "FUTSTK"
STOCK_OPT_INSTRUMENT = "OPTSTK"


# ── pure parse helpers (DB-free, network-free) ───────────────────────────────────
def _to_int(value: Any) -> Optional[int]:
    if value is None:
        return None
    v = str(value).strip()
    if v == "" or v.upper() == "NA":
        return None
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


def _parse_expiry(value: Any) -> Optional[date]:
    if value is None:
        return None
    v = str(value).strip()
    if v == "" or v.upper() == "NA":
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(v, fmt).date()
        except ValueError:
            continue
    return None


def parse_stock_fno_universe(csv_text: str) -> list[dict[str, Any]]:
    """Parse the detailed scrip-master CSV → one projected row per distinct stock-F&O
    underlying, sorted by symbol.

    Selection: rows with EXCH_ID == "NSE" and INSTRUMENT == "FUTSTK". For each
    underlying we pick the NEAR-MONTH future (the FUTSTK row with the minimum
    expiry_date) to source ``future_security_id`` / ``near_expiry`` / ``lot_size``.
    The ``underlying_security_id`` is taken from that same row's
    UNDERLYING_SECURITY_ID (the option-chain ``UnderlyingScrip``).

    Pure: no I/O. Malformed / NA-expiry rows are skipped silently.
    """
    reader = csv.DictReader(io.StringIO(csv_text))
    # underlying_symbol -> best (near-month) future row
    best: dict[str, dict[str, Any]] = {}
    for row in reader:
        if (row.get("EXCH_ID") or "").strip() != "NSE":
            continue
        if (row.get("INSTRUMENT") or "").strip() != STOCK_FUT_INSTRUMENT:
            continue
        sym = (row.get("UNDERLYING_SYMBOL") or "").strip()
        sec_id = (row.get("SECURITY_ID") or "").strip()
        u_sec_id = (row.get("UNDERLYING_SECURITY_ID") or "").strip()
        expiry = _parse_expiry(row.get("SM_EXPIRY_DATE"))
        if not sym or not sec_id or expiry is None:
            continue
        cand = {
            "underlying_symbol": sym,
            "underlying_security_id": u_sec_id or None,
            "future_security_id": sec_id,
            "near_expiry": expiry,
            "lot_size": _to_int(row.get("LOT_SIZE")),
        }
        cur = best.get(sym)
        if cur is None or expiry < cur["near_expiry"]:
            best[sym] = cand

    return [best[s] for s in sorted(best)]


# ── persistence (generated JSON) ─────────────────────────────────────────────────
def _serialise(universe: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "count": len(universe),
        "note": "lot_size is [verify-me] — exchange revises lot sizes; re-refresh before sizing.",
        "underlyings": [
            {
                "underlying_symbol": u["underlying_symbol"],
                "underlying_security_id": u["underlying_security_id"],
                "future_security_id": u["future_security_id"],
                "near_expiry": u["near_expiry"].isoformat() if u["near_expiry"] else None,
                "lot_size": u["lot_size"],
            }
            for u in universe
        ],
    }


def write_universe(universe: list[dict[str, Any]], path: Path = UNIVERSE_FILE) -> Path:
    """Write the projected universe to the generated JSON. Creates the parent dir."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(_serialise(universe), indent=2), encoding="utf-8")
    logger.info("wrote %d stock-F&O underlyings → %s", len(universe), path)
    return path


def load_universe(path: Path = UNIVERSE_FILE) -> list[dict[str, Any]]:
    """Load the generated universe JSON → list of underlying dicts (near_expiry as date).

    Raises FileNotFoundError if the artefact has not been generated yet — run
    ``python -m core.fno_equity_universe --refresh`` first (off the scrip master).
    """
    raw = json.loads(path.read_text(encoding="utf-8"))
    out: list[dict[str, Any]] = []
    for u in raw.get("underlyings", []):
        out.append(
            {
                "underlying_symbol": u["underlying_symbol"],
                "underlying_security_id": u.get("underlying_security_id"),
                "future_security_id": u.get("future_security_id"),
                "near_expiry": _parse_expiry(u.get("near_expiry")),
                "lot_size": u.get("lot_size"),
            }
        )
    return out


def refresh_universe(force_download: bool = False, path: Path = UNIVERSE_FILE) -> list[dict[str, Any]]:
    """Download (cached) the detailed scrip master, parse the stock-F&O universe,
    write the generated JSON, and return it. Network: only the cached CSV fetch
    (reused from core.fno_instruments)."""
    from core.fno_instruments import _download_csv  # reuse the cached downloader

    csv_text = _download_csv(force=force_download)
    universe = parse_stock_fno_universe(csv_text)
    write_universe(universe, path)
    return universe


# ── CLI ──────────────────────────────────────────────────────────────────────────
def main() -> None:
    logging.basicConfig(level=logging.INFO)
    p = argparse.ArgumentParser(description="Refresh the NSE stock-F&O universe from the scrip master.")
    p.add_argument("--refresh", action="store_true", help="download the master and (re)write the universe JSON")
    p.add_argument("--force-download", action="store_true", help="bypass the scrip-master cache")
    args = p.parse_args()
    if args.refresh:
        universe = refresh_universe(force_download=args.force_download)
        logger.info("stock-F&O universe: %d underlyings", len(universe))
    else:
        p.print_help()


if __name__ == "__main__":
    main()
