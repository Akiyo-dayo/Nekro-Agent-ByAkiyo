"""PNG 里的酒馆人设卡数据（`tEXt` / `zTXt` 块）

酒馆把卡片 JSON 用 base64 塞进 PNG 的 `tEXt` 块，关键字是 `chara`（V2）或
`ccv3`（V3）。这里不依赖 PNG 库，逐块走：长度、CRC、块数与首块都自己校验。
理由有两条：

1. 上传的图片是外部输入，解析器不能假设块长度合理；先读 4 字节长度再切片，
   越界只抛一句中文拒绝，不会把整个文件读进内存的另一份拷贝。
2. 只需要宽高与两个文本块；引入完整 PNG 解码器会把解压与像素内存也带进来，
   而头像另有重新编码的流程。

## 为什么也读 `zTXt`

酒馆自己只写、也只读 `tEXt`（1.19.0 `src/character-card-parser.js:57`），所以 L5 起初不读
`zTXt`。但网上流传的卡里有一类被图片工具重存过：ImageMagick 一类工具会顺手加上
`gAMA` / `cHRM` / `bKGD` 等块，并把文本块改存成压缩的 `zTXt`。这样的卡并不少见，NA 原先只回一句
「这张 PNG 里没有酒馆人设卡数据」，管理员没法知道数据其实就在里面。现在关键字是 `ccv3` / `chara` 的 `zTXt` 也收。`iTXt` 仍然不读：没见过实例，
多一种格式就多一份要防的面。

## 解压炸弹怎么防

`zTXt` 是 deflate 压缩的，压缩比能到上千倍：一块 8 MiB 以内的数据解开可能是几个 GiB。
所以：

- **扫块时不解压**，只记下关键字与原始字节；等选定了要用的那一块（`ccv3` 优先，同关键字
  `tEXt` 优先）才解压这一块，另一块无论多大都不碰。
- **压缩方式字节必须是 0**（PNG 规范唯一定义的 deflate），否则整份拒绝。
- **流式解压、输出封顶**：`zlib.decompressobj` 每次调用都带 `max_length`，累计输出超过
  :data:`MAX_CARD_JSON_BYTES` 就立刻停手、整份拒绝，内存里最多只有上限加一个字节。
  这个上限与 `tEXt` 一侧的单块上限（`MAX_PNG_CHUNK_BYTES`，同为 8 MiB）一致：两种块解出来的
  base64 文本受同一个上限约束，`zTXt` 不会因为压缩而放进更大的卡。
- 数据流没有正常结束（被截断）、结束后还跟着多余字节、或 zlib 报错，同样整份拒绝。

所有这些失败都只回一句「无法解析这张卡」，不带原文、不回显 zlib 的异常文本。

宽高超过上限不在这里拒绝——卡片仍可导入，只是调用方不拿它当头像。
"""

from __future__ import annotations

import base64
import binascii
import struct
import zlib
from dataclasses import dataclass, field

from .errors import CardFileRejected
from .limits import MAX_CARD_JSON_BYTES, MAX_PNG_CHUNK_BYTES, MAX_PNG_CHUNKS

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_IHDR = b"IHDR"
_IEND = b"IEND"
_TEXT = b"tEXt"
_ZTEXT = b"zTXt"
_CARD_KEYWORDS = ("ccv3", "chara")
# PNG 规范里 `zTXt` 唯一定义的压缩方式：deflate（zlib 流）。
_ZTXT_METHOD_DEFLATE = 0
# 解析失败的统一说法，不带原文、不带异常文本。
_UNPARSEABLE = "无法解析这张卡"


@dataclass(frozen=True)
class PngCardInfo:
    """一张 PNG 的宽高与卡片文本块

    Attributes:
        width: IHDR 里的宽，按像素。
        height: IHDR 里的高，按像素。
        texts: 所有 `tEXt` 的关键字到原文映射；同关键字只留第一块。
        compressed: 关键字是 `ccv3` / `chara` 的 `zTXt` 块，关键字到「压缩方式字节 + 压缩数据」的
            映射，**还没解压**；同关键字只留第一块。要用时由 :func:`card_text_from_png` 解压。
    """

    width: int
    height: int
    texts: dict[str, str]
    compressed: dict[str, bytes] = field(default_factory=dict)


def _walk_chunks(data: bytes) -> list[tuple[bytes, bytes]]:
    """按 PNG 块结构走一遍，返回 `(类型, 数据)` 列表并校验边界与 CRC

    这是读与写共用的唯一一套校验：写回时如果另写一份宽松的走法，坏图会被原样
    带着新的文本块写出去，坏 CRC 反而更晚才暴露。
    """
    if not data.startswith(PNG_SIGNATURE):
        raise CardFileRejected("这不是 PNG 文件")

    chunks: list[tuple[bytes, bytes]] = []
    position = len(PNG_SIGNATURE)
    seen_iend = False
    while position < len(data):
        if len(chunks) >= MAX_PNG_CHUNKS:
            raise CardFileRejected(f"PNG 块数超过 {MAX_PNG_CHUNKS}")
        if position + 8 > len(data):
            raise CardFileRejected("PNG 块头不完整")
        length = int.from_bytes(data[position : position + 4], "big")
        chunk_type = data[position + 4 : position + 8]
        if length > MAX_PNG_CHUNK_BYTES:
            raise CardFileRejected(f"PNG 单块超过 {MAX_PNG_CHUNK_BYTES // (1024 * 1024)} MiB")
        end = position + 12 + length
        if end > len(data):
            raise CardFileRejected("PNG 块长度越界")
        chunk_data = data[position + 8 : position + 8 + length]
        stored_crc = int.from_bytes(data[position + 8 + length : end], "big")
        if zlib.crc32(chunk_type + chunk_data) & 0xFFFFFFFF != stored_crc:
            raise CardFileRejected("PNG 块校验失败")
        chunks.append((chunk_type, chunk_data))
        position = end
        if chunk_type == _IEND:
            seen_iend = True
            break

    if not chunks:
        raise CardFileRejected("PNG 没有任何数据块")
    if chunks[0][0] != _IHDR:
        raise CardFileRejected("PNG 的第一块不是 IHDR")
    if not seen_iend:
        raise CardFileRejected("PNG 缺少 IEND 结尾块")
    return chunks


def _read_size(ihdr: bytes) -> tuple[int, int]:
    """从 IHDR 数据里取宽高"""
    if len(ihdr) < 8:
        raise CardFileRejected("PNG 的 IHDR 块不完整")
    width = int.from_bytes(ihdr[0:4], "big")
    height = int.from_bytes(ihdr[4:8], "big")
    return width, height


def _text_keyword(chunk_data: bytes) -> tuple[str, str] | None:
    """拆出 `tEXt` 块的关键字与文本；没有分隔符就忽略这一块"""
    separator = chunk_data.find(b"\0")
    if separator < 0:
        return None
    try:
        keyword = chunk_data[:separator].decode("latin-1")
        value = chunk_data[separator + 1 :].decode("latin-1")
    except UnicodeDecodeError:  # pragma: no cover - latin-1 不会失败，留作防御
        return None
    return keyword, value


def read_png(data: bytes) -> PngCardInfo:
    """读 PNG 的宽高与卡片文本块

    宽或高超过 :data:`MAX_IMAGE_SIDE` 不在这里拒绝，由调用方决定是否还拿它当
    头像；卡片数据本身与图片尺寸无关。
    """
    chunks = _walk_chunks(data)
    width, height = _read_size(chunks[0][1])
    texts: dict[str, str] = {}
    compressed: dict[str, bytes] = {}
    for chunk_type, chunk_data in chunks:
        if chunk_type == _TEXT:
            parsed = _text_keyword(chunk_data)
            if parsed is None:
                continue
            keyword, value = parsed
            if keyword not in texts:
                texts[keyword] = value
        elif chunk_type == _ZTEXT:
            # 只记下卡片关键字的那几块，而且不在这里解压：选定之前，哪一块都可能是炸弹。
            separator = chunk_data.find(b"\0")
            if separator < 0:
                continue
            keyword = chunk_data[:separator].decode("latin-1")
            if keyword in _CARD_KEYWORDS and keyword not in compressed:
                compressed[keyword] = chunk_data[separator + 1 :]
    return PngCardInfo(width=width, height=height, texts=texts, compressed=compressed)


def _inflate_card_text(payload: bytes) -> str:
    """解压一块 `zTXt` 的正文：压缩方式字节 + zlib 流，输出封顶

    每次 `decompress` 都带 `max_length`，累计输出最多到上限加一个字节；超过就拒绝，不会把
    炸弹解开。`max_length` 恒为正数：取 0 在 zlib 里表示「不限」，循环里先判超限、再算余量，
    余量最小是 1。
    """
    if not payload or payload[0] != _ZTXT_METHOD_DEFLATE:
        raise CardFileRejected(_UNPARSEABLE)
    decompressor = zlib.decompressobj()
    output = bytearray()
    pending = payload[1:]
    try:
        while pending and not decompressor.eof:
            room = MAX_CARD_JSON_BYTES + 1 - len(output)
            output += decompressor.decompress(pending, room)
            if len(output) > MAX_CARD_JSON_BYTES:
                raise CardFileRejected(_UNPARSEABLE)
            pending = decompressor.unconsumed_tail
    except zlib.error:
        raise CardFileRejected(_UNPARSEABLE) from None
    # 输入用完了流却没结束（被截断），或结束后还跟着多余字节：都不是一块完好的 `zTXt`。
    if not decompressor.eof or decompressor.unused_data or pending:
        raise CardFileRejected(_UNPARSEABLE)
    return output.decode("latin-1")


def _card_value(info: PngCardInfo) -> str | None:
    """按优先级取卡片的 base64 文本：`ccv3` 先于 `chara`，同关键字 `tEXt` 先于 `zTXt`

    关键字的优先级跨两种块：`zTXt` 的 `ccv3` 也排在 `tEXt` 的 `chara` 前面。只解压选中的那一块。
    """
    for keyword in _CARD_KEYWORDS:
        value = info.texts.get(keyword)
        if value is not None:
            return value
        payload = info.compressed.get(keyword)
        if payload is not None:
            return _inflate_card_text(payload)
    return None


def card_text_from_png(info: PngCardInfo) -> str:
    """取出 PNG 里的卡片 JSON 文本：先 `ccv3`，再 `chara`

    两个都有时用 `ccv3`；`ccv3` 解不出来时**不**回退到 `chara`——回退会把一张
    损坏的新版卡静默读成旧版，报告里却看不出来。`zTXt` 也一样：选中的那一块解压失败就整份
    拒绝，不换下一块。
    """
    value = _card_value(info)
    if value is None:
        raise CardFileRejected("这张 PNG 里没有酒馆人设卡数据")
    try:
        raw = base64.b64decode(value, validate=True)
        return raw.decode("utf-8")
    except (binascii.Error, ValueError, UnicodeDecodeError) as exc:
        raise CardFileRejected("PNG 里的人设卡数据不是有效的 base64 或 UTF-8") from exc


def _pack_chunk(chunk_type: bytes, chunk_data: bytes) -> bytes:
    """按 PNG 的块结构打包并重算 CRC"""
    body = chunk_type + chunk_data
    return struct.pack(">I", len(chunk_data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)


def _card_text_chunk(keyword: str, text: str) -> bytes:
    """把一段 JSON 文本做成 base64 的 `tEXt` 块"""
    encoded = base64.b64encode(text.encode("utf-8"))
    return _pack_chunk(_TEXT, keyword.encode("latin-1") + b"\0" + encoded)


def _is_card_text_chunk(chunk_data: bytes) -> bool:
    """这个 `tEXt` / `zTXt` 块是不是旧的 `chara` / `ccv3`（两种块的关键字都在第一个 NUL 之前）"""
    parsed = _text_keyword(chunk_data)
    return parsed is not None and parsed[0] in _CARD_KEYWORDS


def write_png_card(png: bytes, chara_json: str, ccv3_json: str) -> bytes:
    """把两个卡片 JSON 写进 PNG，返回新图

    原图里的 `chara` / `ccv3` 文本块（`tEXt` 与 `zTXt` 都算）全部去掉，在 `IEND` 前按先
    `chara` 后 `ccv3` 的顺序插入两块 `tEXt`；其余块原样保留，CRC 全部重算。值用 UTF-8 JSON 的
    base64，因此块内容是纯 ASCII。旧的 `zTXt` 卡片块也要去掉：留着它，图里就同时有新旧两份
    卡片数据，换一个只认 `zTXt` 的读取方就会读到旧的。
    """
    chunks = _walk_chunks(png)
    output = bytearray(PNG_SIGNATURE)
    inserted = False
    for chunk_type, chunk_data in chunks:
        if chunk_type == _IEND:
            output += _card_text_chunk("chara", chara_json)
            output += _card_text_chunk("ccv3", ccv3_json)
            inserted = True
        elif chunk_type in (_TEXT, _ZTEXT) and _is_card_text_chunk(chunk_data):
            continue
        output += _pack_chunk(chunk_type, chunk_data)
    if not inserted:  # pragma: no cover - `_walk_chunks` 已经保证有 IEND
        raise CardFileRejected("PNG 缺少 IEND 结尾块")
    return bytes(output)
