from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.auth.security import hash_password
from api.db.models.user import User
from api.db.session import get_db
from api.deps import require_role
from api.schemas import UserOut

router = APIRouter(prefix="/admin", tags=["admin"], dependencies=[Depends(require_role("admin"))])


class CreateUserRequest(BaseModel):
    email: str
    password: str
    role: str = "user"


@router.post("/users", response_model=UserOut, status_code=status.HTTP_201_CREATED)
async def create_user(payload: CreateUserRequest, db: AsyncSession = Depends(get_db)) -> User:
    if payload.role not in ("admin", "user"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid role")

    existing = await db.execute(select(User).where(User.email == payload.email))
    if existing.scalar_one_or_none() is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "Email already registered")

    user = User(email=payload.email, password_hash=hash_password(payload.password), role=payload.role)
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user
