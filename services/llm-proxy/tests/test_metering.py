import uuid
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from llm_proxy import metering
from shared.db.models import ChatSession, UserUsageCounter, UserUsageLimit

_PERIOD_START = date.today().replace(day=1)


async def test_ensure_within_budget_allows_user_with_no_limit_row(
    db_session: AsyncSession, seed_user: uuid.UUID
) -> None:
    await metering.ensure_within_budget(db_session, seed_user)


async def test_ensure_within_budget_allows_unlimited_budget(
    db_session: AsyncSession, seed_user: uuid.UUID
) -> None:
    db_session.add(UserUsageLimit(user_id=seed_user, monthly_budget_usd=None))
    await db_session.flush()

    await metering.ensure_within_budget(db_session, seed_user)


async def test_ensure_within_budget_raises_when_spend_meets_limit(
    db_session: AsyncSession, seed_user: uuid.UUID
) -> None:
    db_session.add(UserUsageLimit(user_id=seed_user, monthly_budget_usd=Decimal("1.00")))
    db_session.add(
        UserUsageCounter(user_id=seed_user, period_start=_PERIOD_START, spent_usd=Decimal("1.00"))
    )
    await db_session.flush()

    with pytest.raises(metering.BudgetExceededError):
        await metering.ensure_within_budget(db_session, seed_user)


async def test_ensure_within_budget_allows_spend_below_limit(
    db_session: AsyncSession, seed_user: uuid.UUID
) -> None:
    db_session.add(UserUsageLimit(user_id=seed_user, monthly_budget_usd=Decimal("5.00")))
    db_session.add(
        UserUsageCounter(user_id=seed_user, period_start=_PERIOD_START, spent_usd=Decimal("1.00"))
    )
    await db_session.flush()

    await metering.ensure_within_budget(db_session, seed_user)


async def test_record_usage_creates_token_usage_row_and_increments_counter(
    db_session: AsyncSession, seed_user: uuid.UUID
) -> None:
    session_id = uuid.uuid4()
    db_session.add(ChatSession(id=session_id, user_id=seed_user))
    await db_session.flush()

    await metering.record_usage(
        db_session,
        session_id=session_id,
        user_id=seed_user,
        model="claude-sonnet-5",
        input_tokens=1000,
        output_tokens=500,
    )

    counter = await db_session.get(UserUsageCounter, (seed_user, _PERIOD_START))
    assert counter is not None
    assert counter.spent_usd == metering.estimate_cost_usd("claude-sonnet-5", 1000, 500)


async def test_record_usage_accumulates_across_multiple_calls(
    db_session: AsyncSession, seed_user: uuid.UUID
) -> None:
    session_id = uuid.uuid4()
    db_session.add(ChatSession(id=session_id, user_id=seed_user))
    await db_session.flush()

    for _ in range(2):
        await metering.record_usage(
            db_session,
            session_id=session_id,
            user_id=seed_user,
            model="claude-haiku-4-5",
            input_tokens=1_000_000,
            output_tokens=0,
        )

    counter = await db_session.get(UserUsageCounter, (seed_user, _PERIOD_START))
    assert counter is not None
    assert counter.spent_usd == Decimal("1.60")


@pytest.mark.parametrize(
    ("model", "expected"),
    [("gpt-6-astra", Decimal("60")), ("gpt-6-sol", Decimal("12")), ("gpt-6-luna", Decimal("0.60"))],
)
def test_estimate_cost_usd_prices_openai_models_by_their_own_tier(model: str, expected: Decimal) -> None:
    assert metering.estimate_cost_usd(model, 1_000_000, 1_000_000) == expected
