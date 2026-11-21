"""卡片检查报告的一句结论、`{{user}}` 密度提醒与名字变量说明

结论文案与分档是纯函数，直接调 `_verdict`；密度与名字变量说明也直接调各自的拼装函数，
再用一张编出来的卡走一遍检查接口（见 `test_tavern_api.py`）。样例文字都是这里现编的。
"""

from __future__ import annotations

import time

from nekro_agent.services.tavern import importer
from nekro_agent.services.worldbook.st_format import ImportNote


class TestVerdict:
    def test_full(self) -> None:
        verdict = importer._verdict(  # noqa: SLF001
            state_only=0,
            scripts=0,
            html_rules=0,
            remote_books=0,
            unsupported_templates=0,
        )

        assert verdict.level == "full"
        assert verdict.text == "可完整使用：人设和世界书都能直接生效。"

    def test_partial_lists_only_the_nonzero_parts(self) -> None:
        verdict = importer._verdict(  # noqa: SLF001
            state_only=0,
            scripts=2,
            html_rules=0,
            remote_books=1,
            unsupported_templates=3,
        )

        assert verdict.level == "partial"
        assert verdict.text == (
            "能用，少了这些附加功能：酒馆助手脚本 2 个（NA 不执行）、"
            "脚本在线加载的世界书 1 本（可以在下方让 NA 代下）、不支持的模板 3 条（不注入）。"
        )

    def test_state_wins_over_other_parts(self) -> None:
        verdict = importer._verdict(  # noqa: SLF001
            state_only=1,
            scripts=2,
            html_rules=1,
            remote_books=1,
            unsupported_templates=1,
        )

        assert verdict.level == "state"
        assert "角色状态" in verdict.text
        assert "酒馆助手脚本" not in verdict.text

    def test_suspected_traps_are_not_an_input(self) -> None:
        # 疑似防盗条目是故意不注入的，不进结论：所有计数为 0 时就是可完整使用。
        verdict = importer._verdict(  # noqa: SLF001
            state_only=0,
            scripts=0,
            html_rules=0,
            remote_books=0,
            unsupported_templates=0,
        )

        assert verdict.level == "full"


class TestUserDensity:
    def test_nineteen_mentions_do_not_warn(self) -> None:
        assert importer._user_density_note("{{user}}" * 19) is None  # noqa: SLF001

    def test_twenty_mentions_warn_and_mix_the_legacy_form(self) -> None:
        content = "{{ user }}" * 10 + "<USER>" * 10

        note = importer._user_density_note(content)  # noqa: SLF001

        assert note is not None
        assert note.kind == "user_density"
        assert note.field == "description"
        assert "出现了 20 次" in note.detail
        assert "{{user}}" in note.detail
        assert "建议只在私聊里用" in note.detail

    def test_adversarial_hundred_thousand_chars_is_fast(self) -> None:
        content = "{{user" * 10_000 + "<user" * 10_000
        assert len(content) >= 100_000

        started = time.thread_time()
        assert importer._user_density_note(content) is None  # noqa: SLF001
        elapsed = time.thread_time() - started

        assert elapsed < 1.0


class TestNameVarNote:
    def test_it_names_the_variables_once_per_card(self) -> None:
        notes = [
            ImportNote("1", "content", "name_var", "x"),
            ImportNote("2", "content", "name_var", "x"),
        ]
        rows = [
            ({"uid": 1}, {"content": "{{getvar::数据库用户名}} 和 {{getvar::角色名}}"}),
            ({"uid": 2}, {"content": "{{getvar::数据库用户名}}"}),
        ]

        note = importer._name_var_note(rows, notes)  # noqa: SLF001

        assert note is not None
        assert note.kind == "name_var"
        assert note.detail == (
            "卡内知识书里 2 条把读名字的变量换成了宏（共 3 处：「数据库用户名」→ {{user}}、"
            "「角色名」→ {{char}}）；猜错了可以在导入后的书详情里改回"
        )
