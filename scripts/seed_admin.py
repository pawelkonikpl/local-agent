"""Idempotent admin provisioning. Run with `uv run python scripts/seed_admin.py`.

Reads SEED_ADMIN_EMAIL / SEED_ADMIN_PASSWORD from the environment; there is no
public registration, so this is the only way to create the first account.
"""

import asyncio
import os
import sys

from sqlalchemy import select

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "services", "api", "src"))

from api.auth.security import hash_password  # noqa: E402
from api.db.models.user import User  # noqa: E402
from api.db.session import AsyncSessionLocal  # noqa: E402


async def seed_admin() -> None:
    email = os.environ["SEED_ADMIN_EMAIL"]
    password = os.environ["SEED_ADMIN_PASSWORD"]

    async with AsyncSessionLocal() as db:
        existing = await db.execute(select(User).where(User.email == email))
        if existing.scalar_one_or_none() is not None:
            print(f"Admin user {email} already exists, skipping.")
            return

        db.add(User(email=email, password_hash=hash_password(password), role="admin"))
        await db.commit()
        print(f"Created admin user {email}.")


if __name__ == "__main__":
    asyncio.run(seed_admin())
