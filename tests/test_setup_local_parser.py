"""setup_local.sh's .env parser must agree with python-dotenv.

`alembic/env.py` reads DB_* from the process environment, so the bootstrap
script has to lift those five keys out of `.env` by itself — in bash, without
sourcing a file full of operator secrets and shell metacharacters. The running
platform reads the SAME file through pydantic-settings, i.e. python-dotenv.

Any disagreement between the two parsers means `alembic upgrade head` migrates
one database while the trader connects to another. That is exactly what the
original `KEY=`-anchored regex did: `DB_NAME = foo` (whitespace around `=`,
which python-dotenv accepts) did not match at all, and the script silently fell
back to the hardcoded default.

These tests extract the shell function verbatim from the script and run it
under bash against lines python-dotenv is then asked to parse too, so the two
implementations are compared rather than merely asserted about.
"""
from __future__ import annotations

import io
import re
import shutil
import subprocess
from pathlib import Path

import pytest
from dotenv import dotenv_values

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "infra" / "scripts" / "setup_local.sh"
UNIT_DIR = REPO_ROOT / "infra" / "systemd"

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="bash not available")

# Realistic hand-edited .env spellings. Each is a single line, and every one of
# them is something an operator plausibly types.
LINES = [
    "DB_HOST=localhost",  # canonical
    "DB_HOST = localhost",  # spaces around '=' — python-dotenv accepts this
    "DB_HOST =localhost",
    "DB_HOST= localhost",
    "DB_PORT=5433",
    "DB_USER=trader   # inline comment",
    'DB_PASSWORD="pw123"',
    "DB_PASSWORD='pw123'",
    'DB_PASSWORD="pw123" # my note',  # quoted value THEN a comment
    'DB_PASSWORD="pw 123"',  # spaces are why one quotes in the first place
    "DB_PASSWORD=pw#123",  # '#' without leading space is NOT a comment
    "DB_PASSWORD=a b c",
    "export DB_USER=trader",
    "export DB_USER = trader",
    "DB_HOST=localhost\r",  # CRLF file edited on Windows
    'DB_NAME="dhan_x"\r',
]


def _extract_env_get() -> str:
    """Pull `env_get()` out of setup_local.sh (the script self-executes, so it
    cannot simply be sourced)."""
    src = SCRIPT.read_text()
    match = re.search(r"^env_get\(\) \{\n.*?^\}\n", src, re.MULTILINE | re.DOTALL)
    assert match, "env_get() not found in setup_local.sh — did it get renamed?"
    return match.group(0)


def _bash_env_get(tmp_path: Path, content: str, key: str) -> str | None:
    """Run the script's own env_get against `content`; None == 'not found'."""
    env_file = tmp_path / ".env"
    env_file.write_bytes(content.encode())
    harness = tmp_path / "harness.sh"
    harness.write_text(
        'ENV_FILE="$1"\n'
        + _extract_env_get()
        + '\nif value="$(env_get "$2")"; then printf "FOUND:%s" "$value"; else printf "MISS"; fi\n'
    )
    out = subprocess.run(
        ["bash", str(harness), str(env_file), key],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return out[len("FOUND:") :] if out.startswith("FOUND:") else None


@pytest.mark.parametrize("line", LINES)
def test_matches_python_dotenv(tmp_path, line):
    """The bash parser and python-dotenv must extract the same value."""
    key = line.split("=")[0].replace("export ", "").strip()
    expected = dotenv_values(stream=io.StringIO(line.rstrip("\r") + "\n"))[key]
    assert _bash_env_get(tmp_path, line + "\n", key) == expected


def test_spaces_around_equals_is_not_a_miss(tmp_path):
    """The regression that motivated this file: a value that parses fine for the
    app must not read as absent here (silent fallback to the default DB)."""
    content = "DB_NAME = my_real_db\nDB_PASSWORD = s3cret\n"
    assert _bash_env_get(tmp_path, content, "DB_NAME") == "my_real_db"
    assert _bash_env_get(tmp_path, content, "DB_PASSWORD") == "s3cret"


def test_quoted_value_with_trailing_comment_keeps_no_quotes(tmp_path):
    """Quotes must not leak into the value when a comment follows the closing
    quote — that string goes straight to `alembic upgrade head` as a password."""
    assert _bash_env_get(tmp_path, 'DB_PASSWORD="pw123" # note\n', "DB_PASSWORD") == "pw123"


def test_last_assignment_wins(tmp_path):
    assert _bash_env_get(tmp_path, "DB_HOST=first\nDB_HOST = second\n", "DB_HOST") == "second"


def test_commented_out_and_absent_keys_are_misses(tmp_path):
    """A commented-out key must not be read, and a missing one must report the
    miss so the caller can announce its fallback instead of hiding it."""
    content = "# DB_HOST=nope\nDB_PORT=5432\n"
    assert _bash_env_get(tmp_path, content, "DB_HOST") is None
    assert _bash_env_get(tmp_path, content, "DB_PASSWORD") is None


def test_empty_value_is_a_miss(tmp_path):
    assert _bash_env_get(tmp_path, "DB_PASSWORD=\nDB_HOST=  \n", "DB_PASSWORD") is None
    assert _bash_env_get(tmp_path, "DB_PASSWORD=\nDB_HOST=  \n", "DB_HOST") is None


def test_key_is_not_matched_as_a_substring(tmp_path):
    assert _bash_env_get(tmp_path, "BACKTEST_DB_HOST=elsewhere\n", "DB_HOST") is None


@pytest.mark.parametrize(
    "unit", ["dhan-trader.service", "dhan-api.service", "dhan-alert@.service"]
)
def test_canonical_units_carry_the_templated_user_line(unit):
    """setup_local.sh templates the service user with `sed s/^User=ubuntu$/…/`
    and now aborts if that yields nothing. Catch a drifting `User=` line here,
    in CI, rather than on the box mid-bootstrap."""
    text = (UNIT_DIR / unit).read_text()
    assert re.search(r"^User=ubuntu$", text, re.MULTILINE), (
        f"{unit} no longer has a bare 'User=ubuntu' line — setup_local.sh's sed "
        "would no longer template the service user"
    )
