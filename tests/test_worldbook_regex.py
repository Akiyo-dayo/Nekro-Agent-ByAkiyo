"""世界书正则关键词：解析、校验、执行时限与熔断

正则来自管理员与导入的酒馆文件，两处都不可信，因此这份用例分三层：

1. **解析**：`/…/flags` 与 `use_regex` 两种来源、三个有意义的旗标、无效旗标、JS 命名组
   改写且不误伤后顾断言；
2. **校验**：危险形状（反向引用、重复套重复、重复里的 `|`、两个不定长重复、超长）一律
   被拒，判据与输出过滤是同一份；
3. **执行**：慢正则熔断后不再执行、`forget_entry` 能恢复、每轮总耗时超限后剩余关键词
   按未命中处理、熔断表与输出过滤的停用表互不影响。

计时全部用注入的假时钟，不靠 `sleep`：靠真实时间会让用例在慢机器上随机红。
"""

from __future__ import annotations

import pytest

from nekro_agent.services.output_filter import engine as output_filter_engine
from nekro_agent.services.output_filter.rules import MAX_REGEX_LENGTH
from nekro_agent.services.worldbook import safe_regex
from nekro_agent.services.worldbook.safe_regex import (
    RegexRunner,
    RegexSpec,
    disabled_regex_keys,
    forget_entry,
    parse_regex_key,
    validate_regex_key,
)


@pytest.fixture(autouse=True)
def _fresh_state() -> None:
    """每条用例从干净的熔断状态与编译缓存开始"""
    safe_regex._reset_runtime_state_for_tests()
    output_filter_engine._reset_runtime_state_for_tests()


class FakeClock:
    """按脚本推进的时钟：每次调用前进 `step` 秒

    `search` 每次对一个窗口调用两次时钟（结束减开始），因此 `step` 就是「一次 search 的
    耗时」。用脚本而不是真实时间，边界（恰好 0.1 秒）才能被准确表达。
    """

    def __init__(self, step: float = 0.0) -> None:
        self.step = step
        self.now = 0.0

    def __call__(self) -> float:
        self.now += self.step
        return self.now


class TestRuntimeSafetyCheck:
    """执行前再查一遍：手工改库、规则收紧之后存进来的坏正则也不能开跑"""

    @pytest.mark.parametrize(
        ("pattern", "text"),
        # `(a|b)` 会被编译器折成字符类、不再是分支，所以分支这一例要用多字符的两支。
        [(r"(a)\1", "xaay"), ("(ab|cd)+e", "xxabcde"), ("(a+)+c", "aac")],
    )
    def test_unsafe_pattern_never_runs(self, pattern: str, text: str) -> None:
        """这些形状跑起来都会命中；结果为假只能是因为执行前被安全检查挡下了"""
        runner = RegexRunner()
        spec = RegexSpec(pattern=pattern)
        assert runner.search(1, pattern, spec, text, case_sensitive=True) is False

    def test_overlong_pattern_never_runs(self) -> None:
        runner = RegexRunner()
        pattern = "a" * (MAX_REGEX_LENGTH + 1)
        spec = RegexSpec(pattern=pattern)
        assert runner.search(1, pattern, spec, pattern, case_sensitive=True) is False

    def test_safe_pattern_still_runs(self) -> None:
        runner = RegexRunner()
        spec = RegexSpec(pattern="龙.")
        assert runner.search(1, "龙.", spec, "一条龙来了", case_sensitive=True) is True


class TestParse:
    def test_slash_form_is_regex(self) -> None:
        spec = parse_regex_key("/龙./i", use_regex=False)
        assert spec is not None
        assert spec.valid
        assert spec.pattern == "龙."
        assert spec.flags & 2  # re.IGNORECASE

    def test_plain_keyword_is_not_regex(self) -> None:
        assert parse_regex_key("龙族", use_regex=False) is None

    def test_slash_form_without_flags(self) -> None:
        spec = parse_regex_key("/龙族/", use_regex=False)
        assert spec is not None
        assert spec.valid
        assert spec.flags == 0

    def test_greedy_to_last_slash(self) -> None:
        """模式里自己带 `/` 也要能解析：`(.+)` 贪婪到最后一个 `/`"""
        spec = parse_regex_key("/a/b/c/i", use_regex=False)
        assert spec is not None
        assert spec.pattern == "a/b/c"

    def test_use_regex_takes_whole_key(self) -> None:
        """`use_regex` 形态没有旗标语法：整条关键词就是模式"""
        spec = parse_regex_key("龙.*", use_regex=True)
        assert spec is not None
        assert spec.pattern == "龙.*"
        assert spec.flags == 0

    def test_use_regex_with_slashes_keeps_them(self) -> None:
        spec = parse_regex_key("/龙/", use_regex=True)
        assert spec is not None
        assert spec.pattern == "/龙/"

    @pytest.mark.parametrize(("source", "bit"), [("i", 2), ("s", 16), ("m", 8)])
    def test_meaningful_flags(self, source: str, bit: int) -> None:
        spec = parse_regex_key(f"/x/{source}", use_regex=False)
        assert spec is not None
        assert spec.flags & bit

    @pytest.mark.parametrize("source", ["g", "y", "u", "isyu"])
    def test_ignored_flags_are_accepted(self, source: str) -> None:
        """`g` / `y` / `u` 是酒馆里合法但对 Python 没有对应语义的旗标"""
        spec = parse_regex_key(f"/x/{source}", use_regex=False)
        assert spec is not None
        assert spec.valid

    def test_unknown_flag_is_invalid(self) -> None:
        spec = parse_regex_key("/x/q", use_regex=False)
        assert spec is not None
        assert not spec.valid
        assert "旗标" in spec.error

    def test_empty_pattern_is_invalid(self) -> None:
        """空模式会匹配一切：`use_regex` 下必须判无效，否则一条没写完的条目等于常驻"""
        spec = parse_regex_key("", use_regex=True)
        assert spec is not None
        assert not spec.valid

    def test_slash_form_needs_a_pattern(self) -> None:
        """`/…/flags` 形态的 `(.+)` 要求至少一个字符，`//` 根本不是正则形态"""
        assert parse_regex_key("//", use_regex=False) is None


class TestNamedGroups:
    def test_js_named_group_is_rewritten(self) -> None:
        spec = parse_regex_key("/(?<who>龙)/", use_regex=False)
        assert spec is not None
        assert spec.pattern == "(?P<who>龙)"

    @pytest.mark.parametrize("assertion", ["(?<=龙)", "(?<!龙)"])
    def test_lookbehind_is_untouched(self, assertion: str) -> None:
        """后顾断言也是 `(?<` 开头，一起改写会把合法正则变成语法错误"""
        spec = parse_regex_key(f"/{assertion}族/", use_regex=False)
        assert spec is not None
        assert spec.pattern == f"{assertion}族"
        assert validate_regex_key(f"/{assertion}族/", use_regex=False) is None

    def test_rewritten_group_compiles_and_matches(self) -> None:
        runner = RegexRunner()
        spec = parse_regex_key("/(?<who>龙)族/", use_regex=False)
        assert spec is not None
        assert runner.search(1, "(?<who>龙)族", spec, "他看见龙族飞过") is True


class TestValidate:
    @pytest.mark.parametrize(
        "pattern",
        [
            r"/(a)\1/",  # 反向引用
            r"/(a+)+/",  # 重复套重复
            r"/(a|aa)+/",  # 重复里用 |
            r"/.*a.*b/",  # 两个不定长重复
            "/" + "a" * (MAX_REGEX_LENGTH + 1) + "/",  # 超长
        ],
    )
    def test_dangerous_patterns_are_rejected(self, pattern: str) -> None:
        """这些形状都会灾难性回溯，必须在保存时就挡住"""
        reason = validate_regex_key(pattern, use_regex=False)
        assert reason is not None
        assert reason != ""

    def test_plain_keyword_passes(self) -> None:
        assert validate_regex_key("龙族", use_regex=False) is None

    def test_simple_regex_passes(self) -> None:
        assert validate_regex_key("/龙.族/", use_regex=False) is None

    def test_broken_syntax_is_rejected(self) -> None:
        """复杂度检查放过它、`re.compile` 挡下来：两层校验都要在"""
        reason = validate_regex_key("/(?P<n>龙/", use_regex=False)
        assert reason is not None

    def test_limit_is_shared_with_output_filter(self) -> None:
        """长度上限与输出过滤同一个常量，两边不该出现两个数"""
        assert MAX_REGEX_LENGTH == 200


class TestExecution:
    def _run(self, runner: RegexRunner, pattern: str, text: str, *, entry_id: int = 1) -> bool:
        spec = parse_regex_key(pattern, use_regex=True)
        assert spec is not None
        return runner.search(entry_id, pattern, spec, text)

    def test_matches(self) -> None:
        assert self._run(RegexRunner(), "龙.族", "他看见龙与族飞过") is True

    def test_does_not_match(self) -> None:
        assert self._run(RegexRunner(), "凤凰", "他看见龙族飞过") is False

    def test_case_sensitivity_flag_comes_from_slash_form(self) -> None:
        """`/…/` 形态只看自己的旗标，与酒馆一致"""
        spec = parse_regex_key("/dragon/", use_regex=False)
        assert spec is not None
        runner = RegexRunner()
        assert runner.search(1, "/dragon/", spec, "DRAGON", case_sensitive=True) is False
        spec_i = parse_regex_key("/dragon/i", use_regex=False)
        assert spec_i is not None
        assert runner.search(1, "/dragon/i", spec_i, "DRAGON", case_sensitive=True) is True

    def test_case_sensitivity_from_entry_for_use_regex(self) -> None:
        spec = parse_regex_key("dragon", use_regex=True)
        assert spec is not None
        runner = RegexRunner()
        assert runner.search(1, "dragon", spec, "DRAGON", case_sensitive=False) is True
        assert runner.search(2, "dragon", spec, "DRAGON", case_sensitive=True) is False

    def test_dotall_flag(self) -> None:
        assert self._run(RegexRunner(), r"龙.族", "龙\n族") is False
        assert self._run(RegexRunner(), r"龙.?族", "龙\n族") is False
        assert self._run(RegexRunner(), r"(?s)龙.族", "龙\n族") is True

    def test_multiline_flag(self) -> None:
        assert self._run(RegexRunner(), "^族", "龙\n族") is False
        assert self._run(RegexRunner(), "(?m)^族", "龙\n族") is True

    def test_long_text_is_scanned_in_overlapping_windows(self) -> None:
        """命中跨在窗口边界上时，没有重叠就两边都看不见"""
        text = "a" * (output_filter_engine.REGEX_WINDOW - 2) + "龙族" + "b" * 100
        assert self._run(RegexRunner(), "龙族", text) is True

    def test_slow_regex_is_disabled_after_limit(self) -> None:
        """同一个（条目, 关键词）慢到 `SLOW_LIMIT` 次就熔断"""
        clock = FakeClock(step=safe_regex.SLOW_SECONDS + 0.05)
        runner = RegexRunner(clock=clock, budget_seconds=100.0)
        pattern = "龙"
        for _ in range(safe_regex.SLOW_LIMIT):
            assert self._run(runner, pattern, "龙", entry_id=7) is True
        assert (7, pattern) in disabled_regex_keys()

        # 熔断之后直接返回 False，连一次 search 都不做（时钟不再前进）。
        frozen = clock.now
        assert self._run(runner, pattern, "龙", entry_id=7) is False
        assert clock.now == frozen

    def test_forget_entry_restores_it(self) -> None:
        """条目被编辑（往往就是在修那条慢正则）后要能立刻恢复"""
        clock = FakeClock(step=safe_regex.SLOW_SECONDS + 0.05)
        runner = RegexRunner(clock=clock, budget_seconds=100.0)
        for _ in range(safe_regex.SLOW_LIMIT):
            self._run(runner, "龙", "龙", entry_id=7)
        assert (7, "龙") in disabled_regex_keys()

        forget_entry(7)
        assert disabled_regex_keys() == {}

        runner2 = RegexRunner(clock=FakeClock(step=0.0), budget_seconds=100.0)
        assert runner2.search(7, "龙", RegexSpec(pattern="龙"), "龙") is True

    def test_forget_entry_does_not_touch_other_entries(self) -> None:
        clock = FakeClock(step=safe_regex.SLOW_SECONDS + 0.05)
        runner = RegexRunner(clock=clock, budget_seconds=100.0)
        for entry_id in (1, 2):
            for _ in range(safe_regex.SLOW_LIMIT):
                self._run(runner, "龙", "龙", entry_id=entry_id)
        assert set(disabled_regex_keys()) == {(1, "龙"), (2, "龙")}

        forget_entry(1)
        assert set(disabled_regex_keys()) == {(2, "龙")}

    def test_budget_exhaustion_stops_further_regexes(self) -> None:
        """本轮总耗时超限后剩余关键词按未命中处理，并且只记一条 WARNING

        产品代码走 loguru，`caplog` 恒为空，所以这里接一个真正的 loguru sink。
        """
        from loguru import logger

        captured: list[str] = []
        sink_id = logger.add(lambda message: captured.append(message.record["message"]), level="WARNING")
        try:
            clock = FakeClock(step=0.06)
            runner = RegexRunner(clock=clock, budget_seconds=0.05)
            assert self._run(runner, "龙", "龙", entry_id=1) is True
            assert self._run(runner, "凤", "凤", entry_id=2) is False
            assert self._run(runner, "凰", "凰", entry_id=3) is False
        finally:
            logger.remove(sink_id)

        budget_warnings = [message for message in captured if "总耗时" in message]
        assert len(budget_warnings) == 1

    def test_budget_is_per_runner(self) -> None:
        """耗时按轮次累计：新的一轮（新实例）从零开始"""
        first = RegexRunner(clock=FakeClock(step=0.06), budget_seconds=0.05)
        assert self._run(first, "龙", "龙", entry_id=1) is True
        second = RegexRunner(clock=FakeClock(step=0.0), budget_seconds=0.05)
        assert self._run(second, "龙", "龙", entry_id=1) is True

    def test_invalid_spec_never_matches(self) -> None:
        spec = RegexSpec(pattern="龙", error="不支持的旗标 'q'")
        assert RegexRunner().search(1, "/龙/q", spec, "龙") is False

    def test_uncompilable_pattern_never_raises(self) -> None:
        """编译不过的正则（导入时校验漏掉的）不该让整轮提示词抛异常"""
        assert RegexRunner().search(1, "(", RegexSpec(pattern="("), "(") is False


class TestIsolationFromOutputFilter:
    def test_disabled_tables_are_separate(self) -> None:
        """两边的键完全不是一回事，混在一起会让一条坏世界书正则停掉无辜的过滤规则"""
        clock = FakeClock(step=safe_regex.SLOW_SECONDS + 0.05)
        runner = RegexRunner(clock=clock, budget_seconds=100.0)
        spec = parse_regex_key("龙", use_regex=True)
        assert spec is not None
        for _ in range(safe_regex.SLOW_LIMIT):
            runner.search(5, "龙", spec, "龙")

        assert disabled_regex_keys() == {(5, "龙"): safe_regex.DISABLED_REASON}
        assert output_filter_engine.disabled_regexes() == {}

    def test_output_filter_disable_does_not_affect_worldbook(self) -> None:
        """反向也要成立：输出过滤停用了一条正则，世界书里同名正则照常跑"""
        output_filter_engine._disabled_regexes["龙"] = "运行过慢，已停用"
        try:
            spec = parse_regex_key("龙", use_regex=True)
            assert spec is not None
            assert RegexRunner().search(1, "龙", spec, "龙") is True
            assert disabled_regex_keys() == {}
        finally:
            output_filter_engine._reset_runtime_state_for_tests()
