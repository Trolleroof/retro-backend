"""Generate one opaque token and a D1 insert containing only its digest."""

import hashlib
import secrets
import sys


def main() -> None:
    token = secrets.token_urlsafe(32)
    digest = hashlib.sha256(token.encode("utf-8")).hexdigest()

    print(token, file=sys.stderr)
    print(
        "INSERT INTO users (id, auth_token_hash) "
        f"VALUES (?1, '{digest}');"
    )
    print(
        "INSERT INTO auth_sessions (token_hash, user_id) "
        f"VALUES ('{digest}', ?1);"
    )


if __name__ == "__main__":
    main()
