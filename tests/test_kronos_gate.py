"""TEST-06 — KronosGate: shadow mode, enforcing mode, fail-open, persistence.

FakeEngine replaces KronosSignalEngine (no torch, no DB).
FakeDB captures log_signal calls so we can assert persistence without a real DB.
"""
import asyncio

from ml.kronos_gate import KronosGate


# ── Test doubles ─────────────────────────────────────────────────────────────

class FakeEngine:
    """Controllable replacement for KronosSignalEngine."""

    def __init__(self, side="BUY", confidence=0.8, score=0.75,
                 data_age_min=5, forecasted_return=0.02, raise_on_call=False):
        self.side = side
        self.confidence = confidence
        self.score = score
        self.data_age_min = data_age_min
        self.forecasted_return = forecasted_return
        self.raise_on_call = raise_on_call
        self.call_count = 0

    async def score_from_db(self, security_id: str, exchange_segment: str = "NSE_EQ"):
        self.call_count += 1
        if self.raise_on_call:
            raise RuntimeError("FakeEngine: simulated model failure")
        return {
            "side": self.side,
            "confidence": self.confidence,
            "score": self.score,
            "data_age_min": self.data_age_min,
            "forecasted_return": self.forecasted_return,
            "current_price": 100.0,
            "horizon_price": 102.0,
            "last_bar_ts": "2026-06-12T09:30:00",
            "scorer_version": "zero-shot-v1",
        }


class FakeDB:
    """Captures log_signal calls without touching the database."""

    def __init__(self):
        self.calls: list[dict] = []

    async def log_signal(self, **kwargs):
        self.calls.append(kwargs)


# ── Shadow mode: always allows, always persists ───────────────────────────────

def test_shadow_allows_when_model_agrees():
    """Shadow mode must allow even when the model agrees with direction."""
    engine = FakeEngine(side="BUY", confidence=0.9)
    db = FakeDB()
    gate = KronosGate(engine, db_backend=db, shadow=True)
    result = asyncio.run(gate.allows("2885", "BUY"))
    assert result is True


def test_shadow_allows_when_model_disagrees():
    """Shadow mode must allow regardless of model verdict (BLOCK is not enforced)."""
    engine = FakeEngine(side="SELL", confidence=0.9)
    db = FakeDB()
    gate = KronosGate(engine, db_backend=db, shadow=True)
    result = asyncio.run(gate.allows("2885", "BUY"))
    assert result is True


def test_shadow_allows_when_confidence_low():
    """Shadow mode must allow even when confidence is below threshold."""
    engine = FakeEngine(side="BUY", confidence=0.1)
    db = FakeDB()
    gate = KronosGate(engine, db_backend=db, min_confidence=0.4, shadow=True)
    result = asyncio.run(gate.allows("2885", "BUY"))
    assert result is True


def test_shadow_persists_verdict():
    """Shadow mode must call db.log_signal once per allows() invocation."""
    engine = FakeEngine(side="SELL", confidence=0.9)
    db = FakeDB()
    gate = KronosGate(engine, db_backend=db, shadow=True)
    asyncio.run(gate.allows("2885", "BUY"))
    assert len(db.calls) == 1


def test_shadow_persists_correct_fields():
    """The persisted record must carry the expected metadata."""
    engine = FakeEngine(side="BUY", confidence=0.85, score=0.75)
    db = FakeDB()
    gate = KronosGate(engine, db_backend=db, shadow=True)
    asyncio.run(gate.allows("2885", "BUY"))
    call = db.calls[0]
    assert call["security_id"] == "2885"
    assert call["strategy"] == "orb_gate"
    features = call["features"]
    assert features["shadow"] is True
    assert features["enforced"] is False
    assert features["requested_direction"] == "BUY"
    assert features["verdict"] in ("ALLOW", "BLOCK")


def test_shadow_persists_on_every_call():
    """Each allows() call must produce exactly one log_signal call."""
    engine = FakeEngine()
    db = FakeDB()
    gate = KronosGate(engine, db_backend=db, shadow=True)
    asyncio.run(gate.allows("2885", "BUY"))
    asyncio.run(gate.allows("3045", "SELL"))
    assert len(db.calls) == 2


# ── Enforcing mode: gates on side + confidence ───────────────────────────────

def test_enforcing_allows_when_model_agrees_with_confidence():
    """Enforcing mode allows when side matches AND confidence >= threshold."""
    engine = FakeEngine(side="BUY", confidence=0.8)
    db = FakeDB()
    gate = KronosGate(engine, db_backend=db, min_confidence=0.4, shadow=False)
    result = asyncio.run(gate.allows("2885", "BUY"))
    assert result is True


def test_enforcing_blocks_when_side_disagrees():
    """Enforcing mode must return False when model side != requested direction."""
    engine = FakeEngine(side="SELL", confidence=0.9)
    db = FakeDB()
    gate = KronosGate(engine, db_backend=db, min_confidence=0.4, shadow=False)
    result = asyncio.run(gate.allows("2885", "BUY"))
    assert result is False


def test_enforcing_blocks_when_confidence_below_threshold():
    """Enforcing mode must return False when confidence < min_confidence."""
    engine = FakeEngine(side="BUY", confidence=0.3)
    db = FakeDB()
    gate = KronosGate(engine, db_backend=db, min_confidence=0.4, shadow=False)
    result = asyncio.run(gate.allows("2885", "BUY"))
    assert result is False


def test_enforcing_blocks_when_confidence_exactly_at_boundary():
    """Boundary: confidence exactly equal to min_confidence must be allowed."""
    engine = FakeEngine(side="BUY", confidence=0.4)
    db = FakeDB()
    gate = KronosGate(engine, db_backend=db, min_confidence=0.4, shadow=False)
    result = asyncio.run(gate.allows("2885", "BUY"))
    assert result is True


def test_enforcing_blocks_hold_side():
    """When model returns HOLD it should not match BUY or SELL."""
    engine = FakeEngine(side="HOLD", confidence=0.9)
    db = FakeDB()
    gate = KronosGate(engine, db_backend=db, min_confidence=0.4, shadow=False)
    assert asyncio.run(gate.allows("2885", "BUY")) is False
    assert asyncio.run(gate.allows("2885", "SELL")) is False


def test_enforcing_persists_even_on_block():
    """Enforcing mode must still persist the verdict even when it blocks."""
    engine = FakeEngine(side="SELL", confidence=0.9)
    db = FakeDB()
    gate = KronosGate(engine, db_backend=db, shadow=False)
    asyncio.run(gate.allows("2885", "BUY"))
    assert len(db.calls) == 1
    assert db.calls[0]["features"]["verdict"] == "BLOCK"
    assert db.calls[0]["features"]["enforced"] is True


def test_enforcing_persisted_features_correct():
    """When enforcing, features must record enforced=True, shadow=False."""
    engine = FakeEngine(side="BUY", confidence=0.9)
    db = FakeDB()
    gate = KronosGate(engine, db_backend=db, min_confidence=0.4, shadow=False)
    asyncio.run(gate.allows("2885", "BUY"))
    features = db.calls[0]["features"]
    assert features["enforced"] is True
    assert features["shadow"] is False


# ── Fail-open: model errors must never block ──────────────────────────────────

def test_fail_open_on_model_exception():
    """If score_from_db raises, allows() must return True (fail-open)."""
    engine = FakeEngine(raise_on_call=True)
    db = FakeDB()
    gate = KronosGate(engine, db_backend=db, shadow=False)
    result = asyncio.run(gate.allows("2885", "BUY"))
    assert result is True


def test_fail_open_in_shadow_mode_on_model_exception():
    """Fail-open applies in shadow mode too."""
    engine = FakeEngine(raise_on_call=True)
    gate = KronosGate(engine, db_backend=None, shadow=True)
    result = asyncio.run(gate.allows("2885", "BUY"))
    assert result is True


def test_fail_open_does_not_persist_on_exception():
    """When the model raises, _persist is never called (nothing to save)."""
    engine = FakeEngine(raise_on_call=True)
    db = FakeDB()
    gate = KronosGate(engine, db_backend=db, shadow=False)
    asyncio.run(gate.allows("2885", "BUY"))
    # No persistence should have been attempted
    assert len(db.calls) == 0


def test_fail_open_db_error_does_not_propagate():
    """If db.log_signal raises, allows() must still return the gate verdict."""

    class BrokenDB:
        async def log_signal(self, **kwargs):
            raise RuntimeError("DB is down")

    engine = FakeEngine(side="BUY", confidence=0.9)
    gate = KronosGate(engine, db_backend=BrokenDB(), shadow=False)
    # Should not raise — the gate logs the error but returns normally
    result = asyncio.run(gate.allows("2885", "BUY"))
    assert result is True  # model agreed, and DB failure doesn't flip verdict


# ── No DB backend — graceful no-op ───────────────────────────────────────────

def test_no_db_backend_shadow_still_allows():
    """When db_backend=None, shadow mode must still allow and not raise."""
    engine = FakeEngine(side="SELL", confidence=0.9)
    gate = KronosGate(engine, db_backend=None, shadow=True)
    result = asyncio.run(gate.allows("2885", "BUY"))
    assert result is True


def test_no_db_backend_enforcing_still_gates():
    """When db_backend=None, enforcing mode must still block on mismatch."""
    engine = FakeEngine(side="SELL", confidence=0.9)
    gate = KronosGate(engine, db_backend=None, shadow=False)
    result = asyncio.run(gate.allows("2885", "BUY"))
    assert result is False
