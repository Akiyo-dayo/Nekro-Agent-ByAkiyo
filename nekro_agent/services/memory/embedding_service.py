"""Embedding 服务

提供文本向量化功能，复用项目现有的 OpenAI 兼容 API。
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx

from nekro_agent.core.config import ModelConfigGroup, config
from nekro_agent.core.logger import get_sub_logger
from nekro_agent.services.agent.openai import gen_openai_embeddings
from nekro_agent.services.agent.upstream_redaction import (
    collect_llm_secret_values,
    redact_upstream_text,
)
from nekro_agent.tools.tenant_egress import is_tenant_owned_group

logger = get_sub_logger("memory.embedding")

DEFAULT_TIMEOUT = 30
MAX_RETRIES = 3
RETRY_DELAY = 1.0


def get_memory_embedding_dimension() -> int:
    """获取记忆系统当前配置的 embedding 维度"""
    return max(1, int(config.MEMORY_EMBEDDING_DIMENSION))


def get_memory_embedding_model_group() -> str:
    """获取记忆系统当前配置的 embedding 模型组"""
    return config.MEMORY_EMBEDDING_MODEL_GROUP


def get_kb_embedding_dimension() -> int:
    """获取知识库当前配置的 embedding 维度"""
    return max(1, int(config.KB_EMBEDDING_DIMENSION))


def get_kb_embedding_model_group() -> str:
    """获取知识库当前配置的 embedding 模型组"""
    return config.KB_EMBEDDING_MODEL_GROUP.strip()


def validate_kb_embedding_model_group() -> ModelConfigGroup:
    """校验知识库 embedding 模型组存在且类型正确"""
    group_name = get_kb_embedding_model_group()
    if not group_name:
        raise ValueError("知识库 Embedding 模型组未配置")

    try:
        model_group = config.get_model_group_info(group_name)
    except KeyError as e:
        raise ValueError(f"知识库 Embedding 模型组 '{group_name}' 不存在") from e

    if model_group.MODEL_TYPE != "embedding":
        raise ValueError(
            f"知识库 Embedding 模型组 '{group_name}' 类型不是 embedding，当前为 {model_group.MODEL_TYPE}",
        )

    return model_group


def ensure_kb_embedding_available() -> tuple[str, int]:
    """校验并返回知识库 embedding 当前配置"""
    validate_kb_embedding_model_group()
    return get_kb_embedding_model_group(), get_kb_embedding_dimension()


class EmbeddingService:
    """Embedding 服务

    封装向量化调用，提供重试机制和批量处理能力。
    """

    def __init__(
        self,
        model_group: str | None = None,
        dimension: int | None = None,
        timeout: int = DEFAULT_TIMEOUT,
    ):
        self.model_group = model_group
        self.dimension = dimension
        self.timeout = timeout

    @property
    def resolved_dimension(self) -> int:
        """解析当前应使用的 embedding 维度"""
        return self.dimension if self.dimension is not None else get_memory_embedding_dimension()

    @property
    def resolved_model_group(self) -> str:
        """解析当前应使用的 embedding 模型组"""
        return self.model_group if self.model_group is not None else get_memory_embedding_model_group()

    def _get_model_config(self) -> dict[str, Any]:
        """获取模型配置"""
        try:
            model_info = config.get_model_group_info(self.resolved_model_group)
            return {
                "model": model_info.CHAT_MODEL,
                "api_key": model_info.API_KEY,
                "base_url": model_info.BASE_URL,
            }
        except Exception as e:
            logger.error(f"获取 Embedding 模型配置失败: {e}")
            raise ValueError(f"Embedding 模型组 '{self.resolved_model_group}' 配置无效") from e

    async def embed_text(
        self,
        text: str,
        *,
        requester: tuple[str, str] | None = None,
    ) -> list[float]:
        """生成单条文本的向量

        Args:
            text: 输入文本
            requester: 发起方的 `(adapter_key, instance_key)`；传了就带到用量流水上，
                让这笔 `EMBEDDING` 花费能归到租户。不传与改造前完全相同（知识库等调用方不动）。

        Returns:
            向量列表

        Raises:
            ValueError: 模型配置无效或向量维度不匹配
            Exception: API 调用失败
        """
        if not text or not text.strip():
            raise ValueError("输入文本不能为空")

        from nekro_agent.services.usage import LLMUsageContext, LLMUsageScene, usage_scope

        model_config = self._get_model_config()
        last_error: Exception | None = None
        # 归属只到「哪个模型组」这一层：本服务是被记忆检索/知识库/沉淀共用的底层设施，
        # 它拿不到会话，也不该为了记账反过来去要一个 chat_key（那会把接口传染给所有调用方）。
        # 场景单列 `EMBEDDING`，因此这一档在用量页上是独立可见的：它是唯一一条按
        # 「每条 query 一次请求」放大的花销，涨起来时该动的是检索开关而不是对话配额。
        #
        # ⚠️ 每次尝试各记一行（记账在 `gen_openai_embeddings` 里面），
        # 所以 `MAX_RETRIES` 次重试在账上是 N 行 —— 那是对的，每一次都真的打了上游。
        usage_context = LLMUsageContext(
            scene=LLMUsageScene.EMBEDDING,
            adapter_key=requester[0] if requester is not None else "",
            instance_key=requester[1] if requester is not None else "",
        ).with_model_group(
            self.resolved_model_group,
            config.MODEL_GROUPS.get(self.resolved_model_group),
        )

        for attempt in range(MAX_RETRIES):
            try:
                with usage_scope(usage_context):
                    embedding = await gen_openai_embeddings(
                        model=model_config["model"],
                        input=text.strip(),
                        dimensions=self.resolved_dimension,
                        api_key=model_config["api_key"],
                        base_url=model_config["base_url"],
                        timeout=self.timeout,
                        tenant_egress=is_tenant_owned_group(self.resolved_model_group),
                    )

                # 验证维度
                if len(embedding) != self.resolved_dimension:
                    logger.warning(
                        f"向量维度不匹配: 期望 {self.resolved_dimension}, 实际 {len(embedding)}",
                    )

                logger.debug(f"文本向量化成功: {text[:30]}... -> {len(embedding)}维")
                return embedding

            except httpx.HTTPStatusError as e:
                last_error = e
                # 上游异常脱敏的同一条判据：`detail` 抄的是**上游响应体原文**，而供应商在 4xx 体里
                # 回显 `Authorization` 头 / `Incorrect API key provided: sk-…` 是常见形态。
                # Embedding 是第二个直连上游的通道（第一个是 chat），判据共用 `upstream_redaction`
                # 那一份，不在这里另写一套（另写就是 §2.10 的判据分叉）。
                secret_values = collect_llm_secret_values(model_config["api_key"])
                response_preview = (
                    redact_upstream_text(e.response.text[:300], secret_values) if e.response is not None else ""
                )
                logger.warning(
                    f"Embedding 请求失败 (尝试 {attempt + 1}/{MAX_RETRIES}): "
                    f"status={e.response.status_code if e.response else 'unknown'} "
                    f"model={model_config['model']} dimension={self.resolved_dimension} "
                    f"detail={response_preview}",
                )
                if e.response is not None and e.response.status_code < 500:
                    break
                if attempt < MAX_RETRIES - 1:
                    await asyncio.sleep(RETRY_DELAY * (attempt + 1))
            except Exception as e:
                last_error = e
                # 同上：这一档还兜住了代理串带口令的 `ProxyError` 一类
                logger.warning(
                    f"Embedding 请求失败 (尝试 {attempt + 1}/{MAX_RETRIES}): "
                    f"{redact_upstream_text(str(e), collect_llm_secret_values(model_config['api_key']))}",
                )
                if attempt < MAX_RETRIES - 1:
                    await asyncio.sleep(RETRY_DELAY * (attempt + 1))

        raise last_error or Exception("Embedding 请求失败")

    async def embed_batch(
        self,
        texts: list[str],
        batch_size: int = 10,
    ) -> list[list[float] | None]:
        """批量生成文本向量

        Args:
            texts: 文本列表
            batch_size: 并发批次大小

        Returns:
            向量列表（失败的位置为 None）
        """
        results: list[list[float] | None] = [None] * len(texts)

        for i in range(0, len(texts), batch_size):
            batch = texts[i : i + batch_size]
            tasks = [self.embed_text(t) for t in batch]

            try:
                batch_results = await asyncio.gather(*tasks, return_exceptions=True)
                for j, result in enumerate(batch_results):
                    if isinstance(result, Exception):
                        logger.error(f"批量 Embedding 第 {i + j} 条失败: {result}")
                        results[i + j] = None
                    else:
                        results[i + j] = result
            except Exception as e:
                logger.exception(f"批量 Embedding 失败: {e}")

        success_count = sum(1 for r in results if r is not None)
        logger.info(f"批量 Embedding 完成: {success_count}/{len(texts)} 成功")
        return results

    async def compute_similarity(
        self,
        text1: str,
        text2: str,
    ) -> float:
        """计算两个文本的语义相似度

        Args:
            text1: 文本 1
            text2: 文本 2

        Returns:
            余弦相似度 (0-1)
        """
        emb1, emb2 = await asyncio.gather(
            self.embed_text(text1),
            self.embed_text(text2),
        )
        return self._cosine_similarity(emb1, emb2)

    @staticmethod
    def _cosine_similarity(vec1: list[float], vec2: list[float]) -> float:
        """计算余弦相似度"""
        import math

        dot_product = sum(a * b for a, b in zip(vec1, vec2, strict=False))
        norm1 = math.sqrt(sum(a * a for a in vec1))
        norm2 = math.sqrt(sum(b * b for b in vec2))

        if norm1 == 0 or norm2 == 0:
            return 0.0
        return dot_product / (norm1 * norm2)


# 全局单例
embedding_service = EmbeddingService()
_kb_embedding_service_cache: tuple[str, int, EmbeddingService] | None = None


def get_kb_embedding_service() -> EmbeddingService:
    """获取知识库 embedding 服务单例，配置变更时自动刷新"""
    global _kb_embedding_service_cache

    model_group, dimension = ensure_kb_embedding_available()
    if _kb_embedding_service_cache is not None:
        cached_group, cached_dimension, cached_service = _kb_embedding_service_cache
        if cached_group == model_group and cached_dimension == dimension:
            return cached_service

    service = EmbeddingService(
        model_group=model_group,
        dimension=dimension,
    )
    _kb_embedding_service_cache = (model_group, dimension, service)
    return service


async def embed_text(text: str, *, requester: tuple[str, str] | None = None) -> list[float]:
    """便捷函数：生成文本向量"""
    return await embedding_service.embed_text(text, requester=requester)


async def embed_batch(texts: list[str]) -> list[list[float] | None]:
    """便捷函数：批量生成文本向量"""
    return await embedding_service.embed_batch(texts)


async def embed_kb_text(text: str) -> list[float]:
    """便捷函数：使用知识库独立配置生成文本向量"""
    return await get_kb_embedding_service().embed_text(text)


async def embed_kb_batch(texts: list[str]) -> list[list[float] | None]:
    """便捷函数：使用知识库独立配置批量生成文本向量"""
    return await get_kb_embedding_service().embed_batch(texts)
