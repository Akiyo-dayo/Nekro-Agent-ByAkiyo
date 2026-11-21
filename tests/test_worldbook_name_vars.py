"""世界书正文里「读用户 / 角色名字」的变量换成宏

样本都是这里现编的：`数据库用户名`、`角色名`、`小明` 这类名字，不取自任何真实卡片。
钉住两种写法、按结尾认的分类（含「用户名额」「角色名单」这类不该换的）、条件里与多参数
调用不动、多个变量的计数与顺序、快速路径，以及 `st_entry_to_columns` 接入后的正文、
导入说明与关掉开关时的原样；最后一条用十万字左右的对抗文本看耗时。
"""

from __future__ import annotations

import time

import pytest

from nekro_agent.services.worldbook import st_format
from nekro_agent.services.worldbook.name_vars import NameVarReplacement, rewrite_name_vars


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        ("<%= getvar('数据库用户名') %>", "{{user}}"),
        ('<%= getvar("数据库用户名") %>', "{{user}}"),
        ("<%- getLocalVar('角色名') -%>", "{{char}}"),
        ('<%= getGlobalVar("角色名") %>', "{{char}}"),
        ("<%=getChatVar('玩家名字')%>", "{{user}}"),
        ("<%= getLocalVar ( '角色名' ) ; %>", "{{char}}"),
    ],
)
def test_ejs_output_tags(content: str, expected: str) -> None:
    rewritten, replacements = rewrite_name_vars(content)

    assert rewritten == expected
    assert len(replacements) == 1
    assert replacements[0].macro == expected


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        ("{{getvar::数据库用户名}}", "{{user}}"),
        ("{{ GETGLOBALVAR :: 角色名 }}", "{{char}}"),
        ("{{getvar::UserName}}", "{{user}}"),
        ("{{getvar::CharName}}", "{{char}}"),
    ],
)
def test_tavern_macros(content: str, expected: str) -> None:
    rewritten, replacements = rewrite_name_vars(content)

    assert rewritten == expected
    assert replacements == [NameVarReplacement(replacements[0].var_name, expected, 1)]


@pytest.mark.parametrize(
    ("var_name", "expected_macro"),
    [
        # 用户类：最后一段是整词
        ("用户名", "{{user}}"),
        ("玩家名", "{{user}}"),
        ("主角名", "{{user}}"),
        ("username", "{{user}}"),
        ("playername", "{{user}}"),
        ("用户名字", "{{user}}"),
        ("用户名称", "{{user}}"),
        ("用户姓名", "{{user}}"),
        ("用户昵称", "{{user}}"),
        ("玩家名字", "{{user}}"),
        ("玩家名称", "{{user}}"),
        ("玩家昵称", "{{user}}"),
        ("主角名字", "{{user}}"),
        ("主角名称", "{{user}}"),
        ("主角姓名", "{{user}}"),
        ("主角昵称", "{{user}}"),
        ("usernickname", "{{user}}"),
        ("playernickname", "{{user}}"),
        # 用户类：整个名字带字样，最后一段以名字类词结尾
        ("数据库用户名称", "{{user}}"),
        ("主角.名字", "{{user}}"),
        ("userName", "{{user}}"),
        ("player_name", "{{user}}"),
        ("stat_data.玩家.昵称", "{{user}}"),
        # 用户类：只是碰巧带「用户 / 玩家」或「名」的字样，不换
        ("用户名额", None),
        ("用户签名", None),
        ("用户称呼", None),
        ("玩家排名", None),
        ("user_name_count", None),
        ("玩家ID", None),
        # 角色类：最后一段是整词，或整个名字带字样且以名字类词结尾
        ("角色名", "{{char}}"),
        ("charname", "{{char}}"),
        ("charactername", "{{char}}"),
        ("角色名字", "{{char}}"),
        ("角色名称", "{{char}}"),
        ("角色姓名", "{{char}}"),
        ("角色昵称", "{{char}}"),
        ("character_name", "{{char}}"),
        ("CharName", "{{char}}"),
        ("character name", "{{char}}"),
        ("角色名单", None),
        ("角色.名字", "{{char}}"),
        ("char.name", "{{char}}"),
        ("当前角色名称", "{{char}}"),
        # 带命名空间前缀的单段名
        ("global_user_name", "{{user}}"),
        ("当前用户昵称", "{{user}}"),
        ("stat_data.玩家名称", "{{user}}"),
        # 「谁」和名字类词之间夹了别的东西：是别的东西的名字，不换
        ("stat_data.主角.武器.名称", None),
        ("玩家公会名称", None),
        ("主角坐骑名字", None),
        ("player_rank_name", None),
        ("user.pet.nickname", None),
        ("stat_data.角色.当前地点.名称", None),
        ("角色技能名称", None),
        ("char_weapon_name", None),
        ("敌对玩家名称", None),
        ("stat_data.名称", None),
        # 两者都不是
        ("好感度", None),
        ("stat_data.金钱", None),
        ("名字", None),
    ],
)
def test_classification(var_name: str, expected_macro: str | None) -> None:
    rewritten, replacements = rewrite_name_vars(f"{{{{getvar::{var_name}}}}}")

    if expected_macro is None:
        assert rewritten == f"{{{{getvar::{var_name}}}}}"
        assert replacements == []
    else:
        assert rewritten == expected_macro
        assert replacements[0].macro == expected_macro


def test_condition_and_multi_argument_reads_are_left_alone() -> None:
    content = (
        "<% if (getvar('用户名') == '小明') { %>你好<% } %>"
        "<%= getvar('用户名') + '大人' %>"
        "<%= getvar('用户名', '小明') %>"
        "{{getvar::}}"
    )

    assert rewrite_name_vars(content) == (content, [])


def test_several_variables_count_and_order() -> None:
    content = (
        "<%= getvar('数据库用户名') %>、{{getvar::角色名}}、"
        "<%= getvar('数据库用户名') %>、{{getvar::角色名}}"
    )

    rewritten, replacements = rewrite_name_vars(content)

    assert rewritten == "{{user}}、{{char}}、{{user}}、{{char}}"
    assert replacements == [
        NameVarReplacement("数据库用户名", "{{user}}", 2),
        NameVarReplacement("角色名", "{{char}}", 2),
    ]


def test_fast_path_returns_input_unchanged() -> None:
    content = "这是一段没有变量线索的正文，" * 20

    rewritten, replacements = rewrite_name_vars(content)

    assert rewritten is content
    assert replacements == []


def test_st_entry_to_columns_rewrites_content_and_reports() -> None:
    raw = {
        "uid": 7,
        "content": "你好，<%= getvar('数据库用户名') %>。{{getvar::角色名}}也来了。",
    }

    columns, notes = st_format.st_entry_to_columns(raw)

    assert columns["content"] == "你好，{{user}}。{{char}}也来了。"
    reported = [note for note in notes if note.kind == "name_var"]
    assert reported == [
        st_format.ImportNote(
            "7",
            "content",
            "name_var",
            "读名字的变量换成了宏：「数据库用户名」→ {{user}} ×1、「角色名」→ {{char}} ×1",
        ),
    ]


def test_st_entry_to_columns_can_skip_rewriting() -> None:
    raw = {
        "uid": 8,
        "content": "你好，<%= getvar('数据库用户名') %>。",
    }

    columns, notes = st_format.st_entry_to_columns(raw, rewrite_names=False)

    assert columns["content"] == raw["content"]
    assert [note for note in notes if note.kind == "name_var"] == []


def test_adversarial_hundred_thousand_chars_is_fast() -> None:
    chunk = (
        "<%= getvar('" + "名" * 64 + "') %>"
        "{{getvar::" + "名" * 64 + "}}"
        "<%getLocalVar('" + "x" * 64 + ")"
        "{{getvar::"
    )
    content = chunk * 800
    assert len(content) >= 100_000

    started = time.thread_time()
    _rewritten, _replacements = rewrite_name_vars(content)
    elapsed = time.thread_time() - started

    assert elapsed < 1.0
