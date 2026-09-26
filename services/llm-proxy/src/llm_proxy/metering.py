import uuid
from datetime import date
from decimal import Decimal

from sqlalchemy import insert
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from shared.db.models import TokenUsage, UserUsageCounter, UserUsageLimit

# Approximate list prices in USD per million tokens (input, output), matched by substring
# against the model id. Adjust to your actual Anthropic plan; unknown models fall back to
# the "sonnet" tier so an unrecognized/new model id still gets metered rather than skipped.
_PRICING_PER_MTOK_USD: dict[str, tuple[Decimal, Decimal]] = {
    "opus": (Decimal("15"), Decimal("75")),
    "sonnet": (Decimal("3"), Decimal("15")),
    "haiku": (Decimal("0.80"), Decimal("4")),
}
_DEFAULT_PRICING = _PRICING_PER_MTOK_USD["sonnet"]


class BudgetExceededError(Exception):
    """Raised when a user's spend for the current period already meets or exceeds their budget."""


def _current_period_start() -> date:
    return date.today().replace(day=1)


def _price_per_mtok(model: str) -> tuple[Decimal, Decimal]:
    model_lower = model.lower()
    for key, price in _PRICING_PER_MTOK_USD.items():
        if key in model_lower:
            return price
    return _DEFAULT_PRICING


def estimate_cost_usd(model: str, input_tokens: int, output_tokens: int) -> Decimal:
    input_price, output_price = _price_per_mtok(model)
    return (Decimal(input_tokens) * input_price + Decimal(output_tokens) * output_price) / Decimal(
        1_000_000
    )


async def ensure_within_budget(db: AsyncSession, user_id: uuid.UUID) -> None:
    """Raise BudgetExceededError if the user has no budget left for the current period.

    A user with no `user_usage_limits` row, or a null `monthly_budget_usd`, is unlimited.
    """
    limit = await db.get(UserUsageLimit, user_id)
    if limit is None or limit.monthly_budget_usd is None:
        return

    counter = await db.get(UserUsageCounter, (user_id, _current_period_start()))
    spent = counter.spent_usd if counter is not None else Decimal("0")
    if spent >= limit.monthly_budget_usd:
        raise BudgetExceededError(
            f"Monthly budget of ${limit.monthly_budget_usd} exceeded (spent ${spent})."
        )


async def record_usage(
    db: AsyncSession,
    *,
    session_id: uuid.UUID,
    user_id: uuid.UUID,
    model: str,
    input_tokens: int,
    output_tokens: int,
) -> None:
    # Core statements on the tables, not `db.add()`: an ORM flush sorts every table in the shared
    # `MetaData` by foreign key, and `users` (owned by `api`, never imported here) can't be resolved.
    # The counter upsert also keeps concurrent requests from racing to create the same period row.
    await db.execute(
        insert(TokenUsage.__table__).values(
            session_id=session_id,
            user_id=user_id,
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )
    )
    cost = estimate_cost_usd(model, input_tokens, output_tokens)
    counters = UserUsageCounter.__table__
    upsert = pg_insert(counters).values(user_id=user_id, period_start=_current_period_start(), spent_usd=cost)
    await db.execute(
        upsert.on_conflict_do_update(
            index_elements=[counters.c.user_id, counters.c.period_start],
            set_={"spent_usd": counters.c.spent_usd + upsert.excluded.spent_usd},
        )
    )
    await db.commit()
