from __future__ import annotations

import asyncio
from functools import partial

from sqlalchemy import (
    distinct,
    func,
    insert,
    select,
    update,
)
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.functions import coalesce

from phoenix.auth import compute_password_hash
from phoenix.config import ENABLE_AUTH, PHOENIX_SECRET
from phoenix.db import models
from phoenix.db.enums import COLUMN_ENUMS, AuthMethod, UserRole
from phoenix.server.types import DbSessionFactory


class Facilitator:
    """
    Facilitates the creation of database records necessary for Phoenix to function. This includes
    ensuring that all enum values are present in their respective tables, ensuring that all user
    roles are present, and ensuring that the admin user has a password hash. These tasks will be
    carried out as callbacks at the very beginning of Starlette's lifespan process.
    """

    def __init__(self, db: DbSessionFactory) -> None:
        self._db = db

    async def __call__(self) -> None:
        async with self._db() as session:
            for fn in (
                _ensure_enums,
                _ensure_user_roles,
                *((_ensure_admin_password,) if ENABLE_AUTH else ()),
            ):
                async with session.begin_nested():
                    await fn(session)


async def _ensure_enums(session: AsyncSession) -> None:
    """
    Ensure that all enum values are present in their respective tables. If any values are missing,
    they will be added. If any values are present in the database but not in the enum, an error will
    be raised. This function is idempotent: it will not add duplicate values to the database.
    """
    for column, enum in COLUMN_ENUMS.items():
        table = column.class_
        existing = set([_ async for _ in await session.stream_scalars(select(distinct(column)))])
        expected = set(e.value for e in enum)
        if unexpected := existing - expected:
            raise ValueError(f"Unexpected values in {table.name}.{column.key}: {unexpected}")
        if not (missing := expected - existing):
            continue
        await session.execute(insert(table), [{column.key: v} for v in missing])


async def _ensure_user_roles(session: AsyncSession) -> None:
    """
    Ensure that the system and admin roles are present in the database. If they are not, they will
    be added. The system user will have the email "system@localhost" and the admin user will have
    the email "admin@localhost".
    """
    role_ids = {
        name: id_
        async for name, id_ in await session.stream(
            select(models.UserRole.name, models.UserRole.id)
        )
    }
    existing_roles = [
        name
        async for name in await session.stream_scalars(
            select(distinct(models.UserRole.name)).join_from(models.User, models.UserRole)
        )
    ]
    if (system_role := UserRole.SYSTEM.value) not in existing_roles and (
        system_role_id := role_ids.get(system_role)
    ) is not None:
        system_user = models.User(
            user_role_id=system_role_id,
            email="system@localhost",
            auth_method=AuthMethod.LOCAL.value,
            reset_password=False,
        )
        session.add(system_user)
    if (admin_role := UserRole.ADMIN.value) not in existing_roles and (
        admin_role_id := role_ids.get(admin_role)
    ) is not None:
        admin_user = models.User(
            user_role_id=admin_role_id,
            username="admin",
            email="admin@localhost",
            auth_method=AuthMethod.LOCAL.value,
            reset_password=True,
        )
        session.add(admin_user)
    await session.flush()


async def _ensure_admin_password(session: AsyncSession) -> None:
    """
    Ensure that the first admin user with the LOCAL auth method in the database has a password
    hash. If that admin user already has a password hash, this function will do nothing.
    """
    assert PHOENIX_SECRET
    loop = asyncio.get_running_loop()
    compute = partial(compute_password_hash, password=PHOENIX_SECRET, salt=PHOENIX_SECRET)
    hash_ = await loop.run_in_executor(None, compute)
    password_hash = coalesce(models.User.password_hash, hash_)
    first_local_admin = (
        select(func.min(models.User.id))
        .join(models.UserRole)
        .where(models.UserRole.name == UserRole.ADMIN.value)
        .where(models.User.auth_method == AuthMethod.LOCAL.value)
        .scalar_subquery()
    )
    stmt = (
        update(models.User)
        .where(models.User.id == first_local_admin)
        .values(password_hash=password_hash)
    )
    await session.execute(stmt)
