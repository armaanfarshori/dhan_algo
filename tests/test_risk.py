"""RiskManager pre-trade checks and kill switch."""
from core.risk import RiskManager, RiskConfig


def make_rm(**kw):
    return RiskManager(client=None, config=RiskConfig(**kw))


def test_allows_normal_order():
    rm = make_rm(max_loss_per_trade=100_000)
    ok, msg = rm.check_order(quantity=10, price=500)
    assert ok, msg


def test_blocks_oversized_order():
    rm = make_rm(max_loss_per_trade=1_000)
    ok, msg = rm.check_order(quantity=10, price=500)   # notional 5,000 > 1,000
    assert not ok
    assert "exceeds limit" in msg


def test_kill_switch_blocks_everything():
    rm = make_rm(max_loss_per_trade=100_000)
    rm.config.kill_switch = True
    ok, msg = rm.check_order(quantity=1, price=1)
    assert not ok
    assert "halted" in msg.lower()


def test_max_open_positions():
    rm = make_rm(max_open_positions=2, max_loss_per_trade=100_000)
    rm.state.open_position_count = 2
    ok, msg = rm.check_order(quantity=1, price=1)
    assert not ok
    assert "Max open positions" in msg


def test_halt_state_blocks():
    rm = make_rm(max_loss_per_trade=100_000)
    rm.state.halted = True
    rm.state.halt_reason = "daily loss breached"
    ok, msg = rm.check_order(quantity=1, price=1)
    assert not ok
    rm.resume()
    ok, _ = rm.check_order(quantity=1, price=1)
    assert ok
