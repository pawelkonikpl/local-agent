"""Service-to-service auth: one shared `INTERNAL_PROXY_TOKEN`, sent as a bearer token.

Kept in env / podman secrets on both sides, never in Postgres.
"""

import secrets


def bearer_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def is_valid_bearer(authorization: str | None, expected: str | None) -> bool:
    """Whether an `Authorization` header carries `expected`, compared in constant time.

    An unset `expected` never matches, so a service missing its token refuses every caller.
    """
    if not expected:
        return False
    scheme, _, token = (authorization or "").partition(" ")
    return scheme.lower() == "bearer" and secrets.compare_digest(token, expected)
