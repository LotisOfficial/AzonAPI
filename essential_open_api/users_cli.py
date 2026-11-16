"""Manage users.xml for the AZON Admin API.

    python -m essential_open_api.users_cli genkey
    python -m essential_open_api.users_cli add alice --role editor --first-name Alice --last-name Smith
    python -m essential_open_api.users_cli passwd alice
    python -m essential_open_api.users_cli totp alice
    python -m essential_open_api.users_cli role alice reader
    python -m essential_open_api.users_cli status alice disabled
    python -m essential_open_api.users_cli list

Reads AZON_CONFIG_DIR (the folder holding users.xml) and AZON_TOTP_KEY from the environment.
The API picks up changes without a restart.
"""

from __future__ import annotations

import argparse
import base64
import getpass
import os
import secrets
import sys
from pathlib import Path
from urllib.parse import quote

from .auth import (ROLES, STATUSES, AuthConfigError, User, encrypt_seed, hash_password, load_users,
                   parse_totp_key, save_users)

MIN_PASSWORD_LENGTH = 12


def users_path() -> Path:
    config_dir = (os.environ.get("AZON_CONFIG_DIR") or "").strip()
    if not config_dir or not Path(config_dir).is_dir():
        sys.exit("AZON_CONFIG_DIR must point at an existing folder (outside the web application).")
    return Path(config_dir) / "users.xml"


def read_password(from_stdin: bool) -> str:
    if from_stdin:
        password = sys.stdin.readline().rstrip("\r\n")
    else:
        password = getpass.getpass("New password: ")
        if getpass.getpass("Repeat password: ") != password:
            sys.exit("The passwords do not match.")
    if len(password) < MIN_PASSWORD_LENGTH:
        sys.exit(f"The password must be at least {MIN_PASSWORD_LENGTH} characters.")
    return password


def new_totp(user: User) -> None:
    """Generate a seed, store it encrypted, and print what the authenticator app needs."""
    key = parse_totp_key(os.environ.get("AZON_TOTP_KEY"))
    seed = secrets.token_bytes(20)
    user.totp_secret = encrypt_seed(key, seed)
    b32 = base64.b32encode(seed).decode("ascii")
    label = quote(f"AZON Admin:{user.username}")
    print("\nAdd this account to an authenticator app. This is shown once.")
    print(f"  Secret key : {b32}")
    print(f"  Setup link : otpauth://totp/{label}?secret={b32}&issuer={quote('AZON Admin')}&algorithm=SHA1&digits=6&period=30\n")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Manage AZON Admin users (users.xml).")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("genkey", help="print a new AZON_TOTP_KEY")
    sub.add_parser("list", help="list users")

    add = sub.add_parser("add", help="add a user")
    add.add_argument("username")
    add.add_argument("--role", choices=sorted(ROLES), default="reader")
    add.add_argument("--first-name", default="")
    add.add_argument("--last-name", default="")
    add.add_argument("--email", default="")
    add.add_argument("--ea-user", default="", help="EA_User name in the repository, for the Viewer")
    add.add_argument("--no-totp", action="store_true", help="do not enrol an authenticator now")
    add.add_argument("--password-stdin", action="store_true")

    passwd = sub.add_parser("passwd", help="set a user's password")
    passwd.add_argument("username")
    passwd.add_argument("--password-stdin", action="store_true")

    totp = sub.add_parser("totp", help="enrol or replace a user's authenticator")
    totp.add_argument("username")

    role = sub.add_parser("role", help="set a user's role")
    role.add_argument("username")
    role.add_argument("role", choices=sorted(ROLES))

    status = sub.add_parser("status", help="set a user's status")
    status.add_argument("username")
    status.add_argument("status", choices=sorted(STATUSES))

    args = parser.parse_args(argv)

    if args.command == "genkey":
        print(base64.b64encode(secrets.token_bytes(32)).decode("ascii"))
        return 0

    path = users_path()
    try:
        users = load_users(path) if path.exists() else {}
    except AuthConfigError as exc:
        sys.exit(str(exc))

    if args.command == "list":
        for user in sorted(users.values(), key=lambda u: u.username.lower()):
            print(f"{user.username:24} {user.role:7} {user.status:9} totp={'yes' if user.totp_secret else 'no '} {user.full_name}")
        return 0

    key = args.username.strip().lower()
    if args.command == "add":
        if key in users:
            sys.exit(f"User {args.username!r} already exists.")
        user = User(username=args.username.strip(), password_hash=hash_password(read_password(args.password_stdin)),
                    role=args.role, first_name=args.first_name, last_name=args.last_name, email=args.email,
                    ea_user_name=args.ea_user)
        if not args.no_totp:
            new_totp(user)
        users[key] = user
    else:
        user = users.get(key)
        if user is None:
            sys.exit(f"No user {args.username!r}.")
        if args.command == "passwd":
            user.password_hash = hash_password(read_password(args.password_stdin))
            user.must_change_password = False
        elif args.command == "totp":
            new_totp(user)
        elif args.command == "role":
            user.role = args.role
        elif args.command == "status":
            user.status = args.status

    save_users(path, users)
    print(f"Saved {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
