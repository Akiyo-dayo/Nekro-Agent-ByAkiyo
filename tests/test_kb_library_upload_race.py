"""全局素材库并发上传同内容文件：唯一约束冲突时不留孤儿源文件

两个请求同时查不到同内容的素材、各自把源文件写到自己的路径，后插入的那个撞上
`content_hash` 唯一约束。它的源文件没有任何记录指向，复用已有素材与重新抛错两条出路都必须删掉它；
否则以后往同一路径传不同内容会报"目标路径已存在"。
"""

import io
from pathlib import Path
from typing import Any, List, Optional

import pytest
from fastapi import UploadFile
from tortoise.exceptions import IntegrityError

from nekro_agent.services.kb import library_service


class _FakeAssetModel:
    """`get_or_none` 第一次查不到（并发窗口里），`create` 撞唯一约束，之后再查拿到对方那条"""

    def __init__(self, existing_after_conflict: Optional[object]) -> None:
        self.lookups: List[dict] = []
        self._existing_after_conflict = existing_after_conflict

    async def get_or_none(self, **kwargs: Any) -> Optional[object]:
        self.lookups.append(kwargs)
        return None if len(self.lookups) == 1 else self._existing_after_conflict

    async def create(self, **_kwargs: Any) -> object:
        raise IntegrityError("duplicate key value violates unique constraint")


def _install(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, existing: Optional[object]) -> _FakeAssetModel:
    model = _FakeAssetModel(existing)
    monkeypatch.setattr(library_service, "DBKBAsset", model)
    monkeypatch.setattr(library_service, "ensure_kb_library_dirs", lambda: None)
    monkeypatch.setattr(library_service, "resolve_kb_library_source_path", lambda rel_path: tmp_path / rel_path)
    return model


async def _upload(name: str) -> tuple[object, bool]:
    return await library_service.create_asset_from_upload(
        upload_file=UploadFile(file=io.BytesIO(b"same content"), filename=name),
        source_path="",
        title="",
        category="",
        tags=[],
        summary="",
        is_enabled=True,
    )


async def test_conflict_that_reuses_the_existing_asset_removes_its_own_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    existing = object()
    model = _install(monkeypatch, tmp_path, existing)

    asset, reused = await _upload("readme.md")

    assert (asset, reused) == (existing, True)
    assert len(model.lookups) == 2
    assert not (tmp_path / "readme.md").exists()


async def test_conflict_that_reraises_also_removes_its_own_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _install(monkeypatch, tmp_path, None)

    with pytest.raises(IntegrityError):
        await _upload("readme.md")

    assert not (tmp_path / "readme.md").exists()
