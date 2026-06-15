"""QA finding H2 — token persistence safety.

H2 (High): _write_token rewrites .env with a regex substitution and a direct
           write_text (no temp-file + atomic rename). A crash mid-write
           corrupts .env, and the next boot has no DB credentials. The
           "single-writer lock" promised in the module docstring is not
           implemented.
"""
import os

import pytest

import core.token_manager as tm


def _seed_env(tmp_path):
    env = tmp_path / ".env"
    env.write_text(
        "DHAN_CLIENT_ID=cid\n"
        "DHAN_ACCESS_TOKEN=old_token\n"
        "DB_PASSWORD=secret_pw\n"
        "TELEGRAM_BOT_TOKEN=abc\n")
    return env


def test_write_token_preserves_other_env_lines(tmp_path, monkeypatch):
    """Regression: the token substitution replaces ONLY the token line and
    leaves DB_PASSWORD and others intact."""
    env = _seed_env(tmp_path)
    monkeypatch.setattr(tm, "_ENV_FILE", env)
    monkeypatch.setattr(tm, "_TOKEN_FILE", tmp_path / "dhan_token.json")

    tm._write_token("new_token", "2026-06-15T20:00:00", "cid")

    body = env.read_text()
    assert "DHAN_ACCESS_TOKEN=new_token" in body
    assert "old_token" not in body
    assert "DB_PASSWORD=secret_pw" in body        # other creds untouched
    assert "TELEGRAM_BOT_TOKEN=abc" in body
    assert '"accessToken": "new_token"' in (tmp_path / "dhan_token.json").read_text()


@pytest.mark.xfail(strict=True,
                   reason="H2: .env is rewritten in place (write_text), not via a "
                          "temp file + os.replace, so a crash mid-write corrupts it")
def test_env_write_is_atomic(tmp_path, monkeypatch):
    env = _seed_env(tmp_path)
    monkeypatch.setattr(tm, "_ENV_FILE", env)
    monkeypatch.setattr(tm, "_TOKEN_FILE", tmp_path / "dhan_token.json")

    replace_calls = []
    real_replace = os.replace
    monkeypatch.setattr(os, "replace", lambda a, b: replace_calls.append((a, b)) or real_replace(a, b))

    tm._write_token("new_token", "2026-06-15T20:00:00", "cid")
    assert replace_calls, "atomic write should go through os.replace(tmp, target)"
