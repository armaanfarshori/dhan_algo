"""
Telegram alerts — plain bot API, zero LLM, zero cost.

Replaces the Hermes gateway for the only job it actually did well: putting
platform events in your pocket. The bot token is read from the environment;
sending a message via api.telegram.org is free.

Library use:
    from core.notify import send, send_async
    send("⛔ HALT: daily loss limit")          # sync, fire-and-forget safe
    await send_async("...")                    # from the trader's event loop

CLI (for cron pipelines — sends only if there is input):
    echo "backfill restarted" | python -m core.notify --stdin
    python -m core.notify "direct message"
"""
import asyncio
import json
import logging
import urllib.request

logger = logging.getLogger("dhan.notify")

_API = "https://api.telegram.org/bot{token}/sendMessage"


def send(text: str, timeout: int = 10) -> bool:
    """Synchronous send. Never raises — alerting must not break trading."""
    if not text or not text.strip():
        return False
    try:
        from config import get_config
        cfg = get_config()
        if not cfg.telegram_bot_token or not cfg.telegram_chat_id:
            logger.warning("notify: TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set — dropped: %s",
                           text[:80])
            return False
        req = urllib.request.Request(
            _API.format(token=cfg.telegram_bot_token),
            data=json.dumps({"chat_id": cfg.telegram_chat_id,
                             "text": text[:4000]}).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            ok = json.loads(resp.read()).get("ok", False)
        if not ok:
            logger.warning("notify: telegram rejected message")
        return ok
    except Exception as exc:
        logger.warning("notify: send failed (%s) — %s", exc, text[:80])
        return False


async def send_async(text: str) -> bool:
    """Executor-wrapped send for use inside the trading event loop."""
    return await asyncio.get_event_loop().run_in_executor(None, send, text)


def main():
    import argparse
    import sys
    from dotenv import load_dotenv
    load_dotenv()
    p = argparse.ArgumentParser(description="Send a Telegram alert")
    p.add_argument("message", nargs="?", help="message text")
    p.add_argument("--stdin", action="store_true",
                   help="read message from stdin; silently exit if empty (cron-friendly)")
    args = p.parse_args()

    text = sys.stdin.read().strip() if args.stdin else (args.message or "")
    if not text:
        return                      # empty pipeline → no alert, exit 0
    ok = send(text)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
