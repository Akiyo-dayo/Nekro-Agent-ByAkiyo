"""#70: 平台用户登记的并发安全与联合键唯一性

根因（测试部署实证）：`collector` 的「先 `get_or_none` 再 `user_register`
再 `get_by_union_id`」是 check-then-create——nonebot 每条消息一个并发 task，
同一新用户的首批消息会全部穿过存在性检查、全部插入；`user` 表上又没有
`(adapter_key, platform_userid)` 唯一约束。重复行落地之后，当事人**每一条**
消息都在 `get_or_none` 处抛 `MultipleObjectsReturned`，整条 matcher 挂掉。

修复分三层，本文件逐层钉住：
1. `user` 表部分唯一索引（迁移 24，`WHERE adapter_key <> ''`——`('','')` 空键是
   控制台账号的合法共享形态，不在约束内）；
2. `get_or_create_platform_user`：先查后建 + `IntegrityError` 回读赢家行；
3. `get_by_union_id`：脏数据面前确定性取最早行 + 告警，不抛
   `MultipleObjectsReturned`（与 `DBAdapterInstance.get_by_bound_account`
   同一条规矩：入站链路不能被脏数据拖垮）。
"""

import pathlib
import re

import pytest
from tortoise import Tortoise
from tortoise.exceptions import IntegrityError
from tortoise.timezone import now as db_now

from nekro_agent.models.db_user import DBUser
from nekro_agent.schemas.errors import ConflictError
from nekro_agent.schemas.user import UserCreate
from nekro_agent.services.user.perm import Role
from nekro_agent.services.user.util import get_or_create_platform_user, user_register


@pytest.fixture
async def db():
    await Tortoise.init(db_url="sqlite://:memory:", modules={"models": ["nekro_agent.models"]})
    await Tortoise.generate_schemas()
    yield
    await Tortoise.close_connections()


def _user_create(username: str = "用户甲", platform_userid: str = "12345") -> UserCreate:
    return UserCreate(username=username, password="", adapter_key="onebot_v11", platform_userid=platform_userid)


class TestGetOrCreatePlatformUser:
    async def test_creates_row_for_new_user(self, db) -> None:
        user = await get_or_create_platform_user("onebot_v11", "12345", "新用户")

        assert user is not None
        assert user.username == "新用户"
        assert user.adapter_key == "onebot_v11"
        assert user.platform_userid == "12345"
        assert user.perm_level == Role.User
        assert await DBUser.all().count() == 1

    async def test_second_call_returns_the_same_row(self, db) -> None:
        """同一联合键的第二次登记必须落回同一行——昵称变化不改写（与原实现一致）"""
        first = await get_or_create_platform_user("onebot_v11", "12345", "旧昵称")
        second = await get_or_create_platform_user("onebot_v11", "12345", "新昵称")

        assert second is not None and first is not None
        assert second.id == first.id
        assert second.username == "旧昵称"
        assert await DBUser.all().count() == 1

    async def test_different_users_do_not_collide(self, db) -> None:
        a = await get_or_create_platform_user("onebot_v11", "12345", "甲")
        b = await get_or_create_platform_user("onebot_v11", "67890", "乙")

        assert a is not None and b is not None
        assert a.id != b.id
        assert await DBUser.all().count() == 2

    async def test_admin_nickname_is_refused(self, db) -> None:
        """`user_login` 按 username 单列查 admin：放进一个同名平台用户会把后台登录
        炸成 MultipleObjectsReturned，所以登记侧必须继续把它护住。"""
        assert await get_or_create_platform_user("onebot_v11", "99999", "admin") is None
        assert await DBUser.all().count() == 0

    async def test_admin_nickname_guard_only_blocks_creation(self, db) -> None:
        """保留名检查只拦「新建」：已登记的用户把平台昵称改成 admin 后，照常取回。

        检查若排在查库之前，这个老用户的每条消息都会被静默丢弃（collector 见 None 就 return）。
        保留名守的是 `user_login` 按 username 单列查 admin 的歧义——那是**建行**才会造成的，
        取回一个早已存在的行不增加任何同名行。
        """
        first = await get_or_create_platform_user("onebot_v11", "12345", "普通昵称")
        assert first is not None

        again = await get_or_create_platform_user("onebot_v11", "12345", "admin")

        assert again is not None, "已存在的用户改昵称为 admin 后消息被静默丢弃"
        assert again.id == first.id
        # 取回不改写库里的昵称，也没有多出一行
        assert again.username == "普通昵称"
        assert await DBUser.all().count() == 1

    async def test_admin_nickname_guard_still_blocks_a_brand_new_user_among_existing(self, db) -> None:
        """反向对照：库里已有别人时，新联合键 + admin 昵称依旧不建行"""
        await get_or_create_platform_user("onebot_v11", "12345", "别人")

        assert await get_or_create_platform_user("onebot_v11", "67890", "admin") is None
        assert await DBUser.all().count() == 1

    async def test_losing_the_insert_race_falls_back_to_the_winner(self, db, monkeypatch) -> None:
        """并发下输掉插入竞争的一方必须回读赢家那行，而不是让 IntegrityError 冲出链路

        真库上的交错（唯一约束 + 真 IntegrityError）无法在 sqlite 复现，
        这里伪造那一个交错：在"我查过了、还没插"的窗口里让赢家落库，
        并让 create 抛 IntegrityError——随后回读与判据全部是真的。
        """
        real_create = DBUser.create
        raced = {"done": False}

        async def winner_lands_first(*args, **kwargs):
            if not raced["done"]:
                raced["done"] = True
                await real_create(
                    username="赢家",
                    password="x",
                    adapter_key="onebot_v11",
                    platform_userid="12345",
                    perm_level=Role.User,
                    login_time=db_now(),
                )
                raise IntegrityError("duplicate key value violates unique constraint")
            return await real_create(*args, **kwargs)

        monkeypatch.setattr(DBUser, "create", winner_lands_first)

        user = await get_or_create_platform_user("onebot_v11", "12345", "输家")

        assert raced["done"], "没走到插入竞争那一步，这条用例没有测到它要测的东西"
        assert user is not None and user.username == "赢家"
        assert await DBUser.all().count() == 1

    async def test_integrity_error_without_winner_still_raises(self, db, monkeypatch) -> None:
        """IntegrityError 之后回读不到行 = 约束违例不是联合键撞车，如实抛出"""

        async def boom(*args, **kwargs):
            raise IntegrityError("some other constraint")

        monkeypatch.setattr(DBUser, "create", boom)

        with pytest.raises(IntegrityError):
            await get_or_create_platform_user("onebot_v11", "12345", "用户")


class TestUserRegisterRaceGuard:
    async def test_duplicate_pair_raises_conflict(self, db) -> None:
        await user_register(_user_create())
        with pytest.raises(ConflictError):
            await user_register(_user_create(username="换了昵称"))

    async def test_integrity_error_maps_to_conflict(self, db, monkeypatch) -> None:
        """唯一约束兜底那一路（并发穿过存在性检查）翻译成「用户已存在」而不是 500"""

        async def boom(*args, **kwargs):
            raise IntegrityError("duplicate key")

        monkeypatch.setattr(DBUser, "create", boom)

        with pytest.raises(ConflictError):
            await user_register(_user_create())


class TestGetByUnionIdToleratesDirtyData:
    async def test_duplicate_rows_resolve_to_the_earliest(self, db) -> None:
        """存量重复行（约束缺席的库遗留）不抛 MultipleObjectsReturned，确定性取最早行"""
        kwargs = {
            "password": "x",
            "adapter_key": "onebot_v11",
            "platform_userid": "12345",
            "perm_level": Role.User,
            "login_time": db_now(),
        }
        first = await DBUser.create(username="最早", **kwargs)
        await DBUser.create(username="后来者", **kwargs)

        user = await DBUser.get_by_union_id("onebot_v11", "12345")

        assert user is not None
        assert user.id == first.id
        assert user.username == "最早"
        # 用例本身确实制造了脏数据（否则上面的断言可能是空转）
        assert await DBUser.filter(adapter_key="onebot_v11", platform_userid="12345").count() == 2

    async def test_missing_user_returns_none(self, db) -> None:
        assert await DBUser.get_by_union_id("onebot_v11", "nobody") is None


class TestMigration24Contract:
    """迁移 24 是手工编写的 RunSQL 迁移（aerich 表达不了部分唯一索引）——
    这里钉住它的两个必要动作与「模型未变」的前提。"""

    def _migration_24(self) -> str:
        candidates = sorted(pathlib.Path("migrations/models").glob("24_*.py"))
        assert candidates, "迁移 24（联合键部分唯一索引）不存在"
        return "\n".join(p.read_text(encoding="utf-8") for p in candidates)

    def test_migration_dedups_then_creates_partial_unique_index(self) -> None:
        sql = self._migration_24()

        # 先清存量重复行（保留最早行），否则建索引当场失败
        assert re.search(r'DELETE FROM "user"', sql)
        assert re.search(r'a\."id" > b\."id"', sql)
        # 部分唯一索引：空键（控制台账号）不进约束
        assert re.search(r'CREATE UNIQUE INDEX.*"user"', sql, re.S)
        assert "adapter_key" in sql and "platform_userid" in sql
        assert re.search(r"WHERE.*adapter_key.*<>.*''", sql)
        # 反向对照：裸 UNIQUE 会把控制台账号的 ('','') 空键撞碎，不许没有 WHERE
        index_stmt = re.search(r'CREATE UNIQUE INDEX[^;]+;', sql, re.S)
        assert index_stmt and "WHERE" in index_stmt.group(0)

    def test_migration_24_keeps_models_state_identical_to_23(self) -> None:
        """MODELS_STATE 必须与 23 逐字相同：本迁移只动索引与存量数据，模型零变化。
        这同时是「手工迁移没写歪」的护栏——state 错了 aerich 下次 diff 就乱。"""

        def state_of(pattern: str) -> str:
            text = "\n".join(
                p.read_text(encoding="utf-8") for p in pathlib.Path("migrations/models").glob(pattern)
            )
            m = re.search(r"MODELS_STATE = \(.*?\)\s*$", text, re.S)
            assert m, f"{pattern} 里找不到 MODELS_STATE"
            return m.group(0)

        assert state_of("24_*.py") == state_of("23_*.py")
