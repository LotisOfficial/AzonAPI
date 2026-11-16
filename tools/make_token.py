"""Generate an API token and the matching entry for the token file.

    python tools/make_token.py <name> <read|write|admin>

The token is printed once. Store it in a password manager; only the SHA-256
digest goes into the file named by AZON_API_TOKENS_FILE.
"""

import hashlib
import json
import secrets
import sys

ROLES = ("read", "write", "admin")


def main() -> int:
    if len(sys.argv) != 3 or sys.argv[2] not in ROLES:
        print(__doc__)
        return 2
    name, role = sys.argv[1], sys.argv[2]
    token = secrets.token_urlsafe(32)
    entry = {"name": name, "role": role, "sha256": hashlib.sha256(token.encode()).hexdigest()}
    print("Token (shown once, keep it secret):")
    print(f"  {token}")
    print("Add this entry to the JSON list in your token file:")
    print(f"  {json.dumps(entry)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
