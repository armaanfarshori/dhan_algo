"""QA finding H2 — token persistence safety. FIXED 2026-06-15.

Before: the token manager rewrote .env in place on every refresh (regex sub +
        write_text, non-atomic) — a crash mid-write could corrupt .env and take
        out DB credentials on the next boot. Nothing authoritative even read
        that value (all readers use read_current_token()).

After:  the rotating token is written ONLY to dhan_token.json, atomically
        (temp file + os.replace). .env is never touched by this module.
"""
import os

import core.token_manager as tm


def _seed_env(tmp_path):
    env = tmp_path / ".env"
    env.write_text(
        "DHAN_CLIENT_ID=cid\n"
        "DHAN_ACCESS_TOKEN=seed_token\n"
        "DB_PASSWORD=secret_pw\n"
        "TELEGRAM_BOT_TOKEN=abc\n")
    return env


def test_token_refresh_does_not_touch_env(tmp_path, monkeypatch):
    """H2 fix: a token write leaves .env byte-for-byte unchanged."""
    env = _seed_env(tmp_path)
    tok = tmp_path / "dhan_token.json"
    monkeypatch.setattr(tm, "_ENV_FILE", env)
    monkeypatch.setattr(tm, "_TOKEN_FILE", tok)

    before = env.read_text()
    tm._write_token("new_token", "2026-06-15T20:00:00", "cid")

    assert env.read_text() == before                       # .env untouched
    assert '"accessToken": "new_token"' in tok.read_text()  # token cached


def test_token_file_written_atomically(tmp_path, monkeypatch):
    """H2 fix: the token cache is written via os.replace (atomic), so a crash
    mid-write cannot leave a half-written dhan_token.json."""
    tok = tmp_path / "dhan_token.json"
    monkeypatch.setattr(tm, "_TOKEN_FILE", tok)

    calls = []
    real_replace = os.replace
    monkeypatch.setattr(os, "replace",
                        lambda a, b: calls.append((a, b)) or real_replace(a, b))

    tm._write_token("new_token", "2026-06-15T20:00:00", "cid")

    assert calls, "token file must be persisted via os.replace (temp + rename)"
    assert tok.exists() and "new_token" in tok.read_text()
    assert not tok.with_suffix(tok.suffix + ".tmp").exists()   # temp cleaned up
