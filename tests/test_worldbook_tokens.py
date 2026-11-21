"""世界书 token 计数器：本地 `o200k_base`、模型家族系数、字符估算兜底、分段计数与缓存

要钉住的事：

1. 编码从随代码发布的文件构造，不联网；文件被改过（摘要对不上）就不用，退回字符估算并只打一次 WARNING；
2. 我们抄的切分正则与特殊 token 与 tiktoken 自己的 `o200k_base` 定义逐项一致；
3. 系数：模型组上填的优先，否则按模型名取第一个命中的家族（`gpt-4o` 先于 `gpt-4`、`nemo` 先于 `mistral`、
   `o1`/`o3`/`o4` 按独立的词）；都不中按 1.0；
4. 分段计数与整段编码结果一致（中文、英文），没有标点的长串不再是平方级；
5. 缓存按内容摘要记原始数（不含系数），编码晚一步可用时不沿用字符估算的数；
6. 一批正文合计很长时，摘要与计数都在线程里做，不压在事件循环上。
"""

from __future__ import annotations

import gzip
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterator, List

import pytest
from loguru import logger

from nekro_agent.services.worldbook import tokens


@pytest.fixture(autouse=True)
def _fresh_counter() -> Iterator[None]:
    """每条用例前后让计数器回到「还没加载过」：它是模块级单例"""
    tokens.reset_for_tests()
    yield
    tokens.reset_for_tests()


@pytest.fixture
def warnings_logged() -> Iterator[List[str]]:
    messages: List[str] = []
    sink_id = logger.add(lambda message: messages.append(message.record["message"]), level="WARNING")
    yield messages
    logger.remove(sink_id)


class TestEncoding:
    def test_the_bundled_file_loads_without_network(self) -> None:
        assert tokens.ensure_loaded() is True
        assert tokens.estimator() == "tiktoken"
        assert tokens.base_count("你好，世界") > 0

    def test_our_copy_of_the_encoding_definition_matches_tiktoken(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """切分正则与特殊 token 照抄 tiktoken；库升级改了定义，这里当场红"""
        from tiktoken_ext import openai_public

        monkeypatch.setattr(openai_public, "load_tiktoken_bpe", lambda *_a, **_k: {})
        reference = openai_public.o200k_base()

        assert reference["pat_str"] == tokens.O200K_PAT_STR
        assert reference["special_tokens"] == tokens.O200K_SPECIAL_TOKENS
        assert reference["name"] == tokens.ENCODING_NAME

    def test_a_tampered_file_falls_back_to_char_estimates_with_one_warning(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        warnings_logged: List[str],
    ) -> None:
        bad = tmp_path / "o200k_base.tiktoken.gz"
        bad.write_bytes(gzip.compress(b"AAAA 0\n"))
        monkeypatch.setattr(tokens, "ENCODING_FILE", bad)

        assert tokens.ensure_loaded() is False
        assert tokens.estimator() == "chars"
        assert tokens.base_count("设定" * 10) == tokens.estimate_by_chars("设定" * 10)
        tokens.base_count("再数一次")
        assert len([m for m in warnings_logged if "改用字符估算" in m]) == 1

    def test_special_token_text_is_counted_as_plain_text(self) -> None:
        """正文里写着 `<|endoftext|>` 不能让计数抛错（那是用户写的文字，不是控制符）"""
        assert tokens.base_count("前面<|endoftext|>后面") > 1


class TestFactors:
    @pytest.mark.parametrize(
        ("model_name", "factor", "family"),
        [
            ("deepseek-chat", 0.80, "deepseek"),
            ("gemini-2.5-flash", 0.89, "gemma"),
            ("gemma-3-27b", 0.89, "gemma"),
            ("Qwen2.5-72B-Instruct", 0.90, "qwen"),
            ("yi-large", 0.92, "yi"),
            ("command-r-plus", 0.91, "command-r"),
            ("gpt-4o-mini", 1.00, "o200k"),
            ("gpt-4.1", 1.00, "o200k"),
            ("gpt-5", 1.00, "o200k"),
            ("o3-mini", 1.00, "o200k"),
            ("openai/o1", 1.00, "o200k"),
            ("llama-3.1-70b", 1.02, "llama"),
            ("mistral-nemo", 1.16, "nemo"),
            ("claude-3-5-sonnet", 1.29, "claude"),
            ("mixtral-8x7b", 1.35, "mistral"),
            ("gpt-4-turbo", 1.38, "cl100k"),
            ("gpt-3.5-turbo", 1.38, "cl100k"),
        ],
    )
    def test_model_name_families(self, model_name: str, factor: float, family: str) -> None:
        result = tokens.factor_for_model_name(model_name)
        assert (result.value, result.source, result.family) == (factor, "model_name", family)

    @pytest.mark.parametrize("model_name", ["", "some-relay-model", "proto1x", "kimi-k2-0711"])
    def test_unknown_names_are_one(self, model_name: str) -> None:
        """`o1` 嵌在别的词里（如 `proto1x`）不算 o 系列"""
        assert tokens.factor_for_model_name(model_name) == tokens.TokenFactor(value=1.0, source="default")

    def test_the_group_factor_wins(self) -> None:
        group = SimpleNamespace(TOKEN_COUNT_FACTOR=1.1, CHAT_MODEL="deepseek-chat")
        assert tokens.factor_for_group(group) == tokens.TokenFactor(value=1.1, source="group")

    def test_a_zero_or_bad_group_factor_falls_back_to_the_name(self) -> None:
        for bad in (0, -1, float("nan"), float("inf"), "x", None):
            group = SimpleNamespace(TOKEN_COUNT_FACTOR=bad, CHAT_MODEL="claude-3-opus")
            assert tokens.factor_for_group(group).value == 1.29

    def test_count_applies_the_factor(self) -> None:
        text = "帝国历三百年，北境的雪原上出现了第一座要塞。"
        raw = tokens.base_count(text)
        assert tokens.count_tokens(text, 1.0) == raw
        assert tokens.count_tokens(text, 0.8) == round(raw * 0.8)
        assert tokens.count_tokens("", 1.3) == 0
        assert tokens.count_tokens("字", 0.01) == 1, "非空正文至少 1 token"


class TestChunking:
    def test_chunked_counts_match_whole_encoding(self) -> None:
        tokens.ensure_loaded()
        encoding = tokens._holder.get()  # noqa: SLF001
        prose = "帝国历三百年，北境的雪原上出现了第一座要塞。守将名叫阿尔文，他的剑据说能劈开冰川。" * 400
        english = "The northern fortress was built in the third century of the empire. " * 300
        for text in (prose, english):
            assert len(text) > tokens.CHUNK_CHARS
            assert tokens._count_with_encoding(encoding, text) == len(encoding.encode_ordinary(text))  # noqa: SLF001

    def test_a_long_unpunctuated_run_is_not_quadratic(self) -> None:
        """一整段没有标点的文字是一个 BPE 片段：不分段时二十万字要几分钟"""
        tokens.ensure_loaded()
        started = time.monotonic()
        count = tokens.base_count("设定世界观" * 40_000)
        assert count > 0
        assert time.monotonic() - started < 10

    def test_estimate_by_chars(self) -> None:
        assert tokens.estimate_by_chars("") == 0
        assert tokens.estimate_by_chars("中" * 100) == 78
        assert tokens.estimate_by_chars("a" * 100) == 25
        assert tokens.estimate_by_chars("中" * 100 + "a" * 100) == 103


class TestCache:
    def test_counts_are_cached_by_content(self, monkeypatch: pytest.MonkeyPatch) -> None:
        tokens.ensure_loaded()
        text = "缓存测试的正文"
        first = tokens.base_count(text)
        calls: list[str] = []
        original = tokens._count_with_encoding  # noqa: SLF001

        def spy(encoding: Any, value: str) -> int:
            calls.append(value)
            return original(encoding, value)

        monkeypatch.setattr(tokens, "_count_with_encoding", spy)
        assert tokens.count_tokens(text, 0.8) == round(first * 0.8)
        assert tokens.count_tokens(text, 1.3) == round(first * 1.3)
        assert calls == [], "换系数不必重数"

    def test_estimates_are_not_reused_once_the_encoding_loads(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        text = "先估算后精确"
        bad = tmp_path / "broken.gz"
        bad.write_bytes(b"not gzip")
        real = tokens.ENCODING_FILE
        monkeypatch.setattr(tokens, "ENCODING_FILE", bad)
        estimated = tokens.base_count(text)
        assert estimated == tokens.estimate_by_chars(text)

        tokens._holder.reset()  # noqa: SLF001
        monkeypatch.setattr(tokens, "ENCODING_FILE", real)
        assert tokens.ensure_loaded() is True
        exact = tokens.base_count(text)
        encoding = tokens._holder.get()  # noqa: SLF001
        assert exact == len(encoding.encode_ordinary(text))

    def test_the_cache_drops_the_oldest_half_when_full(self, monkeypatch: pytest.MonkeyPatch) -> None:
        tokens.ensure_loaded()
        monkeypatch.setattr(tokens, "MAX_CACHE_ENTRIES", 4)
        for index in range(5):
            tokens.base_count(f"条目{index}")
        assert len(tokens._cache) <= 4  # noqa: SLF001

    async def test_prefetch_fills_the_cache(self, monkeypatch: pytest.MonkeyPatch) -> None:
        texts = ["预取一", "预取二" * 30_000]
        await tokens.prefetch(texts)
        calls: list[str] = []
        original = tokens._count_with_encoding  # noqa: SLF001

        def spy(encoding: Any, value: str) -> int:
            calls.append(value)
            return original(encoding, value)

        monkeypatch.setattr(tokens, "_count_with_encoding", spy)
        for text in texts:
            tokens.count_tokens(text)
        assert calls == [], "预取过的正文之后只查缓存"

    async def test_base_counts_keep_order_and_match_single_counts(self) -> None:
        texts = ["甲", "", "乙乙乙乙", "甲"]
        assert await tokens.base_counts(texts) == [tokens.base_count(text) for text in texts]
        assert await tokens.base_counts([]) == []

    @pytest.mark.parametrize(("size", "off_loop"), [(100, False), (tokens.PREFETCH_INLINE_CHARS + 1, True)])
    async def test_long_batches_hash_and_count_in_a_worker_thread(
        self,
        monkeypatch: pytest.MonkeyPatch,
        size: int,
        off_loop: bool,
    ) -> None:
        """合计很长时，连「查缓存要算的摘要」也在线程里：已经数过的长正文同样不在事件循环上摘要"""
        text = "长" * size
        tokens.base_count(text)  # 先数进缓存：这一轮全是命中，照样不该在事件循环上算摘要
        threads: list[int] = []
        original = tokens.base_count

        def spy(value: str) -> int:
            threads.append(threading.get_ident())
            return original(value)

        monkeypatch.setattr(tokens, "base_count", spy)
        await tokens.base_counts([text])

        assert len(threads) == 1
        assert (threads[0] != threading.get_ident()) is off_loop

    def test_apply_factor(self) -> None:
        assert tokens.apply_factor(0, 1.3) == 0
        assert tokens.apply_factor(1, 0.1) == 1, "非空正文至少 1"
        assert tokens.apply_factor(100, 1.29) == 129

    def test_approx_chars(self) -> None:
        assert tokens.approx_chars(780, 1.0) == 1000
        assert tokens.approx_chars(780, 0) == 1000, "系数 0 按 1 算"
        assert tokens.approx_chars(0, 1.0) == 0
