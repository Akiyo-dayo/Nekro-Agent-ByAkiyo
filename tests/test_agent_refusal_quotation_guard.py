"""域名档的**防引用**墙：拒绝页被引用时不能判成上游拒答

## 这一条修的是什么

`ProviderRefusalSignature`（域名档）原先只有「域名 + 政策文件名 + 违规动词」三个合取，
**没有** `PROVIDER_BOILERPLATE` 那一档的防引用墙。而这三样在**引用**里出现得和在原页里
一样齐全，于是：

    我查了下，错误是 The prompt could not be submitted，详见
    https://policies.google.com/terms/generative-ai/use-policy 这个 use policy 里写的 violate 条款。

会被判成上游拒答 → 模型的正常回复被丢弃、回合零重试掐断、用户收到「被安全策略挡下了」，
**而实际上什么都没被挡**。触发条件一点也不假想：拒答原文本来就会被贴进群里
（用户 2026-08-18 就是贴着截图来报障的），模型下一轮引用它即可。

## 判据为什么是「残余量」而不是「有没有中日韩字」

`_match_boilerplate` 用的是语种错位（纯 ASCII 标志 + 正文含中日韩字 = 有人在复述）。
那条放到域名档会把**中文渠道前缀 + 英文拒绝页**这一真实形态误放行
（`上游返回错误：<英文原页>`）。残余量是同一件事的定量版本：中日韩字同样计入残余，
所以短前缀过得去、整句引用过不去。`TestChannelPrefixStillDetected` 钉的就是这个差别。
"""

from typing import Tuple

import pytest

from nekro_agent.services.agent.provider_refusal import (
    MAX_RESIDUAL_CHARS,
    PROVIDER_REFUSAL_SIGNATURES,
    ProviderRefusalSignature,
    _match_signature,
    _signature_is_quoted,
    _strip_known_boilerplate,
    detect_provider_refusal,
    is_provider_refusal_text,
    normalize_for_match,
)

# 用户 2026-08-18 提供的截图逐字原文。所有正例都由它派生，派生方式在各自用例里写明。
VERBATIM_REFUSAL = (
    "The prompt could not be submitted. The prompt contains sensitive words that violate "
    "Google's [Generative AI Prohibited Use policy]"
    "(https://policies.google.com/terms/generative-ai/use-policy). "
    "Try rephrasing the prompt. If you think this was an error, "
    "[send feedback](https://ai.google.dev/gemini-api/docs/troubleshooting)."
)

GOOGLE_SIGNATURE = PROVIDER_REFUSAL_SIGNATURES[0]


# (用例名, 正文) —— 这些是**上游在说话**，必须继续判定为拒答
REAL_PAGE_SAMPLES: Tuple[Tuple[str, str], ...] = (
    ("逐字原文", VERBATIM_REFUSAL),
    ("渠道加英文模型名前缀", f"[gemini-3.7-flash-high] {VERBATIM_REFUSAL}"),
    ("渠道加中文短前缀", f"上游返回错误：{VERBATIM_REFUSAL}"),
    ("渠道加中文长前缀", f"上游渠道返回了一个错误，内容如下：{VERBATIM_REFUSAL}"),
    ("渠道折行", VERBATIM_REFUSAL.replace(". ", ".\n").replace(", ", ",\n")),
    ("全大写", VERBATIM_REFUSAL.upper()),
    (
        "剥掉 markdown 的纯文本形态",
        VERBATIM_REFUSAL.replace("[", "").replace("]", "").replace("(", "").replace(")", ""),
    ),
    (
        "截断到前两句 + 裸链接",
        "The prompt could not be submitted. The prompt contains sensitive words that violate "
        "Google's Generative AI Prohibited Use policy. "
        "https://policies.google.com/terms/generative-ai/use-policy",
    ),
)

# (用例名, 正文) —— 这些是**有人在引用这一页**，必须放行
QUOTATION_SAMPLES: Tuple[Tuple[str, str], ...] = (
    (
        "中文引用 + 政策链接",
        "我查了下，错误是 The prompt could not be submitted，详见 "
        "https://policies.google.com/terms/generative-ai/use-policy 这个 use policy 里写的 violate 条款。",
    ),
    (
        "英文引用 + 政策链接",
        "I checked it: the error was The prompt could not be submitted, see "
        "https://policies.google.com/terms/generative-ai/use-policy for the use policy that says violate.",
    ),
    (
        "短中文引用",
        "报错：The prompt could not be submitted，详见 "
        "https://policies.google.com/terms/generative-ai/use-policy 的 use policy violate 条款，我再试一次",
    ),
    (
        "模型向用户转述排障链接",
        "前辈贴的那个报错 https://ai.google.dev/gemini-api/docs/troubleshooting 是说 prompt 里有 "
        "sensitive words violate 了 Generative AI Prohibited Use policy 啦",
    ),
)


class TestRealPageStillDetected:
    """真拒绝页的各种渠道包装形态**不能**被防引用墙误伤"""

    @pytest.mark.parametrize(("name", "text"), REAL_PAGE_SAMPLES, ids=[n for n, _ in REAL_PAGE_SAMPLES])
    def test_real_page_is_detected(self, name: str, text: str) -> None:
        assert is_provider_refusal_text(text) is True, f"{name} 是上游拒绝页，漏判会让死循环复活"

    @pytest.mark.parametrize(("name", "text"), REAL_PAGE_SAMPLES, ids=[n for n, _ in REAL_PAGE_SAMPLES])
    def test_real_page_is_not_judged_as_quotation(self, name: str, text: str) -> None:
        """直判防引用墙本身，避免上一条被别的闸门顺带满足而变成空断言"""
        lowered = normalize_for_match(text.strip())
        assert (
            _signature_is_quoted(lowered, text.strip(), GOOGLE_SIGNATURE, ("prohibited use policy", "violate"))
            is False
        ), f"{name} 被防引用墙判成了引用"


class TestQuotationIsReleased:
    """引用这一页的正文必须放行 —— 这是本条修复的目的"""

    @pytest.mark.parametrize(("name", "text"), QUOTATION_SAMPLES, ids=[n for n, _ in QUOTATION_SAMPLES])
    def test_quotation_is_released(self, name: str, text: str) -> None:
        assert detect_provider_refusal(text) is None, f"{name} 是引用，误判会把模型的正常回复整轮掐掉"

    @pytest.mark.parametrize(("name", "text"), QUOTATION_SAMPLES, ids=[n for n, _ in QUOTATION_SAMPLES])
    def test_sample_really_exercises_the_guard(self, name: str, text: str) -> None:
        """样本有效性自检：这些正文**确实**满足域名档的全部命中条件

        没有这一条，上面那组「放行」断言可能只是因为样本压根就没命中域名档 ——
        那样即使防引用墙被删掉它们也照样绿，是典型的空断言。
        """
        lowered = normalize_for_match(text.strip())
        assert any(host in lowered for host in GOOGLE_SIGNATURE.hosts), f"{name} 没有域名，验不到域名档"
        for index, group in enumerate(GOOGLE_SIGNATURE.phrase_groups):
            assert any(phrase in lowered for phrase in group), f"{name} 第 {index} 组短语未命中，验不到域名档"

    @pytest.mark.parametrize(("name", "text"), QUOTATION_SAMPLES, ids=[n for n, _ in QUOTATION_SAMPLES])
    def test_guard_is_what_releases_them(self, name: str, text: str) -> None:
        """把防引用墙摘掉，这些样本就会被判成拒答 —— 证明拦住它们的正是这道墙"""
        naked = ProviderRefusalSignature(
            provider=GOOGLE_SIGNATURE.provider,
            hosts=GOOGLE_SIGNATURE.hosts,
            phrase_groups=GOOGLE_SIGNATURE.phrase_groups,
            fragments=(),  # 无 fragments 时退回语种错位那条弱墙
        )
        lowered = normalize_for_match(text.strip())
        stripped = text.strip()
        assert _match_signature(lowered, stripped, GOOGLE_SIGNATURE) is None
        # 英文引用不含中日韩字，弱墙拦不住它 —— 正是「弱墙不够、必须有 fragments」的证据
        if not any("一" <= ch <= "鿿" for ch in stripped):
            assert _match_signature(lowered, stripped, naked) is not None, f"{name} 应能在无 fragments 时命中"


class TestChannelPrefixStillDetected:
    """中文渠道前缀 + 英文原页：语种错位那条判据会在这里犯错，残余量不会"""

    def test_chinese_prefixed_english_page_is_detected(self) -> None:
        text = f"上游返回错误：{VERBATIM_REFUSAL}"
        assert any("一" <= ch <= "鿿" for ch in text), "样本必须真的含中日韩字，否则验不到这条差别"
        assert is_provider_refusal_text(text) is True

    def test_residual_of_real_page_stays_within_budget(self) -> None:
        """域名不能在剔链接之前单独剔掉 —— 顺序错了残余会从个位数涨到 70

        `_strip_known_boilerplate` 靠整条 URL 的正则剔链接。先把域名替换成空格会把
        `https://policies.google.com/terms/...` 打断成 `https:// /terms/...`，
        链接不再匹配、整条路径逐字留在残余里，真拒绝页于是被判成引用。
        """
        lowered = normalize_for_match(VERBATIM_REFUSAL)
        residual = lowered
        for fragment in GOOGLE_SIGNATURE.fragments:
            residual = residual.replace(fragment, " ")
        assert len(_strip_known_boilerplate(residual)) <= MAX_RESIDUAL_CHARS

        broken = lowered
        for fragment in GOOGLE_SIGNATURE.fragments:
            broken = broken.replace(fragment, " ")
        for host in GOOGLE_SIGNATURE.hosts:  # 错误顺序：先剔域名
            broken = broken.replace(host, " ")
        assert len(_strip_known_boilerplate(broken)) > MAX_RESIDUAL_CHARS, (
            "错误顺序必须真的算出超标的残余，否则上一条断言证明不了顺序的重要性"
        )


class TestSignatureTableContract:
    """域名档的每一条签名都必须带 fragments，否则它就是在裸奔"""

    def test_every_signature_carries_fragments(self) -> None:
        for signature in PROVIDER_REFUSAL_SIGNATURES:
            assert signature.fragments, (
                f"{signature.provider} 没有 fragments，域名档会退回只靠语种错位的弱墙 —— "
                f"英文引用将不再被拦住"
            )

    def test_verbatim_markers_do_not_leak_generic_policy_words(self) -> None:
        """认版用的整句里**不许**出现政策文件名一类的通用词

        这条是踩出来的：认版一开始用的是 `fragments`，而 `fragments` 含
        `generative ai prohibited use policy`——政策名在**变体页**
        （`Your prompt was blocked because it violates …`）里照样出现，于是变体页被当成
        「认得出的那一版」去算残余量，残余必然超标、真拒绝页被判成引用，
        `TestRecallOnRefusalVariants` 的 V2/V3 当场变红。
        认版必须只用这一版**独有**的整句。
        """
        generic = ("generative ai prohibited use policy", "use policy", "policy", "google's", "send feedback")
        for signature in PROVIDER_REFUSAL_SIGNATURES:
            for marker in signature.verbatim_markers:
                assert marker not in generic, f"{signature.provider} 的认版整句 {marker!r} 是通用词，会误伤变体页"
            assert set(signature.verbatim_markers) <= set(signature.fragments), (
                f"{signature.provider} 的 verbatim_markers 必须是 fragments 的子集，"
                f"否则认得出的那一版算残余时反而剔不干净"
            )

    def test_variant_page_is_not_recognized_as_this_version(self) -> None:
        """变体页认不出版 → 走弱墙 → 仍被判为拒答（召回不能因为这道墙而丢）"""
        variant = (
            "Your prompt was blocked because it violates the Google Generative AI Prohibited Use Policy. "
            "See https://policies.google.com/terms/generative-ai/use-policy for details."
        )
        lowered = normalize_for_match(variant)
        assert not any(marker in lowered for marker in GOOGLE_SIGNATURE.verbatim_markers), "变体页不该被认成这一版"
        assert is_provider_refusal_text(variant) is True, "变体页仍是上游拒绝页，不能因为防引用墙而漏判"

    def test_fragments_cover_the_verbatim_page(self) -> None:
        """fragments 必须真的覆盖逐字原页，否则残余量算出来永远超标、真拒绝页全被放行"""
        lowered = normalize_for_match(VERBATIM_REFUSAL)
        residual = lowered
        for fragment in GOOGLE_SIGNATURE.fragments:
            residual = residual.replace(fragment, " ")
        leftover = _strip_known_boilerplate(residual)
        assert leftover == "", f"逐字原页剔完 fragments 后还剩 {leftover!r}，说明 fragments 漏了句式"
