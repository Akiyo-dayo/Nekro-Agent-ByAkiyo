"""迁移里给已有表加非空列必须带默认值

PostgreSQL 给有数据的表 `ADD … NOT NULL` 而不带 `DEFAULT` 会直接报错，整个迁移回滚，升级后的
实例起不来；在空表上实测却看不出来。这里扫一遍 `migrations/models` 里每个迁移的升级与降级两段，
有这种语句就红。aerich 生成的加列语句不一定带默认值（JSON 列尤其如此），生成后要手工补上
（照 `'{}'::jsonb` 的写法）。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

MIGRATIONS = Path(__file__).resolve().parents[1] / "migrations" / "models"

_ADD_NOT_NULL_WITHOUT_DEFAULT = re.compile(
    r'ALTER\s+TABLE\s+"(?P<table>[^"]+)"\s+ADD\s+(?:COLUMN\s+)?(?:IF\s+NOT\s+EXISTS\s+)?"(?P<column>[^"]+)"'
    r"(?P<rest>[^;]*);",
    re.IGNORECASE,
)


def offending_statements(sql: str) -> list[str]:
    """给出 `sql` 里加了非空列却没带默认值的语句（表名.列名）"""
    found: list[str] = []
    for match in _ADD_NOT_NULL_WITHOUT_DEFAULT.finditer(sql):
        rest = match.group("rest").upper()
        if "NOT NULL" in rest and "DEFAULT" not in rest:
            found.append(f"{match.group('table')}.{match.group('column')}")
    return found


@pytest.mark.parametrize(
    ("sql", "expected"),
    [
        ('ALTER TABLE "t" ADD "c" JSONB NOT NULL;', ["t.c"]),
        ('alter table "t" add column if not exists "c" int not null;', ["t.c"]),
        ('ALTER TABLE "t" ADD "c" JSONB NOT NULL DEFAULT \'{}\'::jsonb;', []),
        ('ALTER TABLE "t" ADD "c" INT NOT NULL DEFAULT 0;', []),
        ('ALTER TABLE "t" ADD "c" VARCHAR(16);', []),
        ('ALTER TABLE "t" ADD "c" TEXT NULL;', []),
    ],
)
def test_the_scanner_itself(sql: str, expected: list[str]) -> None:
    assert offending_statements(sql) == expected


def test_migration_files_are_found() -> None:
    assert len(list(MIGRATIONS.glob("*.py"))) > 40


def test_no_migration_adds_a_not_null_column_without_default() -> None:
    offenders: list[str] = []
    for path in sorted(MIGRATIONS.glob("*.py")):
        text = path.read_text(encoding="utf-8")
        for statement in offending_statements(text):
            offenders.append(f"{path.name}: {statement}")

    assert offenders == []
