"""图片卡片的排版与取数回归测试

卡片本身是像素，但决定它对不对的全是**不看图也能判定**的规则：

1. **字形回退** —— 昵称是用户可控内容。任何一个字符在字体链里找不到字形，都必须被丢掉
   而不是画成豆腐块；豆腐块不会报错、不会降级，是这类功能最典型的线上事故形态。
2. **按像素折行与截断** —— 一个几百字的昵称、一段没有空格的长串，都不允许把卡片撑爆。
   折行还要认得"中文可以逐字断、英文单词不能从中间劈开"。
3. **分列与截断规则** —— 谁排第几、第几个人落在第几列第几行、超过容量之后砍谁，
   是用户一眼就会核对的东西，必须稳定且与人数无关地成立。
4. **展示刻度** —— 榜上的一位小数是 `内部分数 / 2` 的结果。这层换算一旦飘了，
   要么满分不再是 100.0，要么负分被悄悄吃掉，而内部数值语义必须原样不动。
5. **排序范围** —— 好感度按 `(域, 人, 人设)` 隔离，榜单的范围必须逐维锁死在
   「当前会话所在的域 + 当前人设」。漏掉域就把另一个实例/租户的好感排进这个群，
   漏掉人设就把另一个人格下的关系抖出来——两者都是**信息泄露**，不会报错、不会降级，
   在生产上几乎不可能被发现。上榜单位还必须是**人**：合并过的账号只占一行。
6. **心声不打扰主链路** —— 触发判定的四道闸门合起来决定用户感受到的频率，
   而"生成失败就不发"意味着这套判定是唯一需要反复确认的逻辑。

所有排版用例都跑在一个固定字宽的假字体上，因此结果与机器上装了什么字体无关。
"""

import io
from typing import Any, Dict, List, Optional, Sequence, Tuple

import pytest
from PIL import Image
from tortoise import Tortoise

from nekro_agent.core.config import config as core_config
from nekro_agent.models.db_affection import DBAffection
from nekro_agent.models.db_chat_channel import DBChatChannel
from nekro_agent.models.db_chat_message import DBChatMessage
from nekro_agent.models.db_identity import DBIdentityAlias
from nekro_agent.models.db_instance_group import DBInstanceDomainBinding, DBInstanceGroup
from nekro_agent.models.db_preset import GLOBAL_PRESET_OWNER, DBPreset
from nekro_agent.schemas.chat_message import ChatType
from nekro_agent.services.affection.key import AffectionKey
from nekro_agent.services.affection.merge import merge_accounts, unmerge_account
from nekro_agent.services.command.schemas import (
    CommandExecutionContext,
    CommandResponseStatus,
)
from nekro_agent.services.identity_scope import PRESET_NONE, ScopeDomain
from nekro_agent.services.imagecard import affection_rank as rank_module
from nekro_agent.services.imagecard import inner_voice as voice_module
from nekro_agent.services.imagecard.affection_rank import (
    MAX_COLUMNS,
    MIN_ROWS_PER_COLUMN,
    RankCollection,
    RankEntry,
    build_rank_view,
    collect_rank_entries,
    display_score,
    format_score,
    plan_columns,
)
from nekro_agent.services.imagecard.fonts import LayeredFont
from nekro_agent.services.imagecard.inner_voice import (
    MAX_BODY_LENGTH,
    layout_inner_voice,
    prepare_body_text,
    render_inner_voice_card,
)
from nekro_agent.services.imagecard.painter import ELLIPSIS, ellipsize, wrap_text

ADAPTER_KEY = "onebot_v11"
INSTANCE_A = "ob10001"
INSTANCE_B = "ob10002"
CHAT_KEY_A = f"onebot_v11-{INSTANCE_A}-group_777"
CHAT_KEY_B = f"onebot_v11-{INSTANCE_A}-group_888"

# 假字体的字宽。取 10 是为了让"多少像素能放几个字"在用例里一眼可算
CHAR_WIDTH = 10.0


class _FakeFace:
    """字体面替身

    排版只用到两件事：一段文本有多宽、字体的升降部是多少。用等宽替身把这两者钉死，
    用例就不再依赖运行机器上恰好装了哪个字体、那个字体的字宽又是多少。
    """

    def __init__(self, name: str) -> None:
        self.name = name

    def getlength(self, text: str) -> float:
        return CHAR_WIDTH * len(text)

    def getmetrics(self) -> Tuple[int, int]:
        return 16, 4


def make_font(*layers: Sequence[str]) -> LayeredFont:
    """按每层覆盖的字符集构造一条字体链

    Args:
        *layers: 每个参数是该层能画出来的字符；靠前的层优先
    """
    return LayeredFont(
        [
            (_FakeFace(f"layer{index}"), frozenset(ord(char) for char in chars), index > 0)  # pyright: ignore[reportArgumentType]
            for index, chars in enumerate(layers)
        ],
        size=10,
    )


# 覆盖常见 ASCII 与用例里用到的汉字的"主字体"
_MAIN_CHARS = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 ,.-你好世界心声测试昵称超长的用户群甲乙丙丁" + ELLIPSIS
_EMOJI_CHARS = "🌸😀"


@pytest.fixture
def font() -> LayeredFont:
    return make_font(_MAIN_CHARS, _EMOJI_CHARS)


class TestGlyphFallback:
    """字形回退：画不出来的字符必须消失，而不是变成豆腐块"""

    def test_uncovered_characters_are_dropped(self, font: LayeredFont) -> None:
        # ﷽ (U+FDFD) 与 𠀀 (U+20000) 都不在任何一层里
        assert font.renderable("你好﷽世界\U00020000") == "你好世界"

    def test_emoji_falls_back_to_second_layer(self, font: LayeredFont) -> None:
        runs = font.split("你🌸好")
        assert [run.text for run in runs] == ["你", "🌸", "好"]
        # 第二层被标记为彩色字体，绘制时要开 embedded_color
        assert [run.embedded_color for run in runs] == [False, True, False]

    def test_adjacent_same_layer_characters_merge_into_one_run(self, font: LayeredFont) -> None:
        assert [run.text for run in font.split("你好世界")] == ["你好世界"]

    def test_all_uncovered_text_becomes_empty(self, font: LayeredFont) -> None:
        assert font.renderable("﷽﷽") == ""

    def test_space_survives_even_without_a_cmap_entry(self) -> None:
        # 空白不产生字形，丢掉只会把"你 好"粘成"你好"
        assert make_font("你好").renderable("你 好") == "你 好"

    def test_measure_ignores_dropped_characters(self, font: LayeredFont) -> None:
        assert font.measure("你﷽好") == 2 * CHAR_WIDTH


class TestWrapping:
    """按像素折行"""

    def test_chinese_breaks_between_any_two_characters(self, font: LayeredFont) -> None:
        assert wrap_text("你好世界你好世界", font, 4 * CHAR_WIDTH) == ["你好世界", "你好世界"]

    def test_ascii_word_is_not_split_in_the_middle(self, font: LayeredFont) -> None:
        # "hello" 放不进剩余空间时整体换行，而不是断成 "hel" / "lo"
        assert wrap_text("ab hello", font, 5 * CHAR_WIDTH) == ["ab ", "hello"]

    def test_explicit_newlines_are_preserved(self, font: LayeredFont) -> None:
        assert wrap_text("你好\n世界", font, 10 * CHAR_WIDTH) == ["你好", "世界"]

    def test_unbreakable_run_is_hard_split(self, font: LayeredFont) -> None:
        assert wrap_text("abcdefgh", font, 3 * CHAR_WIDTH) == ["abc", "def", "gh"]

    def test_max_lines_truncates_with_ellipsis(self, font: LayeredFont) -> None:
        lines = wrap_text("你好世界你好世界你好世界", font, 4 * CHAR_WIDTH, max_lines=2)
        assert len(lines) == 2
        assert lines[0] == "你好世界"
        assert lines[1].endswith(ELLIPSIS)

    def test_zero_width_yields_nothing(self, font: LayeredFont) -> None:
        assert wrap_text("你好", font, 0) == []

    def test_trailing_punctuation_stays_on_the_previous_line(self, font: LayeredFont) -> None:
        # 逗号不允许出现在行首
        assert wrap_text("你好世界, 你好", font, 4 * CHAR_WIDTH)[0] == "你好世界,"


class TestEllipsize:
    """单行截断"""

    @pytest.mark.parametrize(
        ("text", "budget_chars", "expected"),
        [
            ("你好", 5, "你好"),
            ("你好", 2, "你好"),
            ("你好世界", 3, "你好" + ELLIPSIS),
            ("你好世界", 1, ELLIPSIS),
            ("你好世界", 0, ""),
        ],
    )
    def test_table(self, font: LayeredFont, text: str, budget_chars: int, expected: str) -> None:
        assert ellipsize(text, font, budget_chars * CHAR_WIDTH) == expected

    def test_result_never_exceeds_the_budget(self, font: LayeredFont) -> None:
        budget = 7 * CHAR_WIDTH
        assert font.measure(ellipsize("超长的昵称超长的昵称", font, budget)) <= budget


class TestDisplayScore:
    """展示刻度：内部分数除以 2，保留一位小数"""

    @pytest.mark.parametrize(
        ("score", "expected"),
        [
            (200, "100.0"),  # 内部上限对应满分
            (143, "71.5"),  # 奇数分自然落在 .5 上
            (100, "50.0"),
            (1, "0.5"),
            (0, "0.0"),
            (-1, "-0.5"),
            (-20, "-10.0"),  # 负分照常展示，不截断也不取绝对值
            (-100, "-50.0"),  # 内部下限
        ],
    )
    def test_format_table(self, score: int, expected: str) -> None:
        assert format_score(score) == expected

    def test_mapping_is_monotonic_and_pure(self) -> None:
        scores = [-100, -1, 0, 1, 200]
        assert [display_score(value) for value in scores] == sorted(display_score(value) for value in scores)
        # 换算不回写任何内部状态
        assert display_score(143) == 71.5


class TestColumnPlan:
    """分列规则"""

    @pytest.mark.parametrize(
        ("count", "expected"),
        [
            (0, (0, 0)),
            (1, (1, 1)),
            (8, (1, 8)),  # 每列至少 MIN_ROWS_PER_COLUMN 行，8 个人不摊成两列
            (9, (2, 5)),
            (48, (6, 8)),
            (200, (6, 34)),  # 满载：列数封顶，行数随人数增长
        ],
    )
    def test_table(self, count: int, expected: Tuple[int, int]) -> None:
        assert plan_columns(count) == expected

    def test_columns_never_exceed_the_cap(self) -> None:
        for count in (1, 7, 49, 137, 200):
            columns, rows = plan_columns(count)
            assert columns <= MAX_COLUMNS
            assert columns * rows >= count

    def test_short_lists_keep_columns_dense(self) -> None:
        # 5 个人摊成 5 列各 1 行会看起来像坏掉了
        columns, _ = plan_columns(MIN_ROWS_PER_COLUMN - 3)
        assert columns == 1


def entry(person_uid: str, score: int = 0, nickname: str = "甲") -> RankEntry:
    return RankEntry(person_uid=person_uid, nickname=nickname, score=score)


class TestRankView:
    """榜单的排序、截断与昵称清洗"""

    def test_sorted_by_score_descending(self) -> None:
        view = build_rank_view([entry("1", 3), entry("2", 90), entry("3", -40)], total_active=3)
        assert [row.score_text for row in view.rows] == ["45.0", "1.5", "-20.0"]

    def test_ties_break_by_userid_for_stable_ordering(self) -> None:
        # 同分时不定序会让同一份数据两次渲染出不同的行序
        view = build_rank_view([entry("30"), entry("10"), entry("20")], total_active=3)
        assert [row.suffix for row in view.rows] == ["10", "20", "30"]

        view_reordered = build_rank_view([entry("20"), entry("30"), entry("10")], total_active=3)
        assert [row.suffix for row in view.rows] == [row.suffix for row in view_reordered.rows]

    def test_truncation_keeps_the_highest_scores(self) -> None:
        entries = [entry(str(1000 + index), score=index) for index in range(30)]
        view = build_rank_view(entries, total_active=30, max_entries=10)
        assert view.truncated is True
        assert view.total_ranked == 30
        assert len(view.rows) == 10
        assert view.rows[0].score_text == format_score(29)
        assert view.rows[-1].score_text == format_score(20)

    def test_not_truncated_when_within_capacity(self) -> None:
        view = build_rank_view([entry("1"), entry("2")], total_active=2, max_entries=10)
        assert view.truncated is False

    def test_total_active_may_exceed_ranked_count(self) -> None:
        # 活跃但还没有好感度记录的人计入人数、不上榜
        view = build_rank_view([entry("1")], total_active=227)
        assert view.total_active == 227
        assert len(view.rows) == 1

    def test_column_major_placement_puts_the_top_score_first(self) -> None:
        entries = [entry(str(100 + index), score=100 - index) for index in range(20)]
        view = build_rank_view(entries, total_active=20)
        columns, rows_per_column = plan_columns(20)
        assert (view.columns, view.rows_per_column) == (columns, rows_per_column)
        # 第一列自上而下是第 1..rows_per_column 名，第二列接着往下
        first_of_second_column = view.rows[rows_per_column]
        assert first_of_second_column.score_text == format_score(100 - rows_per_column)

    def test_highlight_marks_only_the_requesting_user(self) -> None:
        view = build_rank_view([entry("11"), entry("22")], total_active=2, highlight_person_uid="22")
        assert [row.highlighted for row in view.rows] == [False, True]

    def test_no_highlight_when_requester_is_unknown(self) -> None:
        view = build_rank_view([entry("11"), entry("22")], total_active=2, highlight_person_uid="")
        assert not any(row.highlighted for row in view.rows)

    def test_control_characters_are_stripped_from_nicknames(self) -> None:
        view = build_rank_view([entry("1", 0, "甲\n乙\r\x00丙")], total_active=1)
        # 换行折成空格、NUL 直接消失：带换行的昵称能在别处伪造出一整行内容
        assert view.rows[0].display_name == "甲 乙 丙"

    def test_blank_nickname_falls_back_to_userid(self) -> None:
        view = build_rank_view([entry("998877", 0, "   ")], total_active=1)
        assert view.rows[0].display_name == "998877"

    def test_overlong_nickname_is_capped(self) -> None:
        view = build_rank_view([entry("1", 0, "超" * 500)], total_active=1)
        assert len(view.rows[0].display_name) <= rank_module.MAX_NICKNAME_LENGTH + 1

    def test_suffix_is_the_userid_tail(self) -> None:
        view = build_rank_view([entry("2000000002", 0)], total_active=1)
        assert view.rows[0].suffix == "0002"

    def test_short_userid_keeps_all_of_it(self) -> None:
        view = build_rank_view([entry("42", 0)], total_active=1)
        assert view.rows[0].suffix == "42"

    def test_empty_input_yields_an_empty_view(self) -> None:
        view = build_rank_view([], total_active=0)
        assert view.rows == ()
        assert (view.columns, view.rows_per_column) == (0, 0)


class TestInnerVoiceText:
    """心声正文的清洗"""

    def test_control_characters_are_removed_but_paragraphs_survive(self) -> None:
        assert prepare_body_text("第一段\x00\n\n\n\n第二段") == "第一段\n\n第二段"

    def test_overlong_body_is_capped(self) -> None:
        assert len(prepare_body_text("心" * 1000)) == MAX_BODY_LENGTH + 1

    def test_whitespace_only_body_becomes_empty(self) -> None:
        # 调用方据此放弃发送，而不是发一张空卡片出去
        assert prepare_body_text("  \n\t ") == ""


class TestInnerVoiceLayout:
    """心声卡片的尺寸推导"""

    @pytest.fixture(autouse=True)
    def _stub_fonts(self, monkeypatch: pytest.MonkeyPatch, font: LayeredFont) -> None:
        monkeypatch.setattr(voice_module, "load_font", lambda _role, _size: font)

    def test_empty_body_is_refused(self) -> None:
        with pytest.raises(ValueError, match="心声正文为空"):
            layout_inner_voice("   ")

    def test_unrenderable_body_is_refused(self) -> None:
        # 整段都画不出来时，卡片会是一片空白，不如不发
        with pytest.raises(ValueError, match="无可渲染字符"):
            layout_inner_voice("﷽﷽")

    def test_card_grows_taller_with_more_lines(self) -> None:
        short = layout_inner_voice("你好")
        long = layout_inner_voice("你好世界" * 40)
        assert len(long.lines) > len(short.lines)
        assert long.height > short.height
        assert long.width == short.width

    def test_line_count_is_capped(self) -> None:
        layout = layout_inner_voice("你好世界" * 200)
        assert len(layout.lines) <= voice_module.MAX_BODY_LINES
        assert layout.lines[-1].endswith(ELLIPSIS)

    def test_body_box_stays_inside_the_card(self) -> None:
        layout = layout_inner_voice("你好世界" * 10)
        assert layout.body_top + layout.body_height < layout.height


class TestRenderSizeContract:
    """渲染输出尺寸 == layout_inner_voice(scale) 推导的对外契约

    c5494ec 修的翻车现场：排版曾被按超采样画布的 render_scale 推导，字体微调
    会让折行与 layout 差出一行。这里把「先用 layout 占位、再等图」的契约
    钉死——谁再改错推导基准，这几条会先红。
    契约测试必须走真渲染（stub 字体没有 PIL 的 getmask2，画不了），
    环境缺中文字体时显式 skip，而不是换假路径绕过契约本身。
    """

    @staticmethod
    def _render(body: str, *, scale: int = 2, **kwargs: Any) -> Tuple[bytes, Any]:
        try:
            png = render_inner_voice_card(body, scale=scale, **kwargs)
        except RuntimeError as e:
            pytest.skip(f"环境无中文字体，跳过真渲染契约：{e}")
        img = Image.open(io.BytesIO(png))
        layout = layout_inner_voice(body, scale=scale)
        return img, layout

    @pytest.mark.parametrize("scale", [1, 2, 3])
    def test_output_size_matches_layout_per_scale(self, scale: int) -> None:
        body = "今天天气真好哦，一起去大冒险吧" * 2
        img, layout = self._render(body, scale=scale)
        assert img.size == (layout.width, layout.height)

    def test_contract_holds_across_body_lengths(self) -> None:
        # 折行边界随正文长度移动，正是当初差一行的触发条件
        for body in ("短句", "中等长度的句子，看看折行。" * 4, "非常长的正文灌进去" * 20):
            img, layout = self._render(body, scale=2)
            assert img.size == (layout.width, layout.height)

    def test_emotion_and_heart_rate_do_not_break_the_contract(self) -> None:
        body = "心率也会画上去，但画布尺寸不归它管"
        for emotion, heart_rate in (("calm", None), ("agitated", 120)):
            img, layout = self._render(body, scale=2, emotion=emotion, heart_rate=heart_rate)
            assert img.size == (layout.width, layout.height)


class TestTriggerGate:
    """心声的触发闸门"""

    @staticmethod
    def gate(**overrides: Any) -> bool:
        from plugins.builtin.inner_voice import should_trigger

        params: Dict[str, Any] = {
            "enabled": True,
            "probability": 0.5,
            "min_interval": 3600,
            "allow_private": False,
            "is_group": True,
            "roll": 0.1,
            "now": 10_000.0,
            "last_sent_at": None,
            "in_flight": False,
        }
        params.update(overrides)
        return should_trigger(**params)

    def test_fires_when_every_gate_is_open(self) -> None:
        assert self.gate() is True

    def test_disabled_never_fires(self) -> None:
        assert self.gate(enabled=False) is False

    def test_roll_at_or_above_probability_does_not_fire(self) -> None:
        assert self.gate(roll=0.5) is False
        assert self.gate(roll=0.49999) is True

    def test_zero_probability_never_fires(self) -> None:
        # 概率为 0 时不允许因为 roll 恰好是 0.0 而漏出去
        assert self.gate(probability=0.0, roll=0.0) is False

    def test_private_chat_requires_the_opt_in(self) -> None:
        assert self.gate(is_group=False) is False
        assert self.gate(is_group=False, allow_private=True) is True

    def test_in_flight_generation_blocks_a_second_one(self) -> None:
        assert self.gate(in_flight=True) is False

    def test_cooldown_blocks_until_the_interval_elapses(self) -> None:
        assert self.gate(last_sent_at=10_000.0 - 3599) is False
        assert self.gate(last_sent_at=10_000.0 - 3600) is True


@pytest.fixture
async def rank_db():
    """内存 SQLite 上的真实表结构

    榜单取数横跨消息表、好感度表、别名表与实例域绑定表，隔离性是**数据层**的性质，
    用假存储验证等于把要保护的东西换掉了，所以这里跑真表。

    注册整个 `nekro_agent.models` 而不是被测的那两张表：解析器要查别名与域绑定，
    只注册一部分会让"忘了在 `models/__init__.py` 里导出"这类漏永远测不出来
    （判据与 `tests/test_identity_scope_foundation.py` 的固件同源）。
    """
    await Tortoise.init(db_url="sqlite://:memory:", modules={"models": ["nekro_agent.models"]})
    await Tortoise.generate_schemas()
    try:
        yield
    finally:
        await Tortoise.close_connections()


NOW = 4_000_000_000.0

# 两个人设档：换人设 = 换一份好感，两份互不可见
PRESET_JIA = 7
PRESET_YI = 9

# 一个人的两个账号（换了 QQ 号），靠别名指向同一个人
USER_MAIN = "10001"
USER_ALT = "20002"


class _StubChannel:
    """频道替身（结构上满足 `rank_module.RankChannel`）

    榜单取数只要四样东西：从哪个会话取成员（`chat_key`）、域从哪来
    （`adapter_key` / `instance_key`）、当前人设是哪一个。用替身而不是真 `DBChatChannel`：
    后者会把插件收集器与适配器注册表整条导入链拖进来，而人设那条回退链
    （频道 → 实例默认 → 系统默认 → 内置）由 `test_identity_scope_foundation.py` 钉住，
    这里只关心"解析出来的那个值有没有真的进到查询条件里"。
    """

    def __init__(
        self,
        *,
        chat_key: str = CHAT_KEY_A,
        instance_key: str = INSTANCE_A,
        preset_id: int = PRESET_NONE,
        adapter_key: str = ADAPTER_KEY,
    ) -> None:
        self.chat_key = chat_key
        self.adapter_key = adapter_key
        self.instance_key = instance_key
        self._preset_id = preset_id

    async def get_effective_preset_id(self) -> int:
        return self._preset_id


async def add_message(
    chat_key: str,
    platform_userid: str,
    *,
    nickname: str = "甲",
    sender_id: Optional[str] = None,
    timestamp: Optional[int] = None,
) -> None:
    await DBChatMessage.create(
        sender_id=sender_id if sender_id is not None else platform_userid,
        sender_name=nickname,
        sender_nickname=nickname,
        is_tome=0,
        is_recalled=False,
        adapter_key=ADAPTER_KEY,
        instance_key=INSTANCE_A,
        message_id=f"m{platform_userid}{timestamp or int(NOW)}",
        chat_key=chat_key,
        chat_type="group",
        platform_userid=platform_userid,
        content_text="hi",
        content_data="[]",
        raw_cq_code="",
        ext_data="{}",
        send_timestamp=timestamp if timestamp is not None else int(NOW),
    )


async def add_legacy_affection(instance_key: str, platform_userid: str, score: int, nickname: str = "") -> None:
    """按**升级前那套**写一行好感：只有三列，两个新列取数据库默认值

    零配置回归的锚点。这样写进去的行落在"自成一域 / 人 = 自己 / 无人设档"，
    必须能被今天的榜单原样查到——查不到就等于测试部署上的存量数据凭空消失。
    """
    await DBAffection.create(
        adapter_key=ADAPTER_KEY,
        instance_key=instance_key,
        platform_userid=platform_userid,
        nickname=nickname,
        score=score,
    )


async def add_affection(
    person_uid: str,
    score: int,
    *,
    domain: Optional[ScopeDomain] = None,
    instance_key: str = INSTANCE_A,
    preset_id: int = PRESET_NONE,
    nickname: str = "",
) -> None:
    """在指定的 (域, 人, 人设) 上写一行好感

    域三列走 `ScopeDomain.as_columns()`——这正是写侧建行用的那一份，
    用例因此不会因为自己拼错了列而假绿。
    """
    columns = (domain or ScopeDomain.of_instance(ADAPTER_KEY, instance_key)).as_columns()
    await DBAffection.create(
        **columns,
        platform_userid=person_uid,
        preset_id=preset_id,
        nickname=nickname,
        score=score,
    )


async def add_alias(domain: ScopeDomain, alias_account: str, person_uid: str) -> None:
    """把一个账号并进某个人（换号场景），只加一行指向、不搬任何数据行"""
    await DBIdentityAlias.create(
        domain_id=domain.domain_id,
        platform_userid=alias_account,
        person_uid=person_uid,
    )


async def collect(
    channel: Optional[_StubChannel] = None,
    *,
    active_days: int = 30,
    highlight_userid: str = "",
) -> RankCollection:
    return await collect_rank_entries(
        channel=channel or _StubChannel(),
        active_days=active_days,
        highlight_userid=highlight_userid,
        now=NOW,
    )


def ranked(collection: RankCollection) -> List[Tuple[str, int]]:
    """榜单内容的可比较形式：`(人, 分数)`，按人排序消除查询返回序的影响"""
    return sorted((item.person_uid, item.score) for item in collection.entries)


class _StubCtx:
    """沙盒上下文替身：只提供插件真正用到的那几样

    `from_chat_key` 是**归属**的来源（`_assert_own_channel` 判的就是它），
    `fs.mixed_forward_file` 与 `ms.send_*` 各自记账，用来断言"被拒时一个字节都没流出去"。
    """

    def __init__(self, from_chat_key: Optional[str]) -> None:
        self.from_chat_key = from_chat_key
        self.fs = self
        self.ms = self
        self.forwarded: List[str] = []
        self.sent: List[Tuple[str, str]] = []

    async def mixed_forward_file(self, file: object, file_name: str = "") -> str:
        self.forwarded.append(file_name)
        return "/sandbox/card.png"

    async def send_image(self, chat_key: str, file_path: str, _ctx: object) -> None:
        self.sent.append((chat_key, file_path))

    async def send_text(self, chat_key: str, message: str, _ctx: object) -> None:
        self.sent.append((chat_key, message))


def _capture_render(monkeypatch: pytest.MonkeyPatch) -> List[Any]:
    """截下交给绘制函数的那个 `RankView`，并跳过真正的像素绘制

    换掉的只有"把视图画成 PNG"这一步——它是唯一需要字体、也是唯一与本用例无关的部分；
    取数、解析、排序、高亮判定全都照常真跑。字体一并按"有"处理，
    否则这台机器上装没装中文字体会决定用例走哪条分支。
    """
    from plugins.builtin import affection_rank as rank_plugin

    captured: List[Any] = []

    def _fake_render(view: Any, *, subtitle: str, **_kwargs: Any) -> bytes:
        captured.append(view)
        return b"png"

    monkeypatch.setattr(rank_plugin, "fonts_available", lambda: True)
    monkeypatch.setattr(rank_plugin, "render_rank_card", _fake_render)
    return captured


@pytest.mark.usefixtures("rank_db")
class TestCollectRankEntries:
    """榜单取数：谁上榜、谁只计人数"""

    async def test_only_members_with_records_are_ranked_but_all_are_counted(self) -> None:
        await add_message(CHAT_KEY_A, "1")
        await add_message(CHAT_KEY_A, "2")
        await add_message(CHAT_KEY_A, "3")
        await add_affection("1", 40)

        collection = await collect()
        assert collection.total_active == 3
        assert ranked(collection) == [("1", 40)]

    async def test_other_channels_do_not_leak_members(self) -> None:
        await add_message(CHAT_KEY_A, "1")
        await add_message(CHAT_KEY_B, "2")
        await add_affection("1", 10)
        await add_affection("2", 20)

        collection = await collect(_StubChannel(chat_key=CHAT_KEY_A))
        assert collection.total_active == 1
        assert ranked(collection) == [("1", 10)]

    async def test_bot_messages_are_excluded(self) -> None:
        await add_message(CHAT_KEY_A, "1")
        await add_message(CHAT_KEY_A, "0", sender_id="-1", nickname="机器人")
        await add_affection("1", 10)
        await add_affection("0", 199)

        collection = await collect()
        assert collection.total_active == 1
        assert ranked(collection) == [("1", 10)]

    async def test_members_outside_the_active_window_are_dropped(self) -> None:
        await add_message(CHAT_KEY_A, "1", timestamp=int(NOW) - 29 * 86400)
        await add_message(CHAT_KEY_A, "2", timestamp=int(NOW) - 31 * 86400)
        await add_affection("1", 10)
        await add_affection("2", 20)

        collection = await collect(active_days=30)
        assert collection.total_active == 1
        assert ranked(collection) == [("1", 10)]

    async def test_latest_nickname_from_messages_wins(self) -> None:
        # 好感度记录里的昵称可能是很久以前那次互动留下的
        await add_message(CHAT_KEY_A, "1", nickname="旧名", timestamp=int(NOW) - 100)
        await add_message(CHAT_KEY_A, "1", nickname="新名", timestamp=int(NOW))
        await add_affection("1", 10, nickname="好感度里的旧名")

        collection = await collect()
        assert [item.nickname for item in collection.entries] == ["新名"]

    async def test_empty_channel_yields_nothing_but_still_reports_a_scope(self) -> None:
        collection = await collect()
        assert (collection.entries, collection.total_active) == ((), 0)
        # 空群也要有确定的排序范围可报：调用方（后台、日志、将来的副标题）不必区分空与非空
        assert collection.scope.affection_domain == ScopeDomain.of_instance(ADAPTER_KEY, INSTANCE_A)
        assert collection.scope.preset_id == PRESET_NONE


@pytest.mark.usefixtures("rank_db")
class TestRankScopeIsTheCurrentDomainAndPreset:
    """排序范围锁死在「当前域 + 当前人设」

    跨域或跨人设排名是**信息泄露**而不是"显示得不太准"：群 A 的人不该出现在群 B 的榜上，
    更不该因为运营换了人设就把另一个人格下的亲疏关系抖出来。两者都不会报错、不会降级，
    因此只能靠用例钉死。
    """

    async def test_another_instance_in_its_own_domain_is_invisible(self) -> None:
        # 默认每个实例自成一域：漏掉域维就会把另一个 Bot 实例的好感度排进这个群
        await add_message(CHAT_KEY_A, "1")
        await add_affection("1", 40, instance_key=INSTANCE_A)
        await add_affection("1", 190, instance_key=INSTANCE_B)

        assert ranked(await collect(_StubChannel(instance_key=INSTANCE_A))) == [("1", 40)]
        assert ranked(await collect(_StubChannel(instance_key=INSTANCE_B))) == [("1", 190)]

    async def test_another_adapter_with_the_same_instance_key_is_invisible(self) -> None:
        # 域的身份是 (适配器, 实例) 二元组，不是实例标识一段
        await add_message(CHAT_KEY_A, "1")
        await add_affection("1", 40, domain=ScopeDomain.of_instance(ADAPTER_KEY, INSTANCE_A))
        await add_affection("1", 190, domain=ScopeDomain.of_instance("minecraft", INSTANCE_A))

        assert ranked(await collect()) == [("1", 40)]

    async def test_another_preset_is_never_ranked(self) -> None:
        # 同一个人在人设甲下 40 分、人设乙下 190 分，两份互不可见
        await add_message(CHAT_KEY_A, "1")
        await add_affection("1", 40, preset_id=PRESET_JIA)
        await add_affection("1", 190, preset_id=PRESET_YI)

        assert ranked(await collect(_StubChannel(preset_id=PRESET_JIA))) == [("1", 40)]
        assert ranked(await collect(_StubChannel(preset_id=PRESET_YI))) == [("1", 190)]

    async def test_switching_the_preset_switches_the_whole_board(self) -> None:
        """换人设的那一刻整张榜切到另一份（旧的那份仍在，换回来就回来）"""
        await add_message(CHAT_KEY_A, "1")
        await add_message(CHAT_KEY_A, "2")
        await add_affection("1", 40, preset_id=PRESET_JIA)
        await add_affection("2", 60, preset_id=PRESET_JIA)

        assert ranked(await collect(_StubChannel(preset_id=PRESET_JIA))) == [("1", 40), ("2", 60)]
        # 切到人设乙：这两个人在乙下面还没有关系，榜是空的，但活跃人数照旧
        after_switch = await collect(_StubChannel(preset_id=PRESET_YI))
        assert (ranked(after_switch), after_switch.total_active) == ([], 2)
        # 换回来，逐字回到原样
        assert ranked(await collect(_StubChannel(preset_id=PRESET_JIA))) == [("1", 40), ("2", 60)]

    async def test_the_no_preset_slot_is_a_preset_of_its_own(self) -> None:
        # 「无人设档」（回落到内置默认人设）是 0 号档，不是"任意人设"的通配
        await add_message(CHAT_KEY_A, "1")
        await add_affection("1", 40, preset_id=PRESET_NONE)
        await add_affection("1", 190, preset_id=PRESET_JIA)

        assert ranked(await collect(_StubChannel(preset_id=PRESET_NONE))) == [("1", 40)]

    async def test_instances_in_one_domain_share_a_board(self) -> None:
        """把几个实例放进同一个域 = 好感互通，两边看到的是同一张榜"""
        group = await DBInstanceGroup.create(name="family")
        for instance_key in (INSTANCE_A, INSTANCE_B):
            await DBInstanceDomainBinding.create(
                adapter_key=ADAPTER_KEY,
                instance_key=instance_key,
                memory_group_id=group.id,
            )
        await add_message(CHAT_KEY_A, "1")
        await add_affection("1", 88, domain=ScopeDomain.of_group(group.id))

        assert ranked(await collect(_StubChannel(instance_key=INSTANCE_A))) == [("1", 88)]
        assert ranked(await collect(_StubChannel(instance_key=INSTANCE_B))) == [("1", 88)]

    async def test_a_group_domain_does_not_leak_into_an_unbound_instance(self) -> None:
        # 没被放进组的实例仍然自成一域——组里的好感不该进它的榜
        group = await DBInstanceGroup.create(name="family")
        await DBInstanceDomainBinding.create(
            adapter_key=ADAPTER_KEY,
            instance_key=INSTANCE_A,
            memory_group_id=group.id,
        )
        await add_message(CHAT_KEY_A, "1")
        await add_affection("1", 88, domain=ScopeDomain.of_group(group.id))

        assert ranked(await collect(_StubChannel(instance_key=INSTANCE_B))) == []

    async def test_the_affection_domain_override_wins(self) -> None:
        """好感域覆盖（前端不暴露）生效时，榜单跟着好感域走而不是记忆域"""
        memory_group = await DBInstanceGroup.create(name="记忆域")
        affection_group = await DBInstanceGroup.create(name="好感域")
        await DBInstanceDomainBinding.create(
            adapter_key=ADAPTER_KEY,
            instance_key=INSTANCE_A,
            memory_group_id=memory_group.id,
            affection_group_id=affection_group.id,
        )
        await add_message(CHAT_KEY_A, "1")
        await add_affection("1", 11, domain=ScopeDomain.of_group(memory_group.id))
        await add_affection("1", 99, domain=ScopeDomain.of_group(affection_group.id))

        assert ranked(await collect()) == [("1", 99)]

    async def test_the_scope_is_reported_for_display(self) -> None:
        group = await DBInstanceGroup.create(name="family")
        await DBInstanceDomainBinding.create(
            adapter_key=ADAPTER_KEY,
            instance_key=INSTANCE_A,
            memory_group_id=group.id,
        )
        await add_message(CHAT_KEY_A, "1")

        collection = await collect(_StubChannel(preset_id=PRESET_JIA))
        assert collection.scope.affection_domain.domain_id == f"g:{group.id}"
        assert collection.scope.preset_id == PRESET_JIA

    async def test_the_rank_filter_is_the_write_key_minus_the_person(self) -> None:
        """榜单的过滤条件必须**恰好**是"写侧的键去掉人那一维"

        差一列就是范围错：多一列查不到别人写的行（榜空了），少一列把别的域/人设的行排进来。
        键的定义在 `services/affection/key.py`，本条是**消费方对它的契约**——
        将来键上加一维却只加进 `as_columns()`、忘了 `scope_columns()`，
        榜单会当场少一维过滤，而这里先红。
        """
        await add_message(CHAT_KEY_A, "1")
        key = AffectionKey.of_scope((await collect(_StubChannel(preset_id=PRESET_JIA))).scope)
        assert set(key.as_columns()) - set(key.scope_columns()) == {"platform_userid"}
        assert set(key.scope_columns()) < set(key.as_columns())

    async def test_the_scope_takes_effect_on_the_next_call(self) -> None:
        """改域必须热生效：任何 TTL 缓存都会让这条红"""
        await add_message(CHAT_KEY_A, "1")
        await add_affection("1", 40, instance_key=INSTANCE_A)
        assert ranked(await collect()) == [("1", 40)]

        group = await DBInstanceGroup.create(name="family")
        await DBInstanceDomainBinding.create(
            adapter_key=ADAPTER_KEY,
            instance_key=INSTANCE_A,
            memory_group_id=group.id,
        )
        await add_affection("1", 88, domain=ScopeDomain.of_group(group.id))
        assert ranked(await collect()) == [("1", 88)]


@pytest.mark.usefixtures("rank_db")
class TestMergedAccountsAppearOnce:
    """榜上是「人」不是「账号」

    换号的人合并之后必须**只占一行**：两行同一个人是"这个群里有多少人"这类数字当场变假，
    而看到自己出现两次的用户会直接把它当成 Bug 报上来。
    """

    @staticmethod
    def _domain() -> ScopeDomain:
        return ScopeDomain.of_instance(ADAPTER_KEY, INSTANCE_A)

    async def test_two_accounts_of_one_person_yield_one_row(self) -> None:
        await add_message(CHAT_KEY_A, USER_MAIN, nickname="旧号", timestamp=int(NOW) - 100)
        await add_message(CHAT_KEY_A, USER_ALT, nickname="新号", timestamp=int(NOW))
        await add_alias(self._domain(), USER_ALT, USER_MAIN)
        await add_affection(USER_MAIN, 40)

        collection = await collect()
        assert ranked(collection) == [(USER_MAIN, 40)]
        # 活跃人数也按人算，否则一个人换了号就把群里的人数灌水成两个
        assert collection.total_active == 1

    async def test_the_persons_latest_nickname_wins_across_accounts(self) -> None:
        await add_message(CHAT_KEY_A, USER_MAIN, nickname="旧号", timestamp=int(NOW) - 100)
        await add_message(CHAT_KEY_A, USER_ALT, nickname="新号", timestamp=int(NOW))
        await add_alias(self._domain(), USER_ALT, USER_MAIN)
        await add_affection(USER_MAIN, 40, nickname="好感度里的旧名")

        assert [item.nickname for item in (await collect()).entries] == ["新号"]

    async def test_a_person_who_only_speaks_from_the_alt_still_ranks(self) -> None:
        """换号不能丢：主号一句话没说，这个人照样带着他的好感上榜"""
        await add_message(CHAT_KEY_A, USER_ALT, nickname="新号")
        await add_alias(self._domain(), USER_ALT, USER_MAIN)
        await add_affection(USER_MAIN, 40)

        assert ranked(await collect()) == [(USER_MAIN, 40)]

    async def test_the_alts_own_leftover_row_is_not_ranked_separately(self) -> None:
        # 合并只加一行指向、不搬数据，副号名下的旧行留在原地（并入语义由合并服务负责）；
        # 但它绝不能作为"另一个人"再上一次榜
        await add_message(CHAT_KEY_A, USER_ALT)
        await add_alias(self._domain(), USER_ALT, USER_MAIN)
        await add_affection(USER_MAIN, 40)
        await add_affection(USER_ALT, 10)

        collection = await collect()
        assert ranked(collection) == [(USER_MAIN, 40)]
        assert collection.total_active == 1

    async def test_unmerged_accounts_stay_two_people(self) -> None:
        await add_message(CHAT_KEY_A, USER_MAIN)
        await add_message(CHAT_KEY_A, USER_ALT)
        await add_affection(USER_MAIN, 40)
        await add_affection(USER_ALT, 10)

        collection = await collect()
        assert ranked(collection) == [(USER_MAIN, 40), (USER_ALT, 10)]
        assert collection.total_active == 2

    async def test_an_alias_declared_in_another_domain_does_not_merge_here(self) -> None:
        """别人域里的"这两个号是同一个人"绝不能改写本域的榜——那是跨域串号"""
        await add_message(CHAT_KEY_A, USER_MAIN)
        await add_message(CHAT_KEY_A, USER_ALT)
        await add_alias(ScopeDomain.of_instance(ADAPTER_KEY, INSTANCE_B), USER_ALT, USER_MAIN)
        await add_affection(USER_MAIN, 40)
        await add_affection(USER_ALT, 10)

        collection = await collect(_StubChannel(instance_key=INSTANCE_A))
        assert ranked(collection) == [(USER_MAIN, 40), (USER_ALT, 10)]
        assert collection.total_active == 2

    @staticmethod
    async def _split_domains() -> Tuple[ScopeDomain, ScopeDomain]:
        """把记忆域与好感域拆开，返回 `(记忆域, 好感域)`

        好感域覆盖字段前端不暴露，但它一旦被用上，"人"就必须在**好感域**里解析：
        别名是按域隔离的，拿记忆域的解析结果去查好感表就是跨域套用。
        """
        memory_group = await DBInstanceGroup.create(name="记忆域")
        affection_group = await DBInstanceGroup.create(name="好感域")
        await DBInstanceDomainBinding.create(
            adapter_key=ADAPTER_KEY,
            instance_key=INSTANCE_A,
            memory_group_id=memory_group.id,
            affection_group_id=affection_group.id,
        )
        return ScopeDomain.of_group(memory_group.id), ScopeDomain.of_group(affection_group.id)

    async def test_an_alias_in_the_memory_domain_does_not_merge_the_board(self) -> None:
        # 记忆那边认成同一个人，不代表好感这边也是——两个域是分开配的
        memory_domain, affection_domain = await self._split_domains()
        await add_message(CHAT_KEY_A, USER_MAIN)
        await add_message(CHAT_KEY_A, USER_ALT)
        await add_alias(memory_domain, USER_ALT, USER_MAIN)
        await add_affection(USER_MAIN, 40, domain=affection_domain)
        await add_affection(USER_ALT, 10, domain=affection_domain)

        collection = await collect()
        assert ranked(collection) == [(USER_MAIN, 40), (USER_ALT, 10)]
        assert collection.total_active == 2

    async def test_an_alias_in_the_affection_domain_merges_the_board(self) -> None:
        memory_domain, affection_domain = await self._split_domains()
        await add_message(CHAT_KEY_A, USER_MAIN)
        await add_message(CHAT_KEY_A, USER_ALT)
        await add_alias(affection_domain, USER_ALT, USER_MAIN)
        await add_affection(USER_MAIN, 40, domain=affection_domain)
        await add_affection(USER_ALT, 10, domain=affection_domain)

        collection = await collect(highlight_userid=USER_ALT)
        assert ranked(collection) == [(USER_MAIN, 40)]
        assert collection.total_active == 1
        # 高亮同样只认好感域里的那个人；记忆域里 USER_ALT 没有别名，套用它就会谁都不高亮
        assert collection.highlight_person_uid == USER_MAIN
        assert memory_domain != affection_domain

    async def test_highlight_follows_the_person_not_the_account(self) -> None:
        """用副号发的指令要高亮到自己那一行，不是谁都不亮"""
        await add_message(CHAT_KEY_A, USER_MAIN)
        await add_message(CHAT_KEY_A, "30003")
        await add_alias(self._domain(), USER_ALT, USER_MAIN)
        await add_affection(USER_MAIN, 40)
        await add_affection("30003", 60)

        collection = await collect(highlight_userid=USER_ALT)
        assert collection.highlight_person_uid == USER_MAIN

        view = build_rank_view(
            collection.entries,
            total_active=collection.total_active,
            highlight_person_uid=collection.highlight_person_uid,
        )
        assert [row.highlighted for row in view.rows] == [False, True]

    async def test_an_unknown_requester_highlights_nobody(self) -> None:
        await add_message(CHAT_KEY_A, USER_MAIN)
        await add_affection(USER_MAIN, 40)

        collection = await collect(highlight_userid="")
        assert collection.highlight_person_uid == ""
        view = build_rank_view(
            collection.entries,
            total_active=collection.total_active,
            highlight_person_uid=collection.highlight_person_uid,
        )
        assert not any(row.highlighted for row in view.rows)


@pytest.mark.usefixtures("rank_db")
class TestZeroConfigRankIsByteForByteToday:
    """未启用多租户、未配置域的部署上，榜单行为与今天逐字相同

    这是本轮最重要的一条回归保险：不配置域、没有别名、频道没绑人设时，取数必须退化成
    今天那条 `(adapter_key, instance_key, platform_userid)` 查询。
    """

    async def test_rows_written_the_old_way_are_still_ranked(self) -> None:
        await add_message(CHAT_KEY_A, "1")
        await add_message(CHAT_KEY_A, "2")
        await add_legacy_affection(INSTANCE_A, "1", 40)
        await add_legacy_affection(INSTANCE_A, "2", 60)

        collection = await collect()
        assert ranked(collection) == [("1", 40), ("2", 60)]
        assert collection.total_active == 2

    async def test_the_result_equals_the_legacy_query_verbatim(self) -> None:
        """与升级前那条查询逐值对拍"""
        await add_message(CHAT_KEY_A, "1")
        await add_message(CHAT_KEY_A, "2")
        await add_message(CHAT_KEY_A, "3")
        await add_legacy_affection(INSTANCE_A, "1", 40)
        await add_legacy_affection(INSTANCE_A, "2", 60)
        await add_legacy_affection(INSTANCE_B, "3", 90)

        legacy = sorted(
            (str(row["platform_userid"]), int(row["score"]))
            for row in await DBAffection.filter(
                adapter_key=ADAPTER_KEY,
                instance_key=INSTANCE_A,
                platform_userid__in=["1", "2", "3"],
            ).values("platform_userid", "score")
        )
        assert ranked(await collect()) == legacy

    async def test_a_single_instance_deployment_has_a_domain_of_its_own(self) -> None:
        # 空实例标识是**合法**的实例标识（单实例适配器），不是"缺失"
        await add_message(CHAT_KEY_A, "1")
        await add_legacy_affection("", "1", 40)

        collection = await collect(_StubChannel(instance_key=""))
        assert ranked(collection) == [("1", 40)]
        assert collection.scope.affection_domain.domain_id == f"i:{ADAPTER_KEY}:"

    async def test_no_config_means_no_extra_rows_are_needed(self) -> None:
        """零配置 = 零默认数据：别名表与域绑定表一行都不用有"""
        await add_message(CHAT_KEY_A, "1")
        await add_legacy_affection(INSTANCE_A, "1", 40)

        assert ranked(await collect()) == [("1", 40)]
        assert await DBIdentityAlias.all().count() == 0
        assert await DBInstanceDomainBinding.all().count() == 0

    async def test_the_person_is_the_account_itself(self) -> None:
        await add_message(CHAT_KEY_A, "1")
        await add_legacy_affection(INSTANCE_A, "1", 40)

        collection = await collect(highlight_userid="1")
        assert collection.highlight_person_uid == "1"
        assert collection.scope.is_alias is False


@pytest.mark.usefixtures("rank_db")
class TestPluginPassesTheResolvedScopeThrough:
    """插件那一层的接线

    取数解析对了、插件却把原始账号当成"人"传给视图，用户看到的就是"我的行没高亮"。
    这一层只有一处接线，但它是用户唯一能感知到的地方。
    """

    async def test_the_command_highlights_the_person_behind_the_alt_account(self) -> None:
        from plugins.builtin import affection_rank as rank_plugin

        await add_message(CHAT_KEY_A, USER_MAIN)
        await add_message(CHAT_KEY_A, "30003")
        await add_alias(ScopeDomain.of_instance(ADAPTER_KEY, INSTANCE_A), USER_ALT, USER_MAIN)
        await add_affection(USER_MAIN, 40)
        await add_affection("30003", 60)

        class _PluginChannel(_StubChannel):
            channel_id = "onebot_v11_777"
            chat_type = ChatType.GROUP

        view, subtitle = await rank_plugin._build_view(  # pyright: ignore[reportPrivateUsage]
            _PluginChannel(),  # pyright: ignore[reportArgumentType]
            highlight_userid=USER_ALT,
        )
        assert [row.highlighted for row in view.rows] == [False, True]
        assert "777" in subtitle


@pytest.mark.usefixtures("rank_db")
class TestMergeIsReversibleOnTheBoard:
    """合并 → 撤销 → 榜单逐值回到原样

    「换账号不能丢」这条设计的前提是**合错了能拆开**。上面几组只验证了"合并之后榜上是一行"，
    那是合并生效；这一组走真的 `services/affection/merge.py`（不是自己插一行别名），
    并且比对**撤销之后**的榜：合并要是把主号那一行的分数直接改掉、或者动了副号那一行，
    这里会当场红。
    """

    @staticmethod
    def _domain() -> ScopeDomain:
        return ScopeDomain.of_instance(ADAPTER_KEY, INSTANCE_A)

    async def test_merge_then_unmerge_restores_both_rows_verbatim(self) -> None:
        await add_message(CHAT_KEY_A, USER_MAIN, nickname="主号", timestamp=int(NOW) - 100)
        await add_message(CHAT_KEY_A, USER_ALT, nickname="副号", timestamp=int(NOW))
        await add_affection(USER_MAIN, 40)
        await add_affection(USER_ALT, 90)

        before = ranked(await collect())
        assert before == [(USER_MAIN, 40), (USER_ALT, 90)]

        await merge_accounts(self._domain(), person_uid=USER_MAIN, alias_platform_userid=USER_ALT)
        merged = await collect()
        # 合并语义是"好感取高的"：这个人拿到 90，榜上只剩一行，活跃人数也按人算
        assert ranked(merged) == [(USER_MAIN, 90)]
        assert merged.total_active == 1

        await unmerge_account(self._domain(), alias_platform_userid=USER_ALT)
        # 删别名即撤销：两边各自回到自己的原值，一分不多一分不少
        assert ranked(await collect()) == before

    async def test_the_alt_row_is_never_touched_by_a_merge(self) -> None:
        """副账号那一行"一个字节都不动"，撤销之后它带着原值重新上榜"""
        await add_message(CHAT_KEY_A, USER_ALT, nickname="副号")
        await add_affection(USER_MAIN, 10)
        await add_affection(USER_ALT, 77)

        await merge_accounts(self._domain(), person_uid=USER_MAIN, alias_platform_userid=USER_ALT)
        assert ranked(await collect()) == [(USER_MAIN, 77)]

        await unmerge_account(self._domain(), alias_platform_userid=USER_ALT)
        # 主号一句话没说过，撤销之后榜上只剩副号自己那一份原值
        assert ranked(await collect()) == [(USER_ALT, 77)]

    async def test_a_merge_takes_effect_on_the_next_call(self) -> None:
        """合并/撤销必须热生效：中间没有任何进程重启，任何 TTL 缓存都会让这条红"""
        await add_message(CHAT_KEY_A, USER_MAIN, timestamp=int(NOW) - 100)
        await add_message(CHAT_KEY_A, USER_ALT, timestamp=int(NOW))
        await add_affection(USER_MAIN, 40)
        await add_affection(USER_ALT, 90)

        assert (await collect()).total_active == 2
        await merge_accounts(self._domain(), person_uid=USER_MAIN, alias_platform_userid=USER_ALT)
        assert (await collect()).total_active == 1
        await unmerge_account(self._domain(), alias_platform_userid=USER_ALT)
        assert (await collect()).total_active == 2


@pytest.mark.usefixtures("rank_db")
class TestTheBoardIsAlwaysTheCallersOwnChannel:
    """沙盒方法的 `chat_key` 由模型自由填写，不能拿它去读别的会话那一份好感

    这是"跨租户串域"在插件侧的落点：榜单的范围恒等于**那个 chat_key 所在频道**的
    (域 + 人设)，所以不校验归属 = 让 A 实例的沙盒指定 B 的会话、把 B 那份榜读进来。
    渲染出的图片会落进**发起方自己的**沙盒互通目录，上榜人数还会直接回到发起方模型的上下文。
    """

    @staticmethod
    async def _channel(chat_key: str, instance_key: str, channel_id: str) -> DBChatChannel:
        return await DBChatChannel.create(
            adapter_key=ADAPTER_KEY,
            instance_key=instance_key,
            channel_id=channel_id,
            channel_type=ChatType.GROUP.value,
            chat_key=chat_key,
            data="{}",
        )

    async def test_a_foreign_chat_key_is_refused_before_anything_is_read(self) -> None:
        from plugins.builtin import affection_rank as rank_plugin

        await self._channel(CHAT_KEY_A, INSTANCE_A, "onebot_v11_777")
        await self._channel(CHAT_KEY_B, INSTANCE_B, "onebot_v11_888")
        await add_message(CHAT_KEY_B, "1")
        await add_affection("1", 190, instance_key=INSTANCE_B)

        ctx = _StubCtx(CHAT_KEY_A)
        with pytest.raises(ValueError, match="当前会话"):
            await rank_plugin.send_affection_rank(ctx, CHAT_KEY_B)  # pyright: ignore[reportArgumentType]
        # 一个字节都没流进发起方的沙盒，也没有任何消息发出去
        assert (ctx.forwarded, ctx.sent) == ([], [])

    async def test_a_ctx_without_a_session_is_refused(self) -> None:
        from plugins.builtin import affection_rank as rank_plugin

        await self._channel(CHAT_KEY_A, INSTANCE_A, "onebot_v11_777")

        ctx = _StubCtx(None)
        with pytest.raises(ValueError, match="不属于任何会话"):
            await rank_plugin.send_affection_rank(ctx, CHAT_KEY_A)  # pyright: ignore[reportArgumentType]
        assert (ctx.forwarded, ctx.sent) == ([], [])

    async def test_its_own_channel_still_works(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """对照组：拒的是"别人的会话"，不是把这个方法整个废掉"""
        from plugins.builtin import affection_rank as rank_plugin

        await self._channel(CHAT_KEY_A, INSTANCE_A, "onebot_v11_777")
        await add_message(CHAT_KEY_A, USER_MAIN, nickname="主号")
        await add_affection(USER_MAIN, 40)

        from nekro_agent.api import message as message_api
        from nekro_agent.core.config import config as core_config

        # 发送走插件推送方法：先判「好感榜插件在这个频道生效」，再调 `nekro_agent.api.message.send_image`。
        # 本组钉的是归属而不是开关，所以把插件放进全局启用，并把发送接到替身上记账。
        monkeypatch.setattr(core_config, "PLUGIN_ENABLED", [*core_config.PLUGIN_ENABLED, rank_plugin.plugin.key])

        async def _send_image(chat_key: str, file_path: str, ctx: Any, **_kwargs: Any) -> None:
            ctx.sent.append((chat_key, file_path))

        monkeypatch.setattr(message_api, "send_image", _send_image)

        rendered = _capture_render(monkeypatch)
        ctx = _StubCtx(CHAT_KEY_A)
        result = await rank_plugin.send_affection_rank(ctx, CHAT_KEY_A, USER_MAIN)  # pyright: ignore[reportArgumentType]

        assert ctx.sent == [(CHAT_KEY_A, "/sandbox/card.png")]
        assert "1 人上榜" in result
        # 发起人交出去了：他自己那一行是高亮的
        assert [row.highlighted for row in rendered[0].rows] == [True]


@pytest.mark.usefixtures("rank_db")
class TestTheCommandEntryPointIsWired:
    """群内指令那一头的接线

    `_build_view` 由上面几组钉住，这里钉的是它**被怎么调用的**：发起人有没有交出去、
    私聊闸门在不在、文本降级会不会把两百行糊到群里。本项目反复栽在"取数改对了、入口照旧"
    这个形态上，所以入口必须自己有用例，而不是靠取数层的用例顺带。
    """

    @staticmethod
    async def _channel(chat_key: str, channel_type: ChatType) -> DBChatChannel:
        return await DBChatChannel.create(
            adapter_key=ADAPTER_KEY,
            instance_key=INSTANCE_A,
            channel_id="onebot_v11_777",
            channel_type=channel_type.value,
            chat_key=chat_key,
            data="{}",
        )

    @staticmethod
    def _context(user_id: str, chat_key: str = CHAT_KEY_A) -> CommandExecutionContext:
        return CommandExecutionContext(
            user_id=user_id,
            chat_key=chat_key,
            username="tester",
            adapter_key=ADAPTER_KEY,
        )

    async def test_the_command_passes_the_requester_through(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from plugins.builtin import affection_rank as rank_plugin

        await self._channel(CHAT_KEY_A, ChatType.GROUP)
        await add_message(CHAT_KEY_A, USER_MAIN, nickname="主号")
        await add_message(CHAT_KEY_A, "30003", nickname="别人")
        await add_alias(ScopeDomain.of_instance(ADAPTER_KEY, INSTANCE_A), USER_ALT, USER_MAIN)
        await add_affection(USER_MAIN, 40)
        await add_affection("30003", 60)

        rendered = _capture_render(monkeypatch)
        # 用副号发的指令：入口不把 `context.user_id` 交出去的话，这里一行都不会亮
        response = await rank_plugin.affection_rank_cmd(self._context(USER_ALT))

        assert response.status == CommandResponseStatus.SUCCESS
        assert [row.highlighted for row in rendered[0].rows] == [False, True]

    async def test_a_private_chat_is_refused_by_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from plugins.builtin import affection_rank as rank_plugin

        await self._channel(CHAT_KEY_A, ChatType.PRIVATE)
        await add_message(CHAT_KEY_A, USER_MAIN)
        await add_affection(USER_MAIN, 40)

        rendered = _capture_render(monkeypatch)
        response = await rank_plugin.affection_rank_cmd(self._context(USER_MAIN))

        assert response.status == CommandResponseStatus.ERROR
        assert rendered == [], "私聊被拒时不该已经渲染过一张卡片"

    async def test_the_text_fallback_lists_only_the_head(self) -> None:
        from plugins.builtin import affection_rank as rank_plugin

        view = build_rank_view(
            [RankEntry(person_uid=f"{index:05d}", nickname=f"u{index}", score=index) for index in range(12)],
            total_active=12,
        )
        text = rank_plugin._text_fallback(view, "群 777 · 近30天活跃 12 人")  # pyright: ignore[reportPrivateUsage]

        # 一条几百行的文本消息在群里比不发还糟：只列前 10 名，剩下的折成一行
        assert len(text.splitlines()) == 1 + 10 + 1
        assert text.splitlines()[-1] == "…… 共 12 人"


@pytest.mark.usefixtures("rank_db")
class TestZeroConfigOnARealChannelNotAStub:
    """零配置回归必须跑在**真** `DBChatChannel` 上

    上一组用的频道替身把 `get_effective_preset_id()` 写死成 `PRESET_NONE`，于是
    "零配置时人设这一维等于 0"这条恰好是**替身自己保证**的：被测代码怎么改，那几条都不会红。
    真频道的人设走 `get_preset()` 那条四层回退链（频道 → 实例默认 → 系统默认 → 内置），
    而**出厂部署在启动时就会**跑 `preset_service.init_default_preset()`，
    把 `AI_CHAT_DEFAULT_PRESET_ID` 指到一行真人设上——
    也就是说"一个人设都没绑"的频道解析出来并不是 0。
    """

    @staticmethod
    async def _channel() -> DBChatChannel:
        return await DBChatChannel.create(
            adapter_key=ADAPTER_KEY,
            instance_key=INSTANCE_A,
            channel_id="group_777",
            channel_type=ChatType.GROUP.value,
            chat_key=CHAT_KEY_A,
            preset_id=None,
            data="{}",
        )

    @staticmethod
    async def _system_default_preset(monkeypatch: pytest.MonkeyPatch) -> DBPreset:
        preset = await DBPreset.create(
            name="可洛喵",
            title="可洛喵",
            avatar="",
            content="系统默认人设",
            description="系统默认人设",
            tags="默认",
            author="system",
            owner_instance_key=GLOBAL_PRESET_OWNER,
        )
        monkeypatch.setattr(core_config, "AI_CHAT_DEFAULT_PRESET_ID", preset.id)
        return preset

    async def test_a_channel_with_no_preset_anywhere_lands_on_slot_zero(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """连系统默认人设都没配的部署：真频道也解析成 0，存量行照常上榜"""
        monkeypatch.setattr(core_config, "AI_CHAT_DEFAULT_PRESET_ID", None)
        channel = await self._channel()
        await add_message(CHAT_KEY_A, "1")
        await add_legacy_affection(INSTANCE_A, "1", 88)

        assert await channel.get_effective_preset_id() == PRESET_NONE
        collection = await collect_rank_entries(channel=channel, active_days=30, now=NOW)
        assert ranked(collection) == [("1", 88)]

    async def test_the_system_default_preset_is_the_slot_new_rows_land_in(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """配了系统默认人设之后，"没绑人设"的频道其实在那个人设档上

        这是回退链的设计意图（"用户面对的就是那个人格"），不是 bug；
        钉住它是为了让下一条的前提可见。
        """
        preset = await self._system_default_preset(monkeypatch)
        channel = await self._channel()
        await add_message(CHAT_KEY_A, "1")
        await add_affection("1", 88, preset_id=preset.id)

        assert await channel.get_effective_preset_id() == preset.id
        collection = await collect_rank_entries(channel=channel, active_days=30, now=NOW)
        assert ranked(collection) == [("1", 88)]
        assert collection.scope.preset_id == preset.id

    @pytest.mark.xfail(
        strict=True,
        reason=(
            "已确认的缺口，责任在人设维的回退链而不在榜单：出厂部署启动时一定会把 "
            "AI_CHAT_DEFAULT_PRESET_ID 指到一行人设上，于是零配置频道解析出的人设是它的行 ID，"
            "而升级前写下的好感行 preset_id=0，整张榜当场空掉（存量行并没有丢，是被范围滤掉了）。"
            "同一个根因在记忆侧由 tests/test_zz_probe_preset_default.py 探针记录。"
            "本条是压在榜单这一侧的绊线：根因修好的那一刻它会由 xfail 变成 XPASS 并让整轮变红，"
            "届时把这个标记删掉即可。"
        ),
    )
    async def test_pre_upgrade_rows_still_rank_on_a_stock_deployment(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        await self._system_default_preset(monkeypatch)
        channel = await self._channel()
        await add_message(CHAT_KEY_A, "1")
        await add_legacy_affection(INSTANCE_A, "1", 88)

        collection = await collect_rank_entries(channel=channel, active_days=30, now=NOW)
        assert ranked(collection) == [("1", 88)]
