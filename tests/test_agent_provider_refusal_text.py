"""上游拒答的第二种形态：渠道把拒绝页当成**普通正文**塞进 `content`（G1）

## 这组用例守的是什么

上一棒守住的是「`content` 为空 + `finish_reason='prohibited_content'`」（测试部署 gemini-3.6
的形态，由 `openai.LLMContentBlockedError` 判掉）。用户另一台部署跑 gemini-3.7-flash-high，
渠道把同一件事渲染成一段**非空正文**返回，HTTP 200、`finish_reason` 正常，于是所有判空闸门
原样放行；旧的 prose 兜底把它整段当代码，沙盒回一条 `SyntaxError: unterminated string
literal`（触发字符是 `Google's` 的撇号），错误连同拒答原文一起回喂，下一轮再拒答，烧满迭代。

`GEMINI_REFUSAL_TEXT` 是**线上逐字原文**，不是我编的近似样本——
`test_fixture_is_the_real_thing` 会把这一点钉死（长度、撇号、以及它真的会炸出
`unterminated string literal`）。

## 断言口径：这组里最重要的不是正例，是反例

小樱是角色扮演 Bot，剧情里大量出现「抱歉 / 我不能 / 违反」。线上 `exec_code` 全表实测：
**25 条命中拒答关键词的正文全是剧情对白，一条上游拒答都没有**。也就是说单关键词判据在
真实数据上准确率是 0，误杀成本远高于漏杀成本（漏杀只是退回今天的现状）。

因此本文件的重心是：

* `test_in_character_refusal_is_never_flagged` —— 任务给的四条反例逐字进表，一条都不许命中；
* `test_negative_samples_are_adversarial` —— 用**单关键词判据**原样跑一遍这四条，断言它
  全部误杀。没有这一条，上面那组可能只是「样本本来就不难」，证明不了判据的保守性；
* `test_every_conjunct_is_load_bearing` —— 把正例的四个合取逐个破坏，每破坏一个就必须
  不再命中。这是防「后人为了兼容某家把判据放宽成单条件」的护栏；
* `test_phrase_groups_do_not_degenerate` —— 组间短语不许互为子串，否则合取会悄悄退化成
  单条件（例如把 `prohibited` 加进第二组，第一组的 `prohibited use policy` 就让它成了摆设）。
"""

from __future__ import annotations

import ast
from typing import List, Tuple

import pytest

from nekro_agent.services.agent.provider_refusal import (
    MAX_REFUSAL_TEXT_CHARS,
    PROVIDER_REFUSAL_SIGNATURES,
    ResponseRefusalKind,
    classify_response_refusal,
    detect_provider_refusal,
    has_code_features,
    is_provider_refusal_text,
    normalize_for_match,
)
from nekro_agent.services.agent.resolver import parse_chat_response

# 线上逐字原文（用户截图 + 本机复现一致）。**不要改动一个字符**：
# 撇号、markdown 链接、两处域名、句末的 `.` 都是判据与回归的锚点。
GEMINI_REFUSAL_TEXT = (
    "The prompt could not be submitted. The prompt contains sensitive words that violate "
    "Google's [Generative AI Prohibited Use policy]"
    "(https://policies.google.com/terms/generative-ai/use-policy). "
    "Try rephrasing the prompt. If you think this was an error, "
    "[send feedback](https://ai.google.dev/gemini-api/docs/troubleshooting)."
)

# 同一段拒绝页被渠道按宽度折行后的形态。折行是真实存在的（`run_agent` 那条平行实现也是
# 按这个理由做空白归一的），而它恰好把 `Prohibited Use policy` 从中间劈开——不做归一
# 的话判据在这种形态上直接失效。断行位置刻意避开 Python 关键字与 `#`，
# 免得样本自己触发代码特征、把「折行也能认出来」这件事证明成别的东西。
WRAPPED_REFUSAL_TEXT = (
    "The prompt could not be submitted. The prompt contains sensitive words that\n"
    "violate Google's [Generative AI Prohibited Use\n"
    "policy](https://policies.google.com/terms/generative-ai/use-policy). Try\n"
    "rephrasing the prompt. If you think this was an error, [send\n"
    "feedback](https://ai.google.dev/gemini-api/docs/troubleshooting)."
)


# --------------------------------------------------------------------------------------
# 零、样本本身的真实性
# --------------------------------------------------------------------------------------


class TestFixtureIntegrity:
    """夹具必须逐字等于线上原文，否则整组用例证明的是另一个东西"""

    def test_fixture_is_the_real_thing(self) -> None:
        # 线上原文 322 字符：拼接写法不能悄悄多/少一个空格
        assert len(GEMINI_REFUSAL_TEXT) == 322
        # 撇号就是那个把沙盒炸掉的字符
        assert "Google's" in GEMINI_REFUSAL_TEXT
        # 两处供应商域名都在
        assert "https://policies.google.com/terms/generative-ai/use-policy" in GEMINI_REFUSAL_TEXT
        assert "https://ai.google.dev/gemini-api/docs/troubleshooting" in GEMINI_REFUSAL_TEXT
        # 没有任何围栏 —— 正因为如此它才会掉进 prose 兜底
        assert "```" not in GEMINI_REFUSAL_TEXT

    def test_fixture_reproduces_the_reported_sandbox_error(self) -> None:
        """把这段文本当代码编译，必然是用户看到的那条 `unterminated string literal`"""
        with pytest.raises(SyntaxError) as excinfo:
            ast.parse(GEMINI_REFUSAL_TEXT)
        assert "unterminated string literal" in str(excinfo.value)

    def test_existing_ast_gate_still_blocks_execution(self) -> None:
        """上一棒的 `ast.parse` 闸门仍然拦住了「被执行」——G1 补的是它之后的那两步

        这条是**边界声明**：本模块不是在重复上一棒的工作。闸门管住了第 1 步（进沙盒），
        管不住第 2、3 步（拒答原文被当成模型的话回喂 → 再拒答 → 烧满迭代），
        因为它只回答「这不是代码」，回答不了「这压根不是模型的输出」。
        """
        parsed = parse_chat_response(GEMINI_REFUSAL_TEXT)
        assert parsed.code_content == ""
        assert parsed.code_missing is True


# --------------------------------------------------------------------------------------
# 一、正例：供应商拒绝页
# --------------------------------------------------------------------------------------


class TestProviderRefusalPositive:
    def test_gemini_refusal_page_is_detected(self) -> None:
        match = detect_provider_refusal(GEMINI_REFUSAL_TEXT)
        assert match is not None
        assert match.provider == "google_gemini"
        assert match.matched_host == "policies.google.com"
        # 两个短语组各中一条 —— 命中是合取的结果，不是某一条关键词说了算
        assert len(match.matched_phrases) == len(PROVIDER_REFUSAL_SIGNATURES[0].phrase_groups)
        assert match.matched_phrases == ("prohibited use policy", "violate")

    def test_predicate_and_classification_agree(self) -> None:
        assert is_provider_refusal_text(GEMINI_REFUSAL_TEXT) is True
        assert classify_response_refusal(GEMINI_REFUSAL_TEXT) is ResponseRefusalKind.PROVIDER_REFUSAL

    @pytest.mark.parametrize(
        ("case", "text"),
        [
            ("首尾空白", f"\n\n  {GEMINI_REFUSAL_TEXT}  \n"),
            ("大小写抖动", GEMINI_REFUSAL_TEXT.upper()),
            ("渠道按宽度折行", WRAPPED_REFUSAL_TEXT),
        ],
    )
    def test_trivial_formatting_variance_still_detected(self, case: str, text: str) -> None:
        """渠道加个换行、或整段大写，判据不能就此失效（这类抖动不改变语义）"""
        assert is_provider_refusal_text(text) is True, case

    def test_wrapped_sample_really_splits_a_phrase(self) -> None:
        """折行样本必须真的把某条短语劈开，否则它证明不了空白归一那条判据

        `Prohibited Use` 与 `policy` 之间的换行是这条用例的全部意义：不做空白归一时
        `"prohibited use policy" in text.lower()` 为 False，判据当场失效。
        """
        assert "Prohibited Use\npolicy" in WRAPPED_REFUSAL_TEXT
        assert "prohibited use policy" not in WRAPPED_REFUSAL_TEXT.lower()
        assert "prohibited use policy" in normalize_for_match(WRAPPED_REFUSAL_TEXT)


# --------------------------------------------------------------------------------------
# 二、反例：模型自己的输出（本组是重心）
# --------------------------------------------------------------------------------------

# (用例名, 正文, 放行的**主要**理由)
IN_CHARACTER_SAMPLES: List[Tuple[str, str, str]] = [
    (
        "角色内拒绝-无代码特征",
        "抱歉呀前辈 我自己其实没有在群里修改名片和头像的权限呢",
        "没有供应商域名，也没有英文政策文件名",
    ),
    (
        "角色内拒绝-包在发言调用里",
        "send_msg_text(_ck, '[@id:1@] 抱歉……我不该说你拱火的')",
        "函数调用 + at 标记，两条代码/协议特征",
    ),
    (
        "注释里出现违反",
        "# 违反规则的做法\npass",
        "`#` 注释行 + 整段可 ast.parse",
    ),
    (
        "表情插件调用里出现抱歉",
        "send_matching_sticker(intent='害羞 可爱 抱歉', reply_mode='image_only')",
        "函数调用 + 关键字参数赋值",
    ),
    (
        "英文角色内拒绝",
        "I'm sorry, I can't help with that one, senpai.",
        "没有供应商域名",
    ),
    (
        "英文里同时出现 policy 与 violate",
        "That would violate the group policy, so I won't do it.",
        "没有供应商域名，且没有完整政策文件名",
    ),
    (
        "剧情里贴了 Google 链接",
        "你可以去 https://policies.google.com 看看条款哦",
        "有域名，但没有政策文件名，也没有违规动词",
    ),
]


class TestInCharacterRefusalNeverBlocked:
    @pytest.mark.parametrize(("case", "text", "reason"), IN_CHARACTER_SAMPLES)
    def test_in_character_refusal_is_never_flagged(self, case: str, text: str, reason: str) -> None:
        assert detect_provider_refusal(text) is None, f"{case}: 误杀了剧情对白（应放行理由：{reason}）"
        assert is_provider_refusal_text(text) is False, case

    @pytest.mark.parametrize(("case", "text", "reason"), IN_CHARACTER_SAMPLES)
    def test_in_character_refusal_never_classifies_as_provider(self, case: str, text: str, reason: str) -> None:
        """三档分类里，剧情对白只能落到「放行」的那两档"""
        kind = classify_response_refusal(text)
        assert kind is not ResponseRefusalKind.PROVIDER_REFUSAL, case
        assert kind in (ResponseRefusalKind.IN_CHARACTER_REFUSAL, ResponseRefusalKind.NORMAL), case

    @pytest.mark.parametrize(
        ("case", "text"),
        [
            ("抄在注释旁边", f"{GEMINI_REFUSAL_TEXT}\n# 前辈 这是 我 抄 回来 的 原文"),
            ("抄在 import 旁边", f"{GEMINI_REFUSAL_TEXT}\nimport 的 事 我 待会 再 说"),
        ],
    )
    def test_model_quoting_the_refusal_page_is_not_an_upstream_refusal(self, case: str, text: str) -> None:
        """模型把拒绝页**抄进自己的输出**时，只要带上代码特征就必须放行

        这条形态一点都不假设：拒答原文正是被回喂进上下文的东西，模型下一轮引用它是可预期的。
        判据在这里倒向「这是模型写的」，因为代码特征只可能来自模型。

        它同时钉住一个实现细节：代码特征必须判在**原文**上。`#` 与行首关键字靠的是行首位置，
        一旦改成判在 `normalize_for_match` 折过的单行上，这两条锚点全废——上面那张
        `SINGLE_SIGNAL_SAMPLES` 表直接调用 `has_code_features`，发现不了这种改动。
        """
        assert detect_provider_refusal(text) is None, case

    def test_negative_samples_are_adversarial(self) -> None:
        """用**单关键词判据**跑一遍前四条：它必须全部误杀

        没有这一条，上面那组用例可能只是「样本本来就不像拒答」，证明不了判据的保守性。
        这四条正是线上 25 条误伤样本的形态代表。
        """
        naive_keywords = ("抱歉", "不能", "违反", "sorry", "policy")

        def naive_judge(text: str) -> bool:
            lowered = text.lower()
            return any(token in lowered for token in naive_keywords)

        for case, text, _ in IN_CHARACTER_SAMPLES[:4]:
            assert naive_judge(text) is True, f"{case}: 样本不再触发单关键词判据，失去了对抗性"
            assert is_provider_refusal_text(text) is False, case


# --------------------------------------------------------------------------------------
# 三、边界
# --------------------------------------------------------------------------------------

BOUNDARY_SAMPLES: List[Tuple[str, str]] = [
    ("空串", ""),
    ("纯空白", "   \n\t  \r\n "),
    ("只有链接", "https://policies.google.com/terms/generative-ai/use-policy"),
    ("只有链接-裸域名", "policies.google.com"),
    ("只有 policy 一个词", "policy"),
    ("只有政策文件名没有域名", "Generative AI Prohibited Use policy"),
    ("有域名有违规动词但没有政策文件名", "This request violates the rules, see https://ai.google.dev/docs"),
    ("有域名有政策文件名但没有违规动词", "See Google's use policy at https://policies.google.com/terms"),
]


class TestBoundaries:
    @pytest.mark.parametrize(("case", "text"), BOUNDARY_SAMPLES)
    def test_boundary_inputs_are_not_provider_refusal(self, case: str, text: str) -> None:
        assert detect_provider_refusal(text) is None, case

    @pytest.mark.parametrize(("case", "text"), [("空串", ""), ("纯空白", "   \n\t ")])
    def test_empty_input_is_normal_not_refusal(self, case: str, text: str) -> None:
        assert classify_response_refusal(text) is ResponseRefusalKind.NORMAL, case
        assert has_code_features(text) is False, case

    def test_length_cap_is_actually_a_ceiling(self) -> None:
        """上限的**取值**本身要被钉住

        下面那条边界用例是按 `MAX_REFUSAL_TEXT_CHARS` 现算样本的，所以把常量改成 100000
        它照样绿——变异实测确认过。真正需要断言的是这个数**还是个天花板**：
        比已知拒绝页（322 字符）大出足够余量，又远小于「一段完整的剧情回复」。
        """
        assert len(GEMINI_REFUSAL_TEXT) < MAX_REFUSAL_TEXT_CHARS <= 2000

    def test_length_cap_boundary(self) -> None:
        """长度上限必须是**一条真闸门**：卡在上限内仍命中，超出就放行

        两侧都断言才算数——只断言「超长不命中」的话，一个恒返回 None 的实现也能过。

        **填充物必须取自这一页自己的句式**（这里用 `send feedback`，它在
        `ProviderRefusalSignature.fragments` 里）。用无关填充（早先版本是 `" ok"`）会同时踩中
        域名档的**防引用**墙——剔掉已知句式后残余上百字，判定「这一页只是被夹在别的话里」，
        于是这条用例会因为**另一道闸门**变红，而它本来只想单独验证长度上限。
        换成页内句式后残余仍是 0，长度这一条才被真正隔离出来。
        """
        filler = " send feedback"
        room = MAX_REFUSAL_TEXT_CHARS - len(GEMINI_REFUSAL_TEXT)
        at_cap = GEMINI_REFUSAL_TEXT + filler * (room // len(filler))
        over_cap = GEMINI_REFUSAL_TEXT + filler * (room // len(filler) + 5)

        assert len(at_cap.strip()) <= MAX_REFUSAL_TEXT_CHARS
        assert len(over_cap.strip()) > MAX_REFUSAL_TEXT_CHARS
        assert is_provider_refusal_text(at_cap) is True, "上限内的样本被别的判据顺手判掉了，这条闸门没被单独验证到"
        assert is_provider_refusal_text(over_cap) is False


# --------------------------------------------------------------------------------------
# 四、每个合取都必须是承重墙（变异式）
# --------------------------------------------------------------------------------------


def _mutate(text: str, *replacements: Tuple[str, str]) -> str:
    mutated = text
    for old, new in replacements:
        assert old in mutated, f"变异用的原文片段不存在：{old!r}（夹具被改过？）"
        mutated = mutated.replace(old, new)
    assert mutated != text
    return mutated


# 判定现在有**两档**（见 `detect_provider_refusal` 的 docstring）：域名档
# （`PROVIDER_REFUSAL_SIGNATURES`）与整句 boilerplate 档（`PROVIDER_BOILERPLATE`）。
# 线上那段原文**两档都能命中**，所以要证明「域名档的某个合取是承重墙」，必须先把整句
# boilerplate 档那条路堵上——否则拆掉域名之后另一档照样接住，用例红得莫名其妙，
# 而真正的结论（域名合取有没有用）一个字都没测到。
#
# 堵法是把 boilerplate 档唯一的自足标志串换掉，其余一字不动。
_DOMAIN_TIER_ONLY = _mutate(
    GEMINI_REFUSAL_TEXT,
    ("The prompt could not be submitted", "The prompt was not accepted"),
)


# (用例名, 被破坏的合取, 变异后的正文)
CONJUNCT_MUTATIONS: List[Tuple[str, str, str]] = [
    (
        "换掉供应商域名",
        "hosts",
        _mutate(
            _DOMAIN_TIER_ONLY,
            ("policies.google.com", "policies.example.com"),
            ("ai.google.dev", "ai.example.dev"),
        ),
    ),
    (
        "去掉政策文件名",
        "phrase_groups[0]",
        _mutate(_DOMAIN_TIER_ONLY, ("Generative AI Prohibited Use policy", "Generative AI guidelines")),
    ),
    (
        "带上口语/人设标记",
        "has_conversational_markers",
        f"{GEMINI_REFUSAL_TEXT} 前辈这是刚才的报错哦",
    ),
    # 以下三条针对**整句 boilerplate 档**自己的合取。这一档没有域名做锚，
    # 顶住误杀的全部重量都压在「占满整段」与「语种没有错位」上。
    (
        "boilerplate 被别的话包着（残余量超标）",
        "_boilerplate_dominates",
        "The upstream just said: The prompt could not be submitted. "
        "Let me try to rephrase what you asked and send it again in a moment.",
    ),
    (
        "英文 boilerplate 被中文包着（语种错位＝有人在引用）",
        "_match_boilerplate/CJK",
        "上一轮的错误是 The prompt could not be submitted",
    ),
    (
        "只剩组内一半（Azure 那组要求两个标志同时命中）",
        "PROVIDER_BOILERPLATE.groups",
        "The response was filtered due to the prompt triggering the content management policy.",
    ),
    (
        "去掉违规/拦截动词",
        "phrase_groups[1]",
        _mutate(
            GEMINI_REFUSAL_TEXT,
            ("could not be submitted", "was not sent"),
            ("that violate", "that mention"),
        ),
    ),
    (
        "带上代码特征",
        "has_code_features",
        f"```\n{GEMINI_REFUSAL_TEXT}\n```",
    ),
    (
        "带上 at 标记",
        "has_agent_markup",
        f"[@id:1@] {GEMINI_REFUSAL_TEXT}",
    ),
]


class TestConjunctsAreLoadBearing:
    @pytest.mark.parametrize(("case", "conjunct", "text"), CONJUNCT_MUTATIONS)
    def test_every_conjunct_is_load_bearing(self, case: str, conjunct: str, text: str) -> None:
        """破坏任意一个合取，命中就必须消失

        这条同时守两个方向：判据不能松（松了这里会绿不掉），也证明每一条判据都真的参与了
        正例的命中，而不是写在那里凑数。
        """
        assert detect_provider_refusal(text) is None, f"{case}: 破坏了 {conjunct} 之后仍然命中，说明该合取形同虚设"

    def test_mutations_only_break_one_thing(self) -> None:
        """变异样本本身仍然是「拒绝页的样子」——排除「因为被改烂了所以不命中」"""
        for case, conjunct, text in CONJUNCT_MUTATIONS:
            if conjunct.startswith("phrase_groups") or conjunct == "hosts":
                assert has_code_features(text) is False, f"{case}: 变异意外引入了代码特征，这条用例证明不了 {conjunct}"
                assert len(text.strip()) <= MAX_REFUSAL_TEXT_CHARS, case

    def test_the_domain_tier_only_base_still_hits(self) -> None:
        """域名档变异的**起点**必须自己还命中，否则那两条用例是空断言

        `_DOMAIN_TIER_ONLY` 是「堵掉整句 boilerplate 档之后」的原文。它要是已经不命中了，
        再往下拆域名/政策名当然也不命中——两条用例会永远绿，而域名合取有没有用一个字没测到。
        变异实测：把域名判据整个删掉，没有这一条时上面两条照样绿。
        """
        match = detect_provider_refusal(_DOMAIN_TIER_ONLY)

        assert match is not None, "堵掉 boilerplate 档之后，域名档必须还能独立接住这段原文"
        assert match.matched_host in PROVIDER_REFUSAL_SIGNATURES[0].hosts, "它必须是被**域名档**接住的"

    def test_the_boilerplate_tier_carries_the_truncated_page_alone(self) -> None:
        """反过来：整句 boilerplate 档也必须能独立成立（去掉域名与政策名之后仍然命中）

        没有这一条，把 `PROVIDER_BOILERPLATE` 整张表删掉时，上面那组用例全部照常绿。
        """
        truncated = "The prompt could not be submitted."

        match = detect_provider_refusal(truncated)

        assert match is not None, "被截断到只剩第一句的拒绝页漏判了"
        assert match.matched_host == "", "它必须是被**整句 boilerplate 档**接住的（那一档没有域名）"


# --------------------------------------------------------------------------------------
# 五、判据表的结构不变量（防后人把合取悄悄放宽成单条件）
# --------------------------------------------------------------------------------------

# 通用到单独出现毫无判别力的词。它们**只能**作为更长短语的一部分出现。
_BANNED_STANDALONE_PHRASES = frozenset(
    {
        "policy",
        "policies",
        "sorry",
        "error",
        "blocked",
        "prohibited",
        "content",
        "refused",
        "safety",
        "违反",
        "抱歉",
        "拒绝",
    },
)


class TestSignatureTableInvariants:
    def test_every_signature_is_a_real_conjunction(self) -> None:
        assert PROVIDER_REFUSAL_SIGNATURES, "签名表不能为空，否则整个模块是空判据"
        for signature in PROVIDER_REFUSAL_SIGNATURES:
            assert signature.hosts, f"{signature.provider}: 必须有域名要求"
            assert len(signature.phrase_groups) >= 2, f"{signature.provider}: 至少两个短语组，否则退化成单条件"
            for group in signature.phrase_groups:
                assert group, f"{signature.provider}: 空短语组会让这一组恒不满足"

    def test_hosts_are_bare_domains(self) -> None:
        for signature in PROVIDER_REFUSAL_SIGNATURES:
            for host in signature.hosts:
                assert "." in host, f"{signature.provider}: {host!r} 不像域名"
                assert "://" not in host, f"{signature.provider}: {host!r} 不要带协议头，判据是子串匹配"
                assert host == host.lower(), f"{signature.provider}: {host!r} 必须是小写（匹配前会 lower）"

    def test_hosts_are_specific_subdomains(self) -> None:
        """域名必须是**具体的政策/文档子域**，不能是供应商的裸主域

        域名是这套判据里唯一的信任锚点。写成 `google.com` 的话，「剧情里提了一句 Google」
        这类正常输出就跨过了第一道合取，整条判据只剩两组英文短语撑着——保守性肉眼可见地
        塌了一半。要求至少两个点（即带子域）把这种放宽挡在门外。

        将来真有供应商把拒绝页挂在裸主域上，那是一次需要重新评估误杀面的判断，
        应当连同新的逐字实据一起讨论，而不是把这条断言悄悄删掉。
        """
        for signature in PROVIDER_REFUSAL_SIGNATURES:
            for host in signature.hosts:
                assert host.count(".") >= 2, f"{signature.provider}: {host!r} 是裸主域，信任锚点太宽"

    def test_no_host_subsumes_another(self) -> None:
        """同一签名里不许有一个域名是另一个的子串

        `("policies.google.com", "google.com")` 这种写法表面上是「多加了一条」，实际是把
        前一条整个作废、把匹配面放宽到整个主域。变异实测：不加这条断言，往表里塞
        `google.com` 全绿。
        """
        for signature in PROVIDER_REFUSAL_SIGNATURES:
            for host_a in signature.hosts:
                for host_b in signature.hosts:
                    if host_a is host_b:
                        continue
                    assert host_b not in host_a, (
                        f"{signature.provider}: {host_b!r} 是 {host_a!r} 的子串，宽的那条会把窄的那条架空"
                    )

    def test_no_standalone_generic_keyword(self) -> None:
        """任何一组里都不许出现「单独一个通用词」——那等于把合取降级成关键词判据"""
        for signature in PROVIDER_REFUSAL_SIGNATURES:
            for index, group in enumerate(signature.phrase_groups):
                for phrase in group:
                    assert phrase == phrase.lower(), f"{signature.provider}: {phrase!r} 必须小写"
                    assert phrase not in _BANNED_STANDALONE_PHRASES, (
                        f"{signature.provider} 第 {index} 组的 {phrase!r} 是通用词，单独出现没有判别力"
                    )

    def test_phrase_groups_do_not_degenerate(self) -> None:
        """组间短语不许互为子串

        举例：把 `prohibited` 加进第二组，第一组的 `prohibited use policy` 一旦命中，
        第二组就自动命中 —— 合取表面还在，实际只剩一个条件。这条用例把那种改动挡在门外。
        """
        for signature in PROVIDER_REFUSAL_SIGNATURES:
            groups = signature.phrase_groups
            for i, group_a in enumerate(groups):
                for j, group_b in enumerate(groups):
                    if i == j:
                        continue
                    for phrase_a in group_a:
                        for phrase_b in group_b:
                            assert phrase_b not in phrase_a, (
                                f"{signature.provider}: 第 {j} 组的 {phrase_b!r} 是第 {i} 组 {phrase_a!r} 的子串，"
                                f"两组合取会退化成单条件"
                            )


# --------------------------------------------------------------------------------------
# 六、代码特征探测器本身
# --------------------------------------------------------------------------------------

# (用例名, 正文, 是否含代码特征)
CODE_FEATURE_SAMPLES: List[Tuple[str, str, bool]] = [
    ("函数调用", "send_msg_text(_ck, 'hi')", True),
    ("下标取值", "data['key']", True),
    ("赋值", "reply_mode = 'image_only'", True),
    ("增量赋值", "count += 1", True),
    ("注释行", "# 违反规则的做法", True),
    ("import 行", "import os", True),
    ("from import 行", "from core.api import send", True),
    ("行首 pass", "pass", True),
    ("围栏", "```python\nprint(1)\n```", True),
    ("三引号", "text = '''hi'''", True),
    ("裸代码-顶层 await", "await send()", True),
    ("孤零零一个词", "policy", True),
    ("拒绝页原文", GEMINI_REFUSAL_TEXT, False),
    ("中文剧情对白", "抱歉呀前辈 我自己其实没有在群里修改名片和头像的权限呢", False),
    ("英文散文带空格括号", "That would violate the policy (see the terms) so I won't.", False),
    ("空串", "", False),
    ("纯空白", "  \n\t ", False),
]


# (信号名, 只触发这一个信号的正文)。刻意都拼成「编译不过」的形态：`ast.parse` 那条兜底
# 太强，几乎能替所有标点特征背锅，于是删掉任何一条正则用例都不会红（变异实测：删掉
# `_COMMENT_LINE_PATTERN` 时上表 62 条全绿）。这张表把每条正则单独钉住。
SINGLE_SIGNAL_SAMPLES: List[Tuple[str, str]] = [
    ("围栏", "``` 抱歉 我 真的 不能 说"),
    ("函数调用", "send(x) 抱歉 这个 不行"),
    ("下标", "data[0] 抱歉 这个 不行"),
    ("赋值", "mode = 图片 模式 抱歉"),
    ("注释行", "# 先跟前辈说一声\n抱歉 我这边真的做不到"),
    ("行首关键字", "import 的 用法 抱歉 我 不能 说"),
    ("三引号", "''' 抱歉 这段 说明"),
    # 下面两条把信号放在**第二行**：`^` 靠的是 `re.MULTILINE`，把整段折成一行就再也匹配不上。
    # 只有首行样本的话，「代码特征也改判在折过的单行上」这种改动不会被任何用例发现。
    ("注释行-非首行", "抱歉 我 想 想\n# 这里 本来 该 写 代码"),
    ("行首关键字-非首行", "抱歉 我 先 说 一句\nimport 的 用法 不 对"),
]


class TestCodeFeatureDetector:
    @pytest.mark.parametrize(("case", "text", "expected"), CODE_FEATURE_SAMPLES)
    def test_has_code_features(self, case: str, text: str, expected: bool) -> None:
        assert has_code_features(text) is expected, case

    @pytest.mark.parametrize(("case", "text"), SINGLE_SIGNAL_SAMPLES)
    def test_each_signal_is_independently_load_bearing(self, case: str, text: str) -> None:
        """每条代码特征都要能**单独**判出代码，不许全靠 `ast.parse` 兜底

        先断言样本编译不过（证明兜底这条路已经堵死），再断言仍然判为含代码特征 ——
        两条一起才说明是这条正则在起作用。少了前一条，删掉任意一条正则用例都不会红。
        """
        with pytest.raises(SyntaxError):
            ast.parse(text)
        assert has_code_features(text) is True, case

    def test_prose_parenthesis_is_not_a_call(self) -> None:
        """英文散文里的「词 + 空格 + 括号」不许被当成函数调用

        允许 `\\s*` 的话，供应商拒绝页自己的措辞（`policy (see below)` 这类）就会被判成代码，
        整个模块直接失效。这条把「紧贴」这个细节钉死。
        """
        assert has_code_features("Please review the policy (see below) before retrying.") is False
        assert has_code_features("Please review the policy(see_below) before retrying.") is True

    def test_urls_are_stripped_before_scanning(self) -> None:
        """URL 里的 `?key=value` 不许被当成赋值 —— 否则正例会被自己的政策链接判掉"""
        assert has_code_features("See https://example.com/a?key=value&x=1 for details.") is False
