"""Set a VoxPath account's password from the command line (for whoever runs the server).

    cd backend
    .venv\\Scripts\\python reset_password.py krish              (asks for the new password)
    .venv\\Scripts\\python reset_password.py you@example.com

The password is typed twice and never shown, echoed or logged. Use the app's
"Forgot password?" link when the backend is running; this is for when it isn't.
"""

from __future__ import annotations

import argparse
import getpass
import os
import sys
from pathlib import Path

import psycopg
from dotenv import load_dotenv

load_dotenv(Path(__file__).with_name(".env"))

import auth  # noqa: E402  (after load_dotenv: auth reads settings from the environment)


def main() -> int:
    parser = argparse.ArgumentParser(description="Set a new password for a VoxPath account.")
    parser.add_argument("account", help="username or email")
    identifier = parser.parse_args().account.strip().lower()

    url = os.getenv("DATABASE_URL")
    if not url:
        print("DATABASE_URL is not set in backend/.env.", file=sys.stderr)
        return 1
    with psycopg.connect(url) as conn:
        row = conn.execute(
            "SELECT id, username FROM users WHERE username = %s OR email = %s LIMIT 1", (identifier, identifier)
        ).fetchone()
        if not row:
            print(f"No account called {identifier!r}.", file=sys.stderr)
            return 1
        user_id, username = row

        password = getpass.getpass(f"New password for {username}: ")
        if len(password) < auth.MIN_PASSWORD_CHARS:
            print(f"Too short: use at least {auth.MIN_PASSWORD_CHARS} characters.", file=sys.stderr)
            return 1
        if getpass.getpass("Type it again: ") != password:
            print("The two passwords don't match; nothing was changed.", file=sys.stderr)
            return 1
        try:
            new_hash = auth.hash_password(password)
        except Exception as error:  # e.g. longer than bcrypt's 72 bytes
            print(getattr(error, "detail", str(error)), file=sys.stderr)
            return 1
        conn.execute("UPDATE users SET password_hash = %s WHERE id = %s", (new_hash, user_id))
        # Any pending reset code is now moot (the table appears once the new backend has run).
        if conn.execute("SELECT to_regclass('password_resets')").fetchone()[0]:
            conn.execute("DELETE FROM password_resets WHERE user_id = %s", (user_id,))
    print(f"Password changed for {username}. Sign in with the new one.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
