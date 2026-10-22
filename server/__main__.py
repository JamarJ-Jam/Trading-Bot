"""Run the app:   python -m server          (web app + bot, on APP_HOST:APP_PORT)
Set up login:  python -m server setup    (prints the lines to add to .env)
"""

from __future__ import annotations

import getpass
import os
import secrets
import sys
from pathlib import Path

from dotenv import load_dotenv


def setup() -> int:
    from .auth import hash_password, new_totp_secret, otpauth_uri, totp

    print("Choose the app password (at least 12 characters).")
    password = getpass.getpass("Password: ")
    if len(password) < 12:
        print("Too short; use at least 12 characters.", file=sys.stderr)
        return 2
    if getpass.getpass("Same again: ") != password:
        print("The two passwords did not match.", file=sys.stderr)
        return 2
    secret = new_totp_secret()
    print("\nAdd this to your authenticator app (Google Authenticator, Authy, Samsung Pass...).")
    print("Most apps let you type the key in; some accept this link:")
    print(f"  key:  {secret}")
    print(f"  link: {otpauth_uri(secret)}")
    print(f"\nThe app should now show {totp(secret)} (it changes every 30 seconds).")
    print("\nPut these three lines in the server's .env file (keep them private):\n")
    print(f"APP_PASSWORD_HASH={hash_password(password)}")
    print(f"APP_TOTP_SECRET={secret}")
    print(f"APP_SECRET_KEY={secrets.token_hex(32)}")
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    load_dotenv()
    if argv[:1] == ["setup"]:
        return setup()
    if argv:
        print(__doc__, file=sys.stderr)
        return 2

    import uvicorn

    from bot.trade_log import setup_logging

    from .api import create_app

    env = dict(os.environ)
    setup_logging(Path(env.get("LOG_DIR") or "logs"),
                  secrets=tuple(env.get(k, "") for k in
                                ("DERIV_API_TOKEN", "MT5_PASSWORD", "OPENROUTER_API_KEY",
                                 "APP_SECRET_KEY")))
    uvicorn.run(create_app(env), host=env.get("APP_HOST", "127.0.0.1"),
                port=int(env.get("APP_PORT", "8000")), log_config=None,
                proxy_headers=True, forwarded_allow_ips=env.get("APP_TRUSTED_PROXIES", "127.0.0.1"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
