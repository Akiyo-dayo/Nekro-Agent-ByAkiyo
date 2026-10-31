"""#64 守卫在 Postgres 上的方言缺陷：原生 SQL 的 `?` 占位符 asyncpg 不认

## 缺陷是什么

`DBModelGroup._preserve_undecryptable_api_key()` 为了绕开 ORM 的解密转换，用原生查询读
`api_key` 的原始列，写的是 `WHERE "id" = ?`。SQLite 驱动认 `?`，**asyncpg 不转换占位符**
（它只认 `$1`），Postgres 直接报语法错误。后果是：任何「内存 `api_key` 为空串的已有行」的
保存都失败——包括内置 `default` 组，以及密钥丢了之后解不开的行（它的属性恰是空串）。
而本仓库的测试一律跑 SQLite，所以这个守卫上线时 5400 多条用例全绿（PROJECT_STATE §2 新增
教训「SQLite 测试掩盖 Postgres 行为」）。

## 本文件钉的三件事

1. **静态**：`nekro_agent/` 下所有 `execute_query*` 调用的 SQL 里不得出现 `?` 占位符，
   并带反向对照证明判据抓得到（§2.34：源码级判据没有天然反例，恒真与正确长得一样）；
2. **真 Postgres 行为**：`NA_TEST_PG_DSN` 开门（没设就 skip；CI 里 service 容器会设），
   在一个独占的 schema 里建 `model_group` 表，覆盖空 Key 行保存成功、解不开的行
   保存其他字段后密文逐字不变且打 WARNING、显式清空仍然有效；
3. 以上与 SQLite 侧既有用例（`test_model_group_credential_hygiene.py`）互为两个方言的
   同一组语义。

## 运行方式

```
$env:NA_TEST_PG_DSN = "postgres://postgres@127.0.0.1:55499/natest"
.\\.venv\\Scripts\\python.exe -m pytest tests/test_model_group_postgres_dialect.py
```

用例只在自己建的随机 schema 里读写（`search_path` 指过去），结束时 `DROP SCHEMA ... CASCADE`，
不碰目标库里已有的任何表。
"""

import ast
import os
import re
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlencode

import pytest
from tortoise import Tortoise, connections
from tortoise.backends.base.executor import EXECUTOR_CACHE

from nekro_agent.core.os_env import OsEnv
from nekro_agent.models.db_model_group import DBModelGroup
from nekro_agent.tools.credential_cipher import (
    is_encrypted_envelope,
    reset_credential_cipher_cache,
)

PG_DSN_ENV = "NA_TEST_PG_DSN"

REPO_ROOT = Path(__file__).parent.parent
PACKAGE_ROOT = REPO_ROOT / "nekro_agent"

# 哨兵：自己编的，不是任何真实凭据
SENTINEL_KEY = "sk-pgdialect-SENTINEL-77f3a2"
SENTINEL_BASE_URL = "https://sentinel-pgdialect-upstream.example/v1"
KEY_MATERIAL_A = "pgdialect-SENTINEL-key-material-A"
KEY_MATERIAL_B = "pgdialect-SENTINEL-key-material-B-totally-unrelated"

# 会把 SQL 文本交给驱动的 Tortoise 连接方法族；`execute_query*` 是任务点名的范围，
# 同族的 insert/many/script 同样要遵守同一条方言约束（同一个 `?`、同一个 asyncpg）
_SQL_EXECUTORS = re.compile(r"^execute_(query\w*|insert|many|script)$")


# ============================================================
# 一、静态判据：原生 SQL 里没有 `?` 占位符
# ============================================================


def _literal_sql_text(node: ast.AST) -> Optional[str]:
    """尽力把一个 SQL 实参还原成它的字面文本；还原不了返回 None

    覆盖：字符串常量（相邻字面量拼接在解析期已合并）、f-string（`{...}` 部分记成 `{}`，
    占位符只会出现在常量片段里）、`+` 拼接。变量、函数调用等一律还原不了——
    调用方把「还原不了」也算违规，因为静态判据对它没有任何发言权。
    """
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        parts: List[str] = []
        for value in node.values:
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                parts.append(value.value)
            else:
                parts.append("{}")
        return "".join(parts)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left = _literal_sql_text(node.left)
        right = _literal_sql_text(node.right)
        if left is not None and right is not None:
            return left + right
    return None


def find_dialect_violations(source: str, filename: str = "<src>") -> Tuple[int, List[str]]:
    """扫一份源码里所有 SQL 执行调用，返回 (扫到的调用数, 违规描述列表)

    违规 = SQL 文本含 `?`，或 SQL 实参无法还原成字面文本。
    """
    tree = ast.parse(source, filename=filename)
    scanned = 0
    violations: List[str] = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            continue
        if not _SQL_EXECUTORS.match(node.func.attr):
            continue
        scanned += 1
        sql_arg: Optional[ast.AST] = node.args[0] if node.args else None
        if sql_arg is None:
            for keyword in node.keywords:
                if keyword.arg in ("query", "sql"):
                    sql_arg = keyword.value
        where = f"{filename}:{node.lineno} {node.func.attr}"
        if sql_arg is None:
            violations.append(f"{where}: 找不到 SQL 实参")
            continue
        text = _literal_sql_text(sql_arg)
        if text is None:
            violations.append(f"{where}: SQL 实参不是可静态还原的字面量，无法证明不含 `?`")
        elif "?" in text:
            violations.append(f"{where}: SQL 含 `?` 占位符（asyncpg 只认 `$n`，Postgres 上是语法错误）: {text!r}")
    return scanned, violations


class TestNoQuestionMarkPlaceholdersInRawSql:
    def test_no_execute_query_call_uses_a_question_mark_placeholder(self) -> None:
        total_scanned = 0
        violations: List[str] = []
        for path in sorted(PACKAGE_ROOT.rglob("*.py")):
            scanned, found = find_dialect_violations(path.read_text(encoding="utf-8"), str(path.relative_to(REPO_ROOT)))
            total_scanned += scanned
            violations.extend(found)
        # 非空对照：判据确实扫到了仓库里已知的几处原生 SQL（改名/搬家导致一处都扫不到时，
        # 「0 违规」就是一句空话）
        assert total_scanned >= 5, f"只扫到 {total_scanned} 处原生 SQL 调用，扫描范围或匹配规则失效"
        assert not violations, "原生 SQL 里有方言不安全的写法:\n" + "\n".join(violations)

    def test_criterion_catches_the_exact_pre_fix_shape(self) -> None:
        """反向对照：修复前那一句原样放进判据，必须被抓出来"""
        pre_fix = (
            "async def f(db, pk):\n"
            "    return await db.execute_query_dict(\n"
            '        \'SELECT "api_key" FROM "model_group" WHERE "id" = ?\',\n'
            "        [pk],\n"
            "    )\n"
        )
        scanned, violations = find_dialect_violations(pre_fix)
        assert scanned == 1
        assert len(violations) == 1 and "占位符" in violations[0]

    def test_criterion_catches_the_other_spellings(self) -> None:
        """f-string 常量片段、`+` 拼接、关键字实参、非字面量实参，各一条"""
        cases: Dict[str, str] = {
            "fstring": "async def f(db, t):\n    await db.execute_query(f'SELECT 1 FROM {t} WHERE a = ?')\n",
            "concat": "async def f(db):\n    await db.execute_query_dict('SELECT 1 ' + 'WHERE a = ?')\n",
            "keyword": "async def f(db):\n    await db.execute_query(query='SELECT 1 WHERE a = ?')\n",
            "variable": "async def f(db, sql):\n    await db.execute_query(sql)\n",
            "insert": "async def f(db):\n    await db.execute_insert('INSERT INTO t VALUES (?)', [1])\n",
        }
        for label, source in cases.items():
            scanned, violations = find_dialect_violations(source)
            assert scanned == 1, label
            assert len(violations) == 1, f"{label}: 判据没抓到 {source!r}"

    def test_criterion_does_not_flag_dialect_safe_spellings(self) -> None:
        """正向对照：整数内联、无参查询、`$n` 占位符都不该被误报"""
        safe_sources = [
            "async def f(db, pk):\n    await db.execute_query_dict(f'SELECT 1 WHERE \"id\" = {int(pk)}')\n",
            "async def f(db):\n    await db.execute_query_dict('SELECT 1')\n",
            "async def f(db):\n    await db.execute_query_dict('SELECT 1 WHERE a = $1', [1])\n",
            "async def f(db):\n    await db.fetch('SELECT 1 WHERE a = ?')\n",  # 不是 Tortoise 执行入口
        ]
        for source in safe_sources:
            _, violations = find_dialect_violations(source)
            assert not violations, f"误报: {source!r} -> {violations}"

    def test_reintroducing_the_bug_in_the_real_source_is_caught(self) -> None:
        """把真实源码里修好的那一句改回 `?`，判据必须当场红（变异对照）"""
        source = (PACKAGE_ROOT / "models" / "db_model_group.py").read_text(encoding="utf-8")
        _, clean = find_dialect_violations(source, "db_model_group.py")
        assert not clean, "前置：修复后的真实源码本身应当干净"

        needle = 'WHERE "id" = {int(self.pk)}\''
        assert needle in source, "修复后的写法找不到，本用例的变异锚点需要同步"
        broken = source.replace(needle, 'WHERE "id" = ?\'')
        assert broken != source
        _, violations = find_dialect_violations(broken, "db_model_group.py")
        assert violations, "把 `?` 改回去之后判据没有报警——它是恒真的"


# ============================================================
# 二、真 Postgres：#64 守卫在 asyncpg 上跑得通
# ============================================================


def _pg_dsn_or_skip() -> str:
    dsn = os.environ.get(PG_DSN_ENV, "")
    if not dsn:
        pytest.skip(
            f"需要真实 PostgreSQL：设 {PG_DSN_ENV}（如 postgres://postgres@127.0.0.1:55499/natest）。"
            "静态判据由 TestNoQuestionMarkPlaceholdersInRawSql 常绿把关；CI 的 backend-tests 工作流会设它。",
        )
    return dsn


def _dsn_with_schema(dsn: str, schema: str) -> str:
    sep = "&" if "?" in dsn else "?"
    return f"{dsn}{sep}{urlencode({'schema': schema})}"


@pytest.fixture
async def pg_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """独占 schema 里的一张真 `model_group` 表 + 与本机无关的加密环境

    表由 Tortoise 按模型定义建（与生产同一份 DDL 来源），只收 `db_model_group` 一个模块：
    本文件只关心这一张表，不需要把全库 schema 铺到目标 Postgres 上。
    """
    dsn = _pg_dsn_or_skip()
    import asyncpg

    schema = f"na_test_{uuid.uuid4().hex[:12]}"
    admin = await asyncpg.connect(dsn=dsn)
    await admin.execute(f'CREATE SCHEMA "{schema}"')

    monkeypatch.setattr(OsEnv, "DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(OsEnv, "CREDENTIAL_SECRET_KEY", KEY_MATERIAL_A)
    reset_credential_cipher_cache()

    # ⚠️ Tortoise 的 `EXECUTOR_CACHE` 是进程级的，键是 (连接名, schema, 表名)，**不含方言**：
    # 同一个进程里先有别的用例在 SQLite 上用过 `model_group`，缓存里就躺着带 SQLite 占位符的
    # INSERT 语句，这里再切到 Postgres 会原样复用它 —— 报的是一个毫不相干的
    # `syntax error at or near ","`，单跑本文件却全绿（全量跑才红）。
    # 进来前清、出去后再清（免得 `$n` 版本反过来漏给排在后面的 SQLite 用例）。
    EXECUTOR_CACHE.clear()
    await Tortoise.init(
        db_url=_dsn_with_schema(dsn, schema),
        modules={"models": ["nekro_agent.models.db_model_group"]},
    )
    await Tortoise.generate_schemas()
    try:
        yield schema
    finally:
        await Tortoise.close_connections()
        EXECUTOR_CACHE.clear()
        reset_credential_cipher_cache()
        await admin.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        await admin.close()


@pytest.fixture
def warn_logs():
    """抓住产品代码真正打出去的 WARNING（loguru 不进 stdlib logging，caplog 恒空）"""
    from loguru import logger

    captured: List[str] = []
    sink_id = logger.add(lambda message: captured.append(message.record["message"]), level="WARNING")
    try:
        yield captured
    finally:
        logger.remove(sink_id)


async def _create_group(name: str, api_key: str) -> DBModelGroup:
    return await DBModelGroup.create(
        name=name,
        ownership="platform",
        owner_tenant_id=None,
        visibility="private",
        group_name=name,
        chat_model="gpt-4o",
        base_url=SENTINEL_BASE_URL,
        api_key=api_key,
        model_type="chat",
    )


async def _raw_api_key(name: str) -> str:
    """原始列：绕开 ORM 的解密转换（拿 ORM 属性断言等于什么都没测）"""
    conn = connections.get("default")
    rows: List[Dict[str, Any]] = await conn.execute_query_dict(
        f'SELECT "api_key" FROM "model_group" WHERE "name" = \'{name}\'',
    )
    assert len(rows) == 1, f"期望恰好一行 name={name}，实际 {len(rows)} 行"
    return rows[0]["api_key"]


class TestGuardRunsOnRealPostgres:
    async def test_the_table_really_lives_in_a_real_postgres_schema(self, pg_env: str) -> None:
        """前置对照：连的确实是 Postgres，且表在本用例独占的 schema 里（不是误连了 SQLite）"""
        conn = connections.get("default")
        rows = await conn.execute_query_dict(
            "SELECT table_schema FROM information_schema.tables WHERE table_name = 'model_group'",
        )
        assert [row["table_schema"] for row in rows] == [pg_env]

    async def test_saving_an_existing_row_with_an_empty_key_succeeds(self, pg_env: str) -> None:
        """内置 `default` 组的形态：空 Key 的已有行，改别的字段并保存

        修复前守卫里的 `WHERE "id" = ?` 在这里抛 asyncpg 的语法错误。
        """
        await _create_group("default", "")
        row = await DBModelGroup.get(name="default")
        assert row.api_key == ""

        row.chat_model = "gpt-4o-mini"
        await row.save()

        assert (await DBModelGroup.get(name="default")).chat_model == "gpt-4o-mini"  # 保存真的执行了
        assert await _raw_api_key("default") == ""

    async def test_saving_other_fields_keeps_an_undecryptable_ciphertext_verbatim(
        self,
        pg_env: str,
        monkeypatch: pytest.MonkeyPatch,
        warn_logs: List[str],
    ) -> None:
        """#64 守卫的本职：解不开的密文，保存其他字段后逐字不变，并点名 WARNING"""
        await _create_group("locked_out", SENTINEL_KEY)
        stored = await _raw_api_key("locked_out")
        assert is_encrypted_envelope(stored), "前置对照：换密钥之前这行确实是密文"

        monkeypatch.setattr(OsEnv, "CREDENTIAL_SECRET_KEY", KEY_MATERIAL_B)
        reset_credential_cipher_cache()
        row = await DBModelGroup.get(name="locked_out")
        assert row.api_key == ""  # 复现起点：fail-closed，属性是空串

        row.chat_model = "gpt-4o-mini"
        await row.save()

        assert await _raw_api_key("locked_out") == stored
        assert (await DBModelGroup.get(name="locked_out")).chat_model == "gpt-4o-mini"
        named = [m for m in warn_logs if "locked_out" in m and "保留" in m]
        assert len(named) == 1, f"期望恰好一条点名 WARNING，实际 {named}"
        assert "NEKRO_CREDENTIAL_SECRET_KEY" in named[0]

        # 换回原密钥仍能解出原值——密文确实还"活着"
        monkeypatch.setattr(OsEnv, "CREDENTIAL_SECRET_KEY", KEY_MATERIAL_A)
        reset_credential_cipher_cache()
        assert (await DBModelGroup.get(name="locked_out")).api_key == SENTINEL_KEY

    async def test_explicit_clear_on_a_readable_row_still_clears(self, pg_env: str) -> None:
        """守卫不能误堵合法清空：解得开的行上提交空串，在 Postgres 上同样真的清掉"""
        await _create_group("clearable", SENTINEL_KEY)
        assert is_encrypted_envelope(await _raw_api_key("clearable"))

        row = await DBModelGroup.get(name="clearable")
        row.api_key = ""
        await row.save()

        assert await _raw_api_key("clearable") == ""
