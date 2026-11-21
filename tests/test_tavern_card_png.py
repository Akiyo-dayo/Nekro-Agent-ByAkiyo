"""PNG 文本块的读取、写入与拒绝边界

卡片是上传文件，PNG 块长度与 CRC 都不能信。这里用 Pillow 现场生成一张小图，再
手工拼 `tEXt` / `zTXt` 块，逐条钉住读取优先级、CRC 与边界检查，以及写回后旧
卡片块被替换、其它块保留、图片仍能被 Pillow 打开。

`zTXt` 的用例全部现场压缩，包括一个解压炸弹：钉住只解压选中的那一块、
压缩方式字节必须是 0、每次解压都带输出上限、截断与尾部多余字节都拒绝、错误不回显原文。
"""

from __future__ import annotations

import base64
import io
import json
import struct
import time
import zlib
from typing import Any, Iterable

import pytest
from PIL import Image

from nekro_agent.services.tavern import card_png
from nekro_agent.services.tavern.card_format import parse_card_file
from nekro_agent.services.tavern.card_png import (
    PNG_SIGNATURE,
    CardFileRejected,
    card_text_from_png,
    read_png,
    write_png_card,
)
from nekro_agent.services.tavern.limits import MAX_CARD_JSON_BYTES


def _pack(kind: bytes, data: bytes) -> bytes:
    """按 PNG 块结构打包并算 CRC"""
    body = kind + data
    return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)


def _text_chunk(keyword: str, text: str) -> bytes:
    return _pack(b"tEXt", keyword.encode("latin-1") + b"\0" + text.encode("latin-1"))


def _ztxt_chunk(keyword: str, text: str = "", *, method: int = 0, stream: bytes | None = None) -> bytes:
    """`zTXt` 块：关键字 + NUL + 压缩方式字节 + zlib 流；`stream` 给定时直接用它（造坏流用）"""
    body = zlib.compress(text.encode("latin-1")) if stream is None else stream
    return _pack(b"zTXt", keyword.encode("latin-1") + b"\0" + bytes([method]) + body)


def _b64(text: str) -> str:
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


def _base_png() -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (8, 8), (200, 30, 40)).save(buffer, format="PNG")
    return buffer.getvalue()


def _iter_chunks(png: bytes) -> list[tuple[bytes, bytes]]:
    """测试自己的松解析：只用来检查写回后的块内容，不参与产品代码校验"""
    chunks: list[tuple[bytes, bytes]] = []
    position = len(PNG_SIGNATURE)
    while position < len(png):
        length = int.from_bytes(png[position : position + 4], "big")
        kind = png[position + 4 : position + 8]
        data = png[position + 8 : position + 8 + length]
        chunks.append((kind, data))
        position += 12 + length
        if kind == b"IEND":
            break
    return chunks


def _insert_before_iend(png: bytes, extra: Iterable[bytes]) -> bytes:
    """把额外块插到 IEND 之前；输入的其它块原样保留"""
    extra = list(extra)
    output = bytearray(PNG_SIGNATURE)
    for kind, data in _iter_chunks(png):
        if kind == b"IEND":
            for chunk in extra:
                output += chunk
        output += _pack(kind, data)
    return bytes(output)


def _rebuild(chunks: Iterable[tuple[bytes, bytes]]) -> bytes:
    return PNG_SIGNATURE + b"".join(_pack(kind, data) for kind, data in chunks)


def _card_json(name: str) -> str:
    return json.dumps({"spec": "chara_card_v2", "spec_version": "2.0", "data": {"name": name, "description": "d"}})


class TestRead:
    def test_only_chara(self) -> None:
        info = read_png(_insert_before_iend(_base_png(), [_text_chunk("chara", "abc")]))

        assert info.width == 8
        assert info.height == 8
        assert info.texts == {"chara": "abc"}

    def test_only_ccv3(self) -> None:
        info = read_png(_insert_before_iend(_base_png(), [_text_chunk("ccv3", "v3")]))

        assert info.texts == {"ccv3": "v3"}

    def test_both_prefers_ccv3(self) -> None:
        png = _insert_before_iend(
            _base_png(),
            [
                _text_chunk("chara", base64.b64encode(b"old").decode("ascii")),
                _text_chunk("ccv3", base64.b64encode(b"new").decode("ascii")),
            ],
        )
        info = read_png(png)

        assert set(info.texts) == {"chara", "ccv3"}
        assert base64.b64decode(info.texts["ccv3"]).decode("utf-8") == "new"
        assert card_text_from_png(info) == "new"

    def test_ztxt_card_chunk_is_recorded_but_not_inflated_while_reading(self) -> None:
        # 扫块时只登记原始字节；连一个坏流也不会在这一步报错。
        png = _insert_before_iend(_base_png(), [_ztxt_chunk("chara", stream=b"not zlib")])
        info = read_png(png)

        assert info.texts == {}
        assert info.compressed == {"chara": b"\0not zlib"}

    def test_duplicate_keyword_keeps_first(self) -> None:
        png = _insert_before_iend(_base_png(), [_text_chunk("chara", "first"), _text_chunk("chara", "second")])

        assert read_png(png).texts == {"chara": "first"}

    def test_non_card_text_keyword_is_kept_too(self) -> None:
        png = _insert_before_iend(_base_png(), [_text_chunk("Comment", "keep me")])

        assert read_png(png).texts == {"Comment": "keep me"}


class _DecompressSpy:
    """包住真的 `decompressobj`，记下每次 `decompress` 的 `max_length` 与输出长度"""

    def __init__(self, calls: list[tuple[int, int]]) -> None:
        self._inner = _REAL_DECOMPRESSOBJ()
        self._calls = calls

    def decompress(self, data: bytes, max_length: int = 0) -> bytes:
        out = self._inner.decompress(data, max_length)
        self._calls.append((max_length, len(out)))
        return out

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


_REAL_DECOMPRESSOBJ = zlib.decompressobj


class TestZtxt:
    def test_ccv3_in_ztxt_is_read(self) -> None:
        card = _card_json("压缩的角色")
        png = _insert_before_iend(_base_png(), [_ztxt_chunk("ccv3", _b64(card))])

        assert card_text_from_png(read_png(png)) == card

    def test_chara_in_ztxt_is_read(self) -> None:
        card = _card_json("旧版压缩")
        png = _insert_before_iend(_base_png(), [_ztxt_chunk("chara", _b64(card))])

        assert card_text_from_png(read_png(png)) == card

    def test_ccv3_wins_across_chunk_types(self) -> None:
        png = _insert_before_iend(
            _base_png(),
            [_text_chunk("chara", _b64("old")), _ztxt_chunk("ccv3", _b64("new"))],
        )

        assert card_text_from_png(read_png(png)) == "new"

    def test_text_wins_over_ztxt_for_the_same_keyword_and_ztxt_is_not_touched(self) -> None:
        # 同关键字有 `tEXt` 就用它；那块坏 `zTXt` 根本不解压，所以不报错。
        png = _insert_before_iend(
            _base_png(),
            [_ztxt_chunk("ccv3", stream=b"broken"), _text_chunk("ccv3", _b64("plain"))],
        )

        assert card_text_from_png(read_png(png)) == "plain"

    def test_chara_ztxt_is_not_inflated_when_ccv3_is_present(self) -> None:
        bomb = zlib.compress(b"\0" * (MAX_CARD_JSON_BYTES * 4), 9)
        png = _insert_before_iend(
            _base_png(),
            [_text_chunk("ccv3", _b64("v3")), _ztxt_chunk("chara", stream=bomb)],
        )

        assert card_text_from_png(read_png(png)) == "v3"

    def test_non_card_keyword_is_ignored(self) -> None:
        png = _insert_before_iend(_base_png(), [_ztxt_chunk("Comment", "hello")])
        info = read_png(png)

        assert info.compressed == {}
        with pytest.raises(CardFileRejected, match="没有酒馆人设卡数据"):
            card_text_from_png(info)

    def test_duplicate_ztxt_keyword_keeps_first(self) -> None:
        png = _insert_before_iend(_base_png(), [_ztxt_chunk("chara", _b64("first")), _ztxt_chunk("chara", _b64("second"))])

        assert card_text_from_png(read_png(png)) == "first"

    def test_unknown_compression_method_is_rejected(self) -> None:
        png = _insert_before_iend(_base_png(), [_ztxt_chunk("ccv3", _b64("x"), method=1)])

        with pytest.raises(CardFileRejected, match="^无法解析这张卡$"):
            card_text_from_png(read_png(png))

    def test_corrupt_stream_is_rejected_without_echoing_zlib(self) -> None:
        png = _insert_before_iend(_base_png(), [_ztxt_chunk("ccv3", stream=b"SENTINEL-not-a-zlib-stream")])

        with pytest.raises(CardFileRejected) as caught:
            card_text_from_png(read_png(png))
        message = str(caught.value)
        assert message == "无法解析这张卡"
        assert "SENTINEL" not in message
        assert "Error" not in message
        assert caught.value.__cause__ is None

    def test_truncated_stream_is_rejected(self) -> None:
        stream = zlib.compress(_b64(_card_json("截断")).encode("ascii"))
        png = _insert_before_iend(_base_png(), [_ztxt_chunk("ccv3", stream=stream[:-6])])

        with pytest.raises(CardFileRejected, match="^无法解析这张卡$"):
            card_text_from_png(read_png(png))

    def test_trailing_bytes_after_the_stream_are_rejected(self) -> None:
        stream = zlib.compress(_b64(_card_json("尾巴")).encode("ascii"))
        png = _insert_before_iend(_base_png(), [_ztxt_chunk("ccv3", stream=stream + b"extra")])

        with pytest.raises(CardFileRejected, match="^无法解析这张卡$"):
            card_text_from_png(read_png(png))

    def test_empty_stream_is_rejected(self) -> None:
        png = _insert_before_iend(_base_png(), [_ztxt_chunk("ccv3", stream=b"")])

        with pytest.raises(CardFileRejected, match="^无法解析这张卡$"):
            card_text_from_png(read_png(png))

    def test_decompression_bomb_is_rejected_with_bounded_output(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # 64 MiB 的零压缩后只有几十 KB；按真实上限（8 MiB）解，必须在上限处停手。
        bomb = zlib.compress(b"\0" * (64 * 1024 * 1024), 9)
        assert len(bomb) < 128 * 1024
        png = _insert_before_iend(_base_png(), [_ztxt_chunk("ccv3", stream=bomb)])
        info = read_png(png)
        calls: list[tuple[int, int]] = []
        monkeypatch.setattr(card_png.zlib, "decompressobj", lambda: _DecompressSpy(calls))

        started = time.monotonic()
        with pytest.raises(CardFileRejected, match="^无法解析这张卡$"):
            card_text_from_png(info)

        assert time.monotonic() - started < 5
        assert calls, "没有走流式解压"
        assert all(0 < max_length <= MAX_CARD_JSON_BYTES + 1 for max_length, _ in calls)
        assert sum(produced for _, produced in calls) <= MAX_CARD_JSON_BYTES + 1

    def test_output_exactly_at_the_limit_is_accepted_and_one_more_byte_is_not(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        text = _b64(_card_json("刚好"))
        png = _insert_before_iend(_base_png(), [_ztxt_chunk("ccv3", text)])
        info = read_png(png)

        monkeypatch.setattr(card_png, "MAX_CARD_JSON_BYTES", len(text))
        assert base64.b64decode(text).decode("utf-8") == card_text_from_png(info)

        monkeypatch.setattr(card_png, "MAX_CARD_JSON_BYTES", len(text) - 1)
        with pytest.raises(CardFileRejected, match="^无法解析这张卡$"):
            card_text_from_png(info)

    def test_a_ztxt_card_goes_through_the_whole_parser(self) -> None:
        card = json.dumps(
            {"spec": "chara_card_v3", "spec_version": "3.0", "data": {"name": "压缩卡", "description": "描述"}},
        )
        png = _insert_before_iend(_base_png(), [_ztxt_chunk("ccv3", _b64(card)), _ztxt_chunk("chara", _b64(card))])

        parsed, info = parse_card_file(png)

        assert parsed.spec == "v3"
        assert parsed.data["name"] == "压缩卡"
        assert info is not None and info.width == 8


class TestRejections:
    def test_bad_signature(self) -> None:
        with pytest.raises(CardFileRejected, match="不是 PNG"):
            read_png(b"not a png at all")

    def test_bad_crc(self) -> None:
        png = bytearray(_base_png())
        # IHDR 的 CRC 在签名 8 字节 + 长度 4 + 类型 4 + 数据 13 之后。
        png[29] ^= 0xFF
        with pytest.raises(CardFileRejected, match="校验失败"):
            read_png(bytes(png))

    def test_length_out_of_bounds(self) -> None:
        png = bytearray(_base_png())
        png[8:12] = (1000).to_bytes(4, "big")
        with pytest.raises(CardFileRejected, match="长度越界"):
            read_png(bytes(png))

    def test_too_many_chunks(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(card_png, "MAX_PNG_CHUNKS", 2)
        with pytest.raises(CardFileRejected, match="块数超过"):
            read_png(_base_png())

    def test_single_chunk_too_large(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(card_png, "MAX_PNG_CHUNK_BYTES", 5)
        with pytest.raises(CardFileRejected, match="单块超过"):
            read_png(_base_png())

    def test_missing_iend(self) -> None:
        png = _base_png()

        with pytest.raises(CardFileRejected, match="IEND"):
            read_png(png[:-12])

    def test_first_chunk_not_ihdr(self) -> None:
        chunks = _iter_chunks(_base_png())
        reordered = [chunks[1], chunks[0], *chunks[2:]]

        with pytest.raises(CardFileRejected, match="IHDR"):
            read_png(_rebuild(reordered))

    def test_bad_base64_does_not_echo_content(self) -> None:
        sentinel = "SENTINEL-raw-must-not-leak"
        png = _insert_before_iend(_base_png(), [_text_chunk("chara", sentinel + "@@@")])
        info = read_png(png)

        with pytest.raises(CardFileRejected) as caught:
            card_text_from_png(info)
        assert sentinel not in str(caught.value)

    def test_bad_utf8_does_not_echo_content(self) -> None:
        value = base64.b64encode(b"\xff\xfe\xfa").decode("ascii")
        png = _insert_before_iend(_base_png(), [_text_chunk("chara", value)])
        info = read_png(png)

        with pytest.raises(CardFileRejected, match="base64 或 UTF-8") as caught:
            card_text_from_png(info)
        assert "\xff" not in str(caught.value)


class TestWrite:
    def test_round_trip_replaces_old_card_chunks_and_keeps_others(self) -> None:
        old = _card_json("旧角色")
        png = _insert_before_iend(
            _base_png(),
            [
                _text_chunk("chara", base64.b64encode(old.encode("utf-8")).decode("ascii")),
                _text_chunk("ccv3", base64.b64encode(old.encode("utf-8")).decode("ascii")),
                _text_chunk("Comment", "keep me"),
            ],
        )
        chara = _card_json("V2 角色")
        ccv3 = json.dumps(
            {
                "spec": "chara_card_v3",
                "spec_version": "3.0",
                "data": {"name": "V3 角色", "nickname": "小名"},
            },
        )

        written = write_png_card(png, chara, ccv3)

        info = read_png(written)
        assert base64.b64decode(info.texts["chara"]).decode("utf-8") == chara
        assert base64.b64decode(info.texts["ccv3"]).decode("utf-8") == ccv3
        assert card_text_from_png(info) == ccv3

        texts = [
            (kind, data)
            for kind, data in _iter_chunks(written)
            if kind == b"tEXt" and data.split(b"\0", 1)[0] in (b"chara", b"ccv3")
        ]
        assert len(texts) == 2
        assert texts[0][1].startswith(b"chara\0")
        assert texts[1][1].startswith(b"ccv3\0")

        other_texts = [data for kind, data in _iter_chunks(written) if kind == b"tEXt" and data.startswith(b"Comment\0")]
        assert other_texts == [b"Comment\0keep me"]

    def test_old_ztxt_card_chunks_are_dropped_and_other_ztxt_kept(self) -> None:
        png = _insert_before_iend(
            _base_png(),
            [
                _ztxt_chunk("ccv3", _b64(_card_json("旧压缩"))),
                _ztxt_chunk("chara", _b64(_card_json("旧压缩"))),
                _ztxt_chunk("Comment", "keep me"),
            ],
        )

        written = write_png_card(png, _card_json("新 V2"), _card_json("新 V3"))

        ztxt = [data for kind, data in _iter_chunks(written) if kind == b"zTXt"]
        assert [data.split(b"\0", 1)[0] for data in ztxt] == [b"Comment"]
        info = read_png(written)
        assert info.compressed == {}
        assert card_text_from_png(info) == _card_json("新 V3")

    def test_written_image_can_still_be_opened_by_pillow(self) -> None:
        written = write_png_card(_base_png(), _card_json("A"), _card_json("B"))

        image = Image.open(io.BytesIO(written))
        assert image.size == (8, 8)

    def test_write_rejects_a_bad_input_png(self) -> None:
        with pytest.raises(CardFileRejected):
            write_png_card(b"not a png", _card_json("A"), _card_json("B"))
