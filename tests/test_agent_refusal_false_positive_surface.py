"""供应商拒答判据的**误杀面**：对抗式复核

## 这一组在防什么

这是个**角色扮演 Bot**。上游拒答判据吹宽一格的后果不是「偶尔多拦一条」，而是：
小樱一说「抱歉」整轮就被掐掉。它比原 bug 更难发现——原 bug 会给用户一条
「已经试到最大迭代次数啦」，而误杀是**静默地少回一次话**，日志里只有一条
「上游拒绝受理」，看上去还挺合理。

所以本文件的重心是**下半张表**（负例），不是召回。判据的正确性在这里被定义为：
「误杀 0」是硬约束，「召回」是在这个约束下尽量取的。

## 语料从哪来

1. **仓库真实语料**：`TestRepoCorpusSweep` 现场把 `nekro_agent/` 与 `tests/` 下**每一条**
   字符串字面量抓出来跑一遍判据（实测 4 万余条，其中 300 余条带人设/协议痕迹）。
   这不是造的样本，是这个项目里真实写过的文本。
2. **小樱人设对白**：`PERSONA_PROSE`(40) / `PERSONA_CODE`(7) / `PERSONA_ECHO`(7)，共 54 条，
   每条都含「抱歉/不能/违反/policy/规则」之一——也就是说**单关键词判据会把它们几乎全部误杀**
   （`test_negatives_defeat_a_naive_judge` 把这一点变成断言，否则「负例全部放行」
   可能只是因为样本本来就不像拒答）。
3. **运行时文本**：沙盒 traceback、裸链接、裸政策名（3 条）。

合计 57 条定向负例。

## 改造前的实测基线（本文件的存在理由）

判定统一到 `provider_refusal` 之前，`run_agent` 里那份「整句标志串的或」在同一份语料上：

- 仓库字面量（去重后 11473 条那一轮）：**误杀 14 条**——裸链接、裸政策名、
  沙盒 traceback、模型在代码里引用那句话；
- 下面这 57 条定向负例：**误杀 8 条**，全部是「模型引用了这一页」那一类。

同一份语料上 `provider_refusal` 误杀 0。差别来自结构：单条整句标志串的「或」回答不了
「**谁**在说这句话」，而拒答原文正是被回喂进上下文的东西，模型下一轮引用它**可预期**，
不是假想形态。
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import List, Tuple

import pytest

from nekro_agent.services.agent.provider_refusal import (
    _CONVERSATIONAL_MARKERS,
    MAX_RESIDUAL_CHARS,
    PROVIDER_BOILERPLATE,
    ResponseRefusalKind,
    classify_response_refusal,
    detect_provider_refusal,
    has_conversational_markers,
    is_provider_refusal_text,
)
from nekro_agent.services.agent.run_agent import (
    is_provider_refusal as run_agent_is_provider_refusal,
)

# ======================================================================================
# 一、小樱的**正常**对白：每条都含拒答关键词，每条都必须放行
# ======================================================================================

# 形态一：裸散文。**这是误杀风险最高的一侧**——没有围栏就是 `code_missing`，
# 而收尾判据恰恰挂在 `code_missing` 那条分支里。
PERSONA_PROSE: List[Tuple[str, str]] = [
    ("道歉-小事", "抱歉抱歉前辈，刚才走神了没听清，你再说一遍好不好呀 QwQ"),
    ("道歉-迟到", "对不起啦，我迟到了……路上被一只猫拦住了，它不让我过去嘛！"),
    ("能力边界", "唔…这个我真的不能做到诶，小樱又不是万能的小机器人呀。"),
    ("拒绝-撒娇", "不可以哦！那是前辈上次答应过我的布丁，说好不能反悔的。"),
    ("规则-群规", "群里有规则的呀，刷屏会被管理员禁言的，前辈可别违反啦。"),
    ("规则-游戏", "这局游戏的规则我记得很清楚：谁先笑谁就输，你已经违反两次了！"),
    ("policy-英文夹杂", "这家公司的 policy 写得好复杂哦，前辈你真的看完了吗？"),
    ("policy-保险", "保险的 policy 到期了要记得续，不然出事就赔不了了呢。"),
    ("英文道歉", "Sorry senpai, I can't help with that one - it is beyond me."),
    ("英文拒绝", "I cannot do that, but I can tell you a story about a cat instead."),
    ("违反-剧情", "他违反了骑士团的誓约，所以才被逐出王都的……这段剧情你忘啦？"),
    ("违反-约定", "说好了不许偷看的，你违反约定了！要罚你请我吃冰淇淋。"),
    ("无法-情绪", "我无法理解为什么有人会讨厌下雨天，明明那么好听。"),
    ("做不到", "做不到就是做不到嘛，小樱又不会魔法……不过我可以陪你一起想办法。"),
    ("拒绝-危险", "这个我拒绝哦，太危险了，前辈不许自己一个人去。"),
    ("安全提醒", "别乱点陌生链接呀，网上说那种是违规的钓鱼站，会被骗钱的。"),
    ("内容边界-温和", "唔…这个话题有点越界了啦，我们聊点别的好不好？"),
    ("引用政策词", "老师说抄作业违反学术规范，会被记过的哦，前辈可别乱来。"),
    ("google-无政策", "我刚 google 了一下，好像没有这家店诶，是不是关门了呀？"),
    ("链接-普通", "喏，这个是官网 https://example.com/help ，规则都写在里面啦。"),
    ("中英混用拒绝", "抱歉呀前辈，I can't 帮你写那个，会被骂的啦。"),
    (
        "长对白-道歉",
        "抱歉抱歉真的很抱歉！我不是故意把你的杯子打碎的，我会赔一个一模一样的给你，"
        "虽然可能要等下个月发零花钱……前辈你不要生气好不好嘛 QwQ",
    ),
    ("反问-规则", "规则是谁定的呀？凭什么我不能吃第二块蛋糕？这不公平！"),
    ("委屈", "我没有违反任何规则呀，明明是他先动手的……前辈你要相信我。"),
    ("解释-限制", "系统那边有限制的，我不能一次发太多条消息，会被当成刷屏的呢。"),
    ("英文-policy句", "Their refund policy says no returns after 30 days. That is rough."),
    ("拒绝-角色内正式", "很抱歉，作为小樱我无法协助处理这个请求，我们换个话题吧。"),
    ("玩笑-违规", "偷偷告诉你，我昨天违规吃了两份夜宵，嘘——不许告诉别人哦！"),
    ("英文-sorry长", "I am sorry, senpai. I really am. But I cannot pretend nothing happened."),
    ("提到审查", "有些话在群里说会被和谐掉的呀，不是我不想说，是规则不让。"),
    ("提到模型", "我也是有安全策略的哦，太过分的要求我会拒绝的，不过你这个还好啦~"),
    ("撒娇-不能", "不能不能不能！今天不能加班！前辈你已经连续熬了三个晚上了呀！"),
    ("学术-违反", "论文里直接复制别人的段落会违反引用规范，要标注来源才行呢。"),
    ("英文-blocked", "The door was blocked by a huge pile of boxes, so we went around."),
    ("英文-violate", "Nobody wants to violate the rules on their first day, right?"),
    ("英文-prohibited", "The sign said smoking is prohibited, so we moved to the terrace."),
    ("英文-submitted", "I submitted the form yesterday but they said it could not be processed."),
    ("英文-sensitive", "He is sensitive about that topic, so please be gentle with him."),
    ("中文-安全策略", "唔…这种内容大概会触发安全策略吧，我们还是别试了。"),
    ("复述故障-中文", "刚才好像出了点问题，模型那边说内容被挡下来了，我换个说法再讲一次哦。"),
]

# 形态二：模型按本项目协议写出来的东西（围栏 / 发言调用 / at 标记 / 思维链）
PERSONA_CODE: List[Tuple[str, str]] = [
    ("code-道歉", "```python\nsend_msg_text(_ck, '抱歉啦前辈，这个我真的不能做到 QwQ')\n```"),
    ("code-规则", "```python\nsend_msg_text(_ck, '群规第三条写着不能刷屏哦，违反了会被禁言的')\n```"),
    ("code-policy", "```python\nsend_msg_text(_ck, 'Their policy says returns are prohibited.')\n```"),
    ("code-无围栏", "send_msg_text(_ck, '对不起呀，我无法帮你做这件事')"),
    ("code-带注释", "```python\n# 前辈问了个我不能回答的问题\nsend_msg_text(_ck, '这个我拒绝哦~')\n```"),
    ("code-at标记", "[@id:114514@] 抱歉呀，这条我不能回答的说"),
    ("code-think", "<think>用户在问一个违反规则的问题</think>\nsend_msg_text(_ck, '换个话题吧~')"),
]

# 形态三：**复述上游故障**。拒答原文会被回喂进上下文，模型下一轮引用它是可预期的，
# 不是假想形态——改造前 `run_agent` 那份判据把下面每一条都判成了「上游拒答」。
PERSONA_ECHO: List[Tuple[str, str]] = [
    (
        "echo-散文",
        "唔…刚才上游提示 The prompt could not be submitted，是被安全策略挡下来了吧，"
        "前辈换个说法再问我一次好不好？",
    ),
    (
        "echo-代码",
        "```python\nsend_msg_text(_ck, '上一轮报错说 The prompt could not be submitted，我再换个说法哦')\n```",
    ),
    (
        "echo-解释政策名",
        "那句话的意思是内容碰到了 Generative AI Prohibited Use policy 这份政策啦，"
        "换个措辞通常就好了哦。",
    ),
    (
        "echo-带链接",
        "前辈你看，报错里给的链接是 https://policies.google.com/terms/generative-ai/use-policy ，"
        "我帮你翻译一下：就是说刚才那段话里有敏感词啦。",
    ),
    (
        "echo-代码带链接",
        "```python\nsend_msg_text(_ck, '政策原文在 https://policies.google.com/terms/generative-ai/use-policy 哦')\n```",
    ),
    (
        "echo-中文转述",
        "刚才那次请求没能提交成功，说是内容违反了 Google 的生成式 AI 使用政策，我们换个说法吧~",
    ),
    # 这一条是**口语闸门唯一的承重点**：它把中文拒绝页的三个合取（域名 + 政策文件名 +
    # 违规动词）**全部**踩中了，域名档拦不住它。删掉 `has_conversational_markers`
    # 这条立刻误杀——`test_conversational_gate_is_load_bearing` 钉住这一点。
    (
        "echo-中文转述带链接",
        "它说我们刚才的话违反了使用政策哦，链接在 "
        "https://policies.google.com/terms/generative-ai/use-policy 这里呀，我们换个说法吧~",
    ),
]

# 形态四：运行时文本。它们不是 LLM 正文，但同一个判定函数一旦被复用就会中招——
# 改造前 `run_agent` 那份把这三条全判成了「上游拒答」。
RUNTIME_TEXTS: List[Tuple[str, str]] = [
    (
        "沙盒traceback复述了那句话",
        'Traceback (most recent call last):\n  File "/app/run_script.py", line 1\n'
        "    The prompt could not be submitted.\n        ^\n"
        "SyntaxError: unterminated string literal (detected at line 1)",
    ),
    ("裸链接", "https://policies.google.com/terms/generative-ai/use-policy"),
    ("裸政策名", "Generative AI Prohibited Use policy"),
]

ALL_NEGATIVES: List[Tuple[str, str]] = [
    *(("prose/" + name, text) for name, text in PERSONA_PROSE),
    *(("code/" + name, text) for name, text in PERSONA_CODE),
    *(("echo/" + name, text) for name, text in PERSONA_ECHO),
    *(("runtime/" + name, text) for name, text in RUNTIME_TEXTS),
]


# ======================================================================================
# 二、变体拒绝页：换措辞 / 换链接 / 去 markdown / 中文化
# ======================================================================================

REAL_REFUSAL = (
    "The prompt could not be submitted. The prompt contains sensitive words that violate "
    "Google's [Generative AI Prohibited Use policy](https://policies.google.com/terms/generative-ai/use-policy). "
    "Try rephrasing the prompt. If you think this was an error, "
    "[send feedback](https://ai.google.dev/gemini-api/docs/troubleshooting)."
)

# (用例名, 正文, 期望命中)
REFUSAL_VARIANTS: List[Tuple[str, str, bool]] = [
    ("V0-逐字原文", REAL_REFUSAL, True),
    (
        "V1-去markdown",
        "The prompt could not be submitted. The prompt contains sensitive words that violate "
        "Google's Generative AI Prohibited Use policy: "
        "https://policies.google.com/terms/generative-ai/use-policy . Try rephrasing the prompt.",
        True,
    ),
    (
        "V2-换措辞-blocked",
        "Your prompt was blocked because it violates the Google Generative AI Prohibited Use Policy. "
        "See https://policies.google.com/terms/generative-ai/use-policy for details.",
        True,
    ),
    (
        "V3-换链接-ai.google.dev",
        "This request was blocked by our safety filters. The content violates the "
        "Generative AI Prohibited Use Policy. Learn more at "
        "https://ai.google.dev/gemini-api/docs/safety-guidance",
        True,
    ),
    (
        "V4-中文化",
        "该提示词无法提交。提示词中包含违反 Google《生成式 AI 使用限制政策》的敏感词，"
        "详见 https://policies.google.com/terms/generative-ai/use-policy 。请尝试换一种说法。",
        True,
    ),
    # 已知漏杀 1：没有任何供应商身份锚点（无域名、也不是某家的整句 boilerplate）。
    # 它与小樱自己转述故障时说的话在**原理上**分不开，只能放行。
    (
        "V5-中文化-无链接（已知漏杀）",
        "抱歉，您的请求已被拦截：内容违反了生成式 AI 使用政策，请修改后重试。",
        False,
    ),
    (
        "V6-OpenAI形态",
        "Your request was rejected as a result of our safety system. "
        "Your prompt may contain text that is not allowed by our safety system.",
        True,
    ),
    (
        "V7-Azure形态",
        "The response was filtered due to the prompt triggering Azure OpenAI's content management policy. "
        "Please modify your prompt and retry.",
        True,
    ),
    # 已知漏杀 2：这句是本用例**自己编的**，没有任何逐字实据。为没有实据的供应商写标志串
    # 等于凭空扩大误杀面，所以刻意不收（见 `provider_refusal` 模块 docstring 的 TODO）。
    ("V8-Anthropic形态（已知漏杀·无实据）", "Output blocked by content filtering policy", False),
    (
        "V9-折行",
        "The prompt could not\nbe submitted. The prompt contains sensitive words\n"
        "that violate Google's [Generative AI Prohibited Use\npolicy]"
        "(https://policies.google.com/terms/generative-ai/use-policy).\nTry rephrasing the prompt.",
        True,
    ),
    (
        "V10-全大写",
        "THE PROMPT COULD NOT BE SUBMITTED. THE PROMPT CONTAINS SENSITIVE WORDS THAT VIOLATE "
        "GOOGLE'S GENERATIVE AI PROHIBITED USE POLICY "
        "(HTTPS://POLICIES.GOOGLE.COM/TERMS/GENERATIVE-AI/USE-POLICY).",
        True,
    ),
    (
        "V11-渠道加了模型名前缀",
        "[gemini-3.7-flash-high] The prompt could not be submitted. The prompt contains sensitive "
        "words that violate Google's Generative AI Prohibited Use policy "
        "(https://policies.google.com/terms/generative-ai/use-policy).",
        True,
    ),
    (
        "V12-中转站中文包装",
        "上游返回错误：The prompt could not be submitted. The prompt contains sensitive words that "
        "violate Google's Generative AI Prohibited Use policy. "
        "(https://policies.google.com/terms/generative-ai/use-policy)",
        True,
    ),
    ("V13-被截断到只剩第一句", "The prompt could not be submitted.", True),
]


# ======================================================================================
# 三、误杀：硬约束，一条都不许有
# ======================================================================================


class TestNoFalsePositiveOnPersonaDialogue:
    @pytest.mark.parametrize(("case", "text"), ALL_NEGATIVES)
    def test_persona_dialogue_is_never_a_provider_refusal(self, case: str, text: str) -> None:
        assert detect_provider_refusal(text) is None, f"{case}: 误杀了正常对白"
        assert is_provider_refusal_text(text) is False, case

    @pytest.mark.parametrize(("case", "text"), ALL_NEGATIVES)
    def test_the_wired_predicate_agrees(self, case: str, text: str) -> None:
        """真正接进 `run_agent` 的那个入口也必须放行

        分开断言不是重复：`run_agent` 曾经有**自己的一份**判定，两份同功能实现各自演化，
        而误杀只会从**接线的那一份**里发生。只测 `provider_refusal` 的话，
        本文件对线上行为一句话都没说。
        """
        assert run_agent_is_provider_refusal(text) is False, f"{case}: 接线入口误杀了正常对白"

    @pytest.mark.parametrize(("case", "text"), ALL_NEGATIVES)
    def test_negatives_land_in_a_pass_through_class(self, case: str, text: str) -> None:
        """三档分类里，正常对白只能落到放行的那两档"""
        assert classify_response_refusal(text) is not ResponseRefusalKind.PROVIDER_REFUSAL, case

    def test_negatives_defeat_a_naive_judge(self) -> None:
        """负例必须**足够对抗**：单关键词判据要能误杀其中绝大多数

        没有这一条，「56 条全部放行」可能只是因为样本本来就不像拒答，证明不了判据的保守性。
        """
        naive_keywords = ("抱歉", "对不起", "不能", "无法", "拒绝", "违反", "规则", "sorry", "policy", "blocked")

        def naive_judge(text: str) -> bool:
            lowered = text.lower()
            return any(token in lowered for token in naive_keywords)

        fooled = [case for case, text in ALL_NEGATIVES if naive_judge(text)]

        assert len(fooled) >= 40, (
            f"只有 {len(fooled)}/{len(ALL_NEGATIVES)} 条能骗过单关键词判据，负例表失去了对抗性"
        )

    def test_the_negative_corpus_is_big_enough_to_mean_something(self) -> None:
        """样本量本身是结论的一部分，缩水了要看得见"""
        assert len(PERSONA_PROSE) >= 30, "任务要求至少 30 条人设对白"
        assert len(ALL_NEGATIVES) >= 55


# ======================================================================================
# 四、召回：在误杀 0 的前提下尽量高
# ======================================================================================


class TestRecallOnRefusalVariants:
    @pytest.mark.parametrize(("case", "text", "expected"), REFUSAL_VARIANTS)
    def test_variant_table(self, case: str, text: str, expected: bool) -> None:
        assert is_provider_refusal_text(text) is expected, f"{case}: 变体拒绝页的结论不符"

    @pytest.mark.parametrize(("case", "text", "expected"), REFUSAL_VARIANTS)
    def test_the_wired_predicate_agrees(self, case: str, text: str, expected: bool) -> None:
        assert run_agent_is_provider_refusal(text) is expected, f"{case}: 接线入口与判定模块结论不一致"

    def test_known_misses_are_exactly_these_two(self) -> None:
        """漏杀清单必须是**显式**的

        「漏了几条」不写死的话，判据被后人收窄到只认逐字原文时这张表照样绿——
        召回悄悄塌掉而没有任何用例发红。反过来，将来把 V5/V8 收进来时这条会提醒
        改的人：那是一次需要重新评估误杀面的判断。
        """
        missed = {case for case, _text, expected in REFUSAL_VARIANTS if not expected}

        assert missed == {
            "V5-中文化-无链接（已知漏杀）",
            "V8-Anthropic形态（已知漏杀·无实据）",
        }, f"漏杀清单变了：{missed}"

    def test_recall_is_at_least_eleven_of_thirteen(self) -> None:
        hits = [case for case, text, _ in REFUSAL_VARIANTS if is_provider_refusal_text(text)]

        assert len(hits) >= 12, f"召回塌了，只命中 {len(hits)}/{len(REFUSAL_VARIANTS)}"


# ======================================================================================
# 五、新加的三道闸门各自都是承重墙
# ======================================================================================


# 一段**踩满域名档三个合取**（域名 + 政策文件名「使用政策」+ 违规动词「违反了」）、
# 却一个口语标记都不带的中文正文。它本身必须命中——所有「加上某个标记就放行」的对照
# 都以此为基准，基准一旦不命中，那些对照全是空断言。
_GATE_PROBE_BASE = "内容违反了使用政策，详见 https://policies.google.com/terms/generative-ai/use-policy"


class TestNewGuardsAreLoadBearing:
    """每条断言都要能回答「这道闸门不存在时我会不会红」——所以每条都配一个**只靠它**放行的样本。"""

    def test_conversational_gate_is_load_bearing(self) -> None:
        """口语闸门：中文拒绝页的三个合取全被踩中，只剩它拦得住

        样本把域名、政策文件名（「使用政策」）、违规动词（「违反了」）**同时**踩中了，
        域名档会直接命中。删掉 `has_conversational_markers` 这条立刻误杀。
        """
        echo = dict(PERSONA_ECHO)["echo-中文转述带链接"]

        # 先证明它真的踩满了域名档的三个合取——否则这条用例证明不了闸门
        lowered = echo.lower()
        assert "policies.google.com" in lowered
        assert "使用政策" in echo
        assert "违反了" in echo

        assert has_conversational_markers(echo) is True, "样本里没有口语标记，这条用例形同虚设"
        assert detect_provider_refusal(echo) is None, "口语闸门没拦住，中文拒绝页的回声被误杀"

    def test_conversational_gate_does_not_swallow_a_real_chinese_refusal(self) -> None:
        """反向：真正的中文拒绝页里没有口语标记，闸门不许把它一起放掉"""
        chinese_page = dict((c, t) for c, t, _ in REFUSAL_VARIANTS)["V4-中文化"]

        assert has_conversational_markers(chinese_page) is False, "闸门收的词太宽，把中文拒绝页也放行了"
        assert is_provider_refusal_text(chinese_page) is True

    def test_every_conversational_marker_is_individually_load_bearing(self) -> None:
        """标记表里的**每一条**都要能单独把一段「踩满合取的中文回声」拦下来

        逐条测而不是整体测，是因为整体测挡不住「删掉其中一条」这种改动：其余标记会替它兜住
        所有样本，用例照样全绿——变异实测：把 `前辈` 从表里去掉，没有这一条时无人发红。
        而删掉一条的方向恰恰是**危险**的那一侧（闸门少一次触发 = 多一次误杀）。

        构造法：先造一段**踩满域名档三个合取**、且一个口语标记都不带的中文正文
        （它必须命中，否则下面的对照没有意义），再逐条把标记接上去。
        """
        assert has_conversational_markers(_GATE_PROBE_BASE) is False, "基准句里混进了口语标记，逐条对照失效"
        assert detect_provider_refusal(_GATE_PROBE_BASE) is not None, (
            "基准句本身就不命中，那么下面每一条「加了标记就放行」都是空断言"
        )

        for marker in _CONVERSATIONAL_MARKERS:
            echoed = f"{_GATE_PROBE_BASE} {marker}"
            assert has_conversational_markers(echoed) is True, f"标记 {marker!r} 没有被识别"
            assert detect_provider_refusal(echoed) is None, (
                f"标记 {marker!r} 不是承重墙：带上它之后仍然被判成上游拒答"
            )

    def test_the_marker_table_still_contains_the_ones_that_matter(self) -> None:
        """标记表必须**逐字**含有这几条

        上面那条逐条用例是遍历表本身写的，所以它对「把某一条改名/删掉」是瞎的——
        表里少一条，它就少测一条，照样全绿。变异实测：把 `前辈` 改成 `zzz前辈`，
        只有这条会红。

        这张清单不是随手挑的：下面每一条都在本文件的负例语料里真实出现过，
        也就是说删掉任何一条，都会有一条小樱的正常对白失去它唯一的放行理由。
        """
        required = ("前辈", "呀", "啦", "嘛", "呢", "哦", "唔", "吧", "qwq", "~")

        missing = [marker for marker in required if marker not in _CONVERSATIONAL_MARKERS]
        assert not missing, f"口语标记表里少了：{missing}"

        # 清单自检：每一条都得在负例语料里真的出现过，否则这张清单只是凭空写的
        corpus_blob = " ".join(text for _case, text in ALL_NEGATIVES).lower()
        unused = [marker for marker in required if marker not in corpus_blob]
        assert not unused, f"这几条在负例语料里根本没出现，清单失去依据：{unused}"

    def test_a_marker_glued_to_a_url_is_still_seen(self) -> None:
        """中文紧贴链接末尾（不加空格）时，摘 URL 不许把它一起吞掉

        中文不加空格直接跟在链接后面是中文语境的常态（「详见 https://example.com/x哦」）。
        摘 URL 的正则一旦写成 `https?://\\S+`，那个「哦」会连同链接一起被吃掉——
        而它正是判「这是小樱在说话」的唯一依据，吃掉就等于闸门在这一格上静默失效。
        实测：改回 `\\S+` 这条立刻红。

        对照组用 ASCII 的 `~`：它紧贴在链接末尾时**本来就分不清是不是路径的一部分**，
        所以那种形态不作要求，只要求非 ASCII 的标记不被吞。
        """
        glued = f"{_GATE_PROBE_BASE}哦"

        assert has_conversational_markers(glued) is True, "紧贴链接的中文语气词被摘 URL 一起吞掉了"
        assert detect_provider_refusal(glued) is None

    def test_conversational_gate_ignores_markers_inside_urls(self) -> None:
        """口语闸门扫描前必须先摘掉 URL

        `~` 在这张标记表里（颜文字/卖萌的波浪号），而它在 URL 路径里是**合法字符**
        （`/~docs` 这种老派路径至今仍在用）。不摘 URL 的话，一页链接里带 `~` 的拒绝页会被
        「这是小樱在说话」放行——闸门本来只该看正文。

        这一条是定向钉子：不摘 URL 时它红，摘了才绿。
        """
        page_with_tilde_url = (
            "The prompt could not be submitted. The prompt contains sensitive words that violate "
            "Google's Generative AI Prohibited Use policy "
            "(https://policies.google.com/~terms/generative-ai/use-policy)."
        )

        assert "~" in page_with_tilde_url, "样本里没有 ~，这条用例证明不了摘 URL 这一步"
        assert has_conversational_markers(page_with_tilde_url) is False, "URL 里的 ~ 被当成了口语标记"
        assert is_provider_refusal_text(page_with_tilde_url) is True

    def test_dominance_gate_is_load_bearing(self) -> None:
        """占满整段闸门：整句 boilerplate 档没有域名做锚，全部重量压在它身上"""
        quoted = (
            "The upstream just said: The prompt could not be submitted. "
            "Let me try to rephrase what you asked and send it again in a moment."
        )

        # 标志串确实命中了，被拦下来的原因只可能是残余量
        assert "the prompt could not be submitted" in quoted.lower()
        assert has_conversational_markers(quoted) is False, "这条不许靠口语闸门放行，否则证明不了残余量闸门"
        assert detect_provider_refusal(quoted) is None, "boilerplate 被别的话包着，仍然被判成上游拒答"

        # 同一句话独占整段时必须命中——两侧都断言，否则恒 None 的实现也能过
        assert is_provider_refusal_text("The prompt could not be submitted.") is True

    def test_residual_budget_ignores_the_policy_link(self) -> None:
        """算残余量时必须先摘掉 URL

        政策链接是拒绝页**自己的一部分**，不是「这一页之外还说了什么」。把它算进残余量，
        一条 47 字符的政策链接就单枪匹马顶爆了预算，带链接但没写全政策名的截断形态直接漏判。

        这一条是定向钉子：不摘 URL 时它红。
        """
        truncated_with_link = (
            "The prompt could not be submitted. "
            "(https://policies.google.com/terms/generative-ai/use-policy)"
        )

        # 先证明它走的确实是**整句 boilerplate 档**：域名档要的政策文件名不在里面
        # （URL 里的 `use-policy` 带连字符，归一化不会把它变成 `use policy`）
        assert "use policy" not in " ".join(truncated_with_link.lower().split())

        match = detect_provider_refusal(truncated_with_link)
        assert match is not None, "带政策链接的截断拒绝页漏判了——残余量把链接算成了「别的话」"
        assert match.matched_host == "", "它应当由整句 boilerplate 档接住"

    def test_residual_budget_is_a_real_ceiling(self) -> None:
        """残余量上限的**取值**本身要被钉住

        上面那条用例是按现算样本写的，把常量改成 100000 它照样绿。
        真正要断言的是这个数还是个天花板：能容下渠道的短前缀，容不下一个中文小句。
        """
        assert 8 <= MAX_RESIDUAL_CHARS <= 40

    def test_script_mismatch_gate_is_load_bearing(self) -> None:
        """语种错位闸门：纯 ASCII 的 boilerplate 被中日韩字包着 = 有人在引用

        这条样本的残余量很小（只有 7 个中文字），大到能被残余量闸门拦下的余地并不多，
        所以它是**独立**于上一条的一道墙。
        """
        quoted = "上一轮的错误是 The prompt could not be submitted"

        assert has_conversational_markers(quoted) is False, "这条不许靠口语闸门放行"
        assert detect_provider_refusal(quoted) is None, "英文 boilerplate 被中文包着，仍然被判成上游拒答"

    def test_boilerplate_groups_are_conjunctions_not_keywords(self) -> None:
        """整句 boilerplate 档里，多标志的组必须**全中**才算数"""
        for entry in PROVIDER_BOILERPLATE:
            for group in entry.groups:
                assert group, f"{entry.provider}: 空组会恒不满足"
                for marker in group:
                    assert marker == marker.lower(), f"{entry.provider}: {marker!r} 必须小写"
                    assert marker in entry.fragments, (
                        f"{entry.provider}: 标志串 {marker!r} 不在 fragments 里，"
                        f"残余量会把它自己算成「别的话」，这一档永远命中不了"
                    )

        # Azure 那组要求两个标志同时命中：任何一半单独出现都判不出东西
        assert is_provider_refusal_text("我这台机器人用的是 Azure OpenAI 的模型组") is False
        assert is_provider_refusal_text("content management policy 这个词该怎么翻译") is False


# ======================================================================================
# 六、仓库真实语料全扫：误杀 0
# ======================================================================================

_REPO_ROOT = Path(__file__).resolve().parents[1]

# 「这条字面量出自小樱之口 / 出自本项目的输出协议」的标记。命中其中任何一个，
# 这条字面量就**绝不允许**被判成上游拒答。
_PERSONA_EVIDENCE = (
    "send_msg_text(",
    "send_msg_file(",
    "[@id:",
    "前辈",
    "QwQ",
    "呀",
    "啦",
    "哦",
    "嘛",
    "呢",
)


def _harvest_string_literals() -> List[Tuple[str, str]]:
    """把 `nekro_agent/` 与 `tests/` 下每一条字符串字面量抓出来

    抓的是**这个项目里真实写过的文本**：提示词模板、插件说明、用例夹具里的模型回复样本、
    错误文案。它比任何手造语料都更能代表判据将来会遇到什么。
    """
    harvested: List[Tuple[str, str]] = []
    for directory in ("nekro_agent", "tests"):
        for path in sorted((_REPO_ROOT / directory).rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"))
            except (SyntaxError, UnicodeDecodeError):  # pragma: no cover - 仓库里不该有
                continue
            origin = f"{path.relative_to(_REPO_ROOT).as_posix()}"
            for node in ast.walk(tree):
                if isinstance(node, ast.Constant) and isinstance(node.value, str):
                    text = node.value
                    if 4 <= len(text.strip()) <= 4000:
                        harvested.append((f"{origin}:{node.lineno}", text))
    return harvested


class TestRepoCorpusSweep:
    """把判据拿去跑仓库里**每一条**字符串字面量。

    改造前 `run_agent` 那份在这份语料上误杀 14 条（裸链接、裸政策名、沙盒 traceback、
    模型在代码里引用那句话……）。
    """

    def test_the_sweep_actually_finds_a_corpus(self) -> None:
        """扫描器自检：抓不到东西的话，下面那条断言是空的"""
        corpus = _harvest_string_literals()

        assert len(corpus) >= 5000, f"只抓到 {len(corpus)} 条字面量，扫描器大概坏了"
        assert any("抱歉" in text for _origin, text in corpus), "语料里连一条中文道歉都没有，不像这个项目"

    def test_no_persona_literal_in_the_repo_is_judged_a_refusal(self) -> None:
        """仓库里凡是带人设/协议痕迹的字面量，一条都不许被判成上游拒答"""
        offenders = [
            (origin, text)
            for origin, text in _harvest_string_literals()
            if any(marker in text for marker in _PERSONA_EVIDENCE) and is_provider_refusal_text(text)
        ]

        assert not offenders, "仓库真实语料里出现误杀：\n" + "\n".join(
            f"  {origin}: {text[:100]!r}" for origin, text in offenders[:10]
        )

    def test_every_repo_hit_is_provider_boilerplate(self) -> None:
        """反过来看命中面：扫出来的每一条都必须真的是供应商 boilerplate

        断言写成「命中的必须含已知 boilerplate 片段」而不是「命中的必须是这几行」——
        后者一加夹具就红，是维护负担而不是保护。
        """
        known_fragments = {fragment for entry in PROVIDER_BOILERPLATE for fragment in entry.fragments}
        known_fragments |= {"policies.google.com", "ai.google.dev"}

        for origin, text in _harvest_string_literals():
            if not is_provider_refusal_text(text):
                continue
            lowered = " ".join(text.lower().split())
            assert any(fragment in lowered for fragment in known_fragments), (
                f"{origin} 被判成上游拒答，但它不含任何已知供应商 boilerplate 片段：{text[:120]!r}"
            )
