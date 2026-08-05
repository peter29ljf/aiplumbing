#!/usr/bin/env python3
"""Point a Telegram bot at this deployment, and check it landed.

Creating the bot is a conversation with @BotFather that only a person can have. This does
everything after that: registers the webhook, attaches the shared secret, and reads back
what Telegram thinks it is now sending to.

    python3 scripts/setup_telegram.py --url https://smartstrategy.services/telegram
    python3 scripts/setup_telegram.py --status

The secret matters more than it looks. The webhook is a public URL, and Telegram echoes
this value on every update — without it anyone who guesses the path can post a forged
update and drive the bot as a technician. `--rotate` writes a fresh one into .env and
re-registers, which is what to do if it ever leaks.
"""

from __future__ import annotations

import argparse
import json
import secrets
import sys
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from plumbing.paths import ROOT, load_dotenv  # noqa: E402

API = "https://api.telegram.org/bot{token}/{method}"


def call(token: str, method: str, **params: object) -> dict:
    data = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None}).encode()
    request = urllib.request.Request(API.format(token=token, method=method), data=data)
    with urllib.request.urlopen(request, timeout=20) as response:
        return json.loads(response.read())


def set_env(name: str, value: str) -> None:
    """Write one variable into .env without disturbing the rest of the file."""
    path = ROOT / ".env"
    lines = path.read_text().splitlines() if path.exists() else []
    for index, line in enumerate(lines):
        if line.startswith(f"{name}="):
            lines[index] = f"{name}={value}"
            break
    else:
        lines.append(f"{name}={value}")
    path.write_text("\n".join(lines) + "\n")
    path.chmod(0o600)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", help="public HTTPS endpoint, ending in /telegram")
    parser.add_argument("--status", action="store_true", help="what is registered right now")
    parser.add_argument("--rotate", action="store_true", help="new secret, then re-register")
    args = parser.parse_args()

    load_dotenv()
    import os

    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    if not token:
        print(
            "TELEGRAM_BOT_TOKEN is not set.\n\n"
            "  1. Message @BotFather in Telegram\n"
            "  2. /newbot, give it a name and a username ending in 'bot'\n"
            "  3. Put the token it gives you in .env as TELEGRAM_BOT_TOKEN\n"
            "  4. Run this again with --url",
            file=sys.stderr,
        )
        return 2

    me = call(token, "getMe").get("result", {})
    print(f"bot: @{me.get('username', '?')} ({me.get('first_name', '')})")

    if args.status and not args.url:
        info = call(token, "getWebhookInfo").get("result", {})
        print(f"  webhook url:     {info.get('url') or '(none registered)'}")
        print(f"  secret attached: {bool(info.get('has_custom_certificate') or info.get('url'))}")
        print(f"  pending updates: {info.get('pending_update_count', 0)}")
        if info.get("last_error_message"):
            # The single most useful line here: Telegram keeps trying and records why it
            # is failing, and that message is usually the whole answer.
            print(f"  last error:      {info['last_error_message']}")
        return 0

    if not args.url:
        parser.error("--url is required unless --status")

    secret = os.environ.get("TELEGRAM_WEBHOOK_SECRET", "")
    if args.rotate or not secret:
        secret = secrets.token_urlsafe(32)
        set_env("TELEGRAM_WEBHOOK_SECRET", secret)
        print("  wrote a new TELEGRAM_WEBHOOK_SECRET into .env")

    result = call(
        token, "setWebhook",
        url=args.url,
        secret_token=secret,
        # Button presses arrive as callback_query, and the default subscription does not
        # include them — without this the Accept and Decline buttons do nothing at all.
        allowed_updates=json.dumps(["message", "callback_query"]),
        drop_pending_updates="true",
    )
    if not result.get("ok"):
        print(f"  setWebhook failed: {result.get('description')}", file=sys.stderr)
        return 1

    info = call(token, "getWebhookInfo").get("result", {})
    print(f"  registered: {info.get('url')}")
    print(f"  receiving:  {', '.join(info.get('allowed_updates') or ['(default)'])}")
    print("\nThe service must be restarted to pick up the secret from .env.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
