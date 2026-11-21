"""角色状态的纯逻辑：模板、状态树、初值解析与宿主校验器

要钉住的事：

1. 初值解析拒绝 YAML 锚点与别名（billion laughs）、限长度 / 节点数 / 深度，JSON 路也限深度；
2. 推导模板照 MVU 的 `$meta`、二元组，`_` 开头默认隐藏只读，私密词表命中默认私密；
3. 校验器：路径、权限（隐藏 / 只读 / 好感映射 / 私密只在私聊）、四种操作、可扩展映射补齐、二元组、
   数的截断、字符串清洗与记忆前过滤、数组条数、新键名、单次上限、被拒的不影响别的处。
"""

from __future__ import annotations

import time
from typing import Any

import pytest

from plugins.builtin.state_table import initvar, validator
from plugins.builtin.state_table import tree as state_tree
from plugins.builtin.state_table.schema import WILDCARD, FieldDef, Template, TemplateError
from plugins.builtin.state_table.validator import PRESENT_KEY, Policy, apply_patch

INITVAR = """
当前时间: 早上
所在地点: 码头
_显示用: 不给模型看
好感度: [50, "对主角的好感，0 到 100"]
角色:
  $meta:
    extensible: true
  阿青:
    心情: 平静
    穿着: 校服
事件: [开场]
私密档案:
  经历: 无
"""


class TestParseInitvar:
    def test_yaml_and_wrappers(self) -> None:
        assert initvar.parse_initvar_text("<initvar>\na: 1\n</initvar>") == {"a": 1}
        assert initvar.parse_initvar_text("```yaml\na: 2\n```") == {"a": 2}
        assert initvar.parse_initvar_text('{"a": 3}') == {"a": 3}

    def test_macros_stay_raw_and_values_expand_per_channel(self) -> None:
        raw = initvar.parse_initvar_text("名字: '{{char}}'\n'{{user}}的包': '{{user}}的东西'")
        expand = lambda text: text.replace("{{char}}", "阿青").replace("{{user}}", "小林")  # noqa: E731

        assert raw == {"名字": "{{char}}", "{{user}}的包": "{{user}}的东西"}  # 模板存原文
        assert initvar.expand_values(raw, expand) == {"名字": "阿青", "{{user}}的包": "小林的东西"}  # 键名不展开
        assert raw["名字"] == "{{char}}"  # 原树不动
        assert len(initvar.expand_values(raw, lambda _text: "长" * 9000)["名字"]) == 2000  # 单个值有上限
        many = {f"键{index}": "{{user}}" for index in range(40)}
        assert initvar.expand_values(many, lambda _text: "长" * 2000) == many  # 整棵展开后太大就用原文

    @pytest.mark.parametrize(
        "text",
        ["a: 2077-13-01", "a: 1024-02-30", "a: " + "9" * 5000, '{"a": ' + "9" * 5000 + "}", "a: 0x" + "f" * 5000],
    )
    def test_whatever_the_parser_raises_is_an_initvar_error(self, text: str) -> None:
        with pytest.raises(initvar.InitvarError):
            initvar.parse_initvar_text(text)

    def test_big_integers_become_floats(self) -> None:
        assert initvar.parse_initvar_text('{"a": ' + "9" * 30 + "}") == {"a": float("9" * 30)}
        with pytest.raises(initvar.InitvarError, match="太大"):
            initvar.parse_initvar_text('{"a": ' + "9" * 400 + "}")

    def test_strip_meta(self) -> None:
        tree = {"$meta": {"strictSet": True}, "a": {"$meta": {"extensible": True}, "b": [{"$arrayMeta": True}, 1]}}

        assert initvar.strip_meta(tree) == {"a": {"b": [1]}}

    def test_anchors_and_aliases_are_rejected(self) -> None:
        bomb = "a: &a [x, x, x, x, x, x, x, x, x]\nb: &b [*a, *a, *a, *a, *a, *a, *a, *a, *a]\nc: [*b, *b, *b, *b, *b, *b, *b, *b, *b]\n"
        with pytest.raises(initvar.InitvarError, match="锚点"):
            initvar.parse_initvar_text(bomb)
        with pytest.raises(initvar.InitvarError, match="锚点"):
            initvar.parse_initvar_text("base: &b {x: 1}\nchild:\n  <<: *b\n")

    def test_limits(self) -> None:
        with pytest.raises(initvar.InitvarError, match="字"):
            initvar.parse_initvar_text("a: " + "x" * (initvar.MAX_INITVAR_CHARS + 1))
        with pytest.raises(initvar.InitvarError, match="节点"):
            initvar.parse_initvar_text("a: [" + "1," * 6000 + "1]")
        with pytest.raises(initvar.InitvarError, match="节点"):
            initvar.parse_initvar_text('{"a": [' + "1," * 6000 + "1]}")
        with pytest.raises(initvar.InitvarError, match="嵌套"):
            initvar.parse_initvar_text("[" * 40 + "]" * 40)
        with pytest.raises(initvar.InitvarError, match="嵌套"):
            initvar.parse_initvar_text("{" * 1 + '"a":' + "[" * 5000 + "]" * 5000 + "}")
        with pytest.raises(initvar.InitvarError, match="嵌套"):
            initvar.parse_initvar_text("a: " + "[" * 5000 + "]" * 5000)

    def test_pathological_inputs_are_fast(self) -> None:
        started = time.thread_time()
        for text in ("<initvar>" * 2000, "```" + "\n" * 10000, "a:\n" + "  b:\n" * 3000, "- " * 9000):
            try:
                initvar.parse_initvar_text(text)
            except initvar.InitvarError:
                pass
        assert time.thread_time() - started < 3

    def test_dates_become_strings_and_root_must_be_mapping(self) -> None:
        assert initvar.parse_initvar_text("日期: 2026-01-02") == {"日期": "2026-01-02"}
        with pytest.raises(initvar.InitvarError):
            initvar.parse_initvar_text("- 只是列表")
        with pytest.raises(initvar.InitvarError):
            initvar.parse_initvar_text("a: [1, 2\n")


class TestDeriveTemplate:
    def test_shapes_meta_pairs_hidden_and_private(self) -> None:
        tree = initvar.parse_initvar_text(INITVAR)
        template, cleaned = initvar.derive_template(tree, private_words=["私密"])

        assert "$meta" not in cleaned["角色"]
        time_def = template.match(("当前时间",))
        assert time_def is not None and time_def.type == "string" and time_def.priority == 3
        affection = template.match(("好感度",))
        assert affection is not None and affection.pair and affection.type == "number"
        assert affection.description.startswith("对主角的好感")
        hidden = template.match(("_显示用",))
        assert hidden is not None and hidden.hidden and hidden.readonly
        roles = template.match(("角色",))
        assert roles is not None and roles.extensible and roles.template == {"心情": "平静", "穿着": "校服"}
        assert template.match(("角色", "小林", "心情")) is not None
        assert template.match(("角色", WILDCARD, "心情")) is not None
        events = template.match(("事件",))
        assert events is not None and events.type == "array" and events.priority == 1
        assert template.is_private(("私密档案", "经历"))
        assert not template.is_private(("当前时间",))

    def test_round_trip_through_json(self) -> None:
        template, _ = initvar.derive_template(initvar.parse_initvar_text(INITVAR), private_words=["私密", " "])

        assert template.private_words == ("私密",)
        assert Template.from_json(template.to_json()) == template

    def test_extensible_maps_take_the_union_of_all_children(self) -> None:
        tree = {
            "角色": {
                "$meta": {"extensible": True},
                "阿青": {"心情": "平静"},
                "小林": {"心情": "开心", "私密经历": "有", "档案": {"身高": 1, "$meta": {}}},
            },
        }
        template, cleaned = initvar.derive_template(tree, private_words=["私密"])

        later_only = template.match(("角色", "阿青", "私密经历"))
        assert later_only is not None and later_only.private  # 只出现在后面孩子里的键也有定义
        assert template.match(("角色", "阿青", "档案", "身高")) is not None
        assert cleaned["角色"]["小林"]["档案"] == {"身高": 1}  # 后面孩子里的 $meta 也去掉

    def test_words_recorded_in_the_template_apply_to_undefined_paths(self) -> None:
        template = Template(fields=(FieldDef(path=("事件",), type="array"),), private_words=("秘密",))

        assert template.is_private(("事件", "0", "秘密"))  # 模板没定义到的具体路径照样按词表判
        assert template.is_private(("新加的", "小秘密"))
        assert template.is_private(("身高",), ["身高"])  # 实例的词表只加严
        assert not template.is_private(("事件", "0", "名字"))

    def test_bad_templates_are_rejected(self) -> None:
        with pytest.raises(TemplateError):
            Template.from_json({"fields": [{"path": [], "type": "string"}]})
        with pytest.raises(TemplateError):
            Template.from_json({"fields": [{"path": ["a"], "type": "函数"}]})
        with pytest.raises(TemplateError):
            Template.from_json({"fields": [{"path": ["a", "b"], "type": "object", "person": True}]})
        with pytest.raises(TemplateError):
            Template.from_json({"fields": [{"path": ["a"], "type": "string"}, {"path": ["a"], "type": "string"}]})


class TestPointer:
    @pytest.mark.parametrize(
        ("raw", "path"), [("/a", ("a",)), ("/a~1b/c~0d", ("a/b", "c~d")), ("/角色/阿青", ("角色", "阿青"))]
    )
    def test_parse(self, raw: str, path: tuple[str, ...]) -> None:
        assert state_tree.parse_pointer(raw) == path

    @pytest.mark.parametrize("raw", ["a", "", "/", "/a//b", "/" + "x" * 65, "/" + "/".join(["a"] * 17), 3])
    def test_invalid(self, raw: Any) -> None:
        with pytest.raises(state_tree.PointerError):
            state_tree.parse_pointer(raw)


def _template() -> Template:
    return Template(
        fields=(
            FieldDef(path=("当前时间",), type="string"),
            FieldDef(path=("好感度",), type="number", pair=True, minimum=0, maximum=100, affection="score"),
            FieldDef(path=("血量",), type="number", minimum=0, maximum=100),
            FieldDef(path=("_显示用",), type="string", hidden=True, readonly=True),
            FieldDef(path=("世界线",), type="string", readonly=True),
            FieldDef(path=("必填",), type="string", required=True),
            FieldDef(path=("事件",), type="array", max_items=3),
            FieldDef(path=("角色",), type="object", extensible=True, template={"心情": "平静", "等级": 1}),
            FieldDef(path=("角色", WILDCARD), type="object"),
            FieldDef(path=("角色", WILDCARD, "心情"), type="string", max_length=10),
            FieldDef(path=("角色", WILDCARD, "等级"), type="number"),
            FieldDef(path=("秘密",), type="string", private=True),
            FieldDef(path=("主角信息",), type="object", person=True),
            FieldDef(path=("主角信息", "穿着"), type="string"),
            FieldDef(path=("主角信息", "隐私"), type="string", private=True),
        ),
    )


def _channel() -> dict[str, Any]:
    return {
        "当前时间": "早上",
        "好感度": [50, "说明"],
        "血量": 80,
        "_显示用": "x",
        "世界线": "甲",
        "必填": "在",
        "事件": ["开场"],
        "角色": {"阿青": {"心情": "平静", "等级": 1}},
        "秘密": "无",
    }


def _apply(patch: Any, *, private: bool = False, affection: bool = True, people: dict | None = None, **kwargs: Any):
    policy = Policy(
        is_private_chat=private,
        affection_enabled=affection,
        aliases={"p1": "u1"},
        counterpart="u1" if private else "",
        **kwargs,
    )
    return apply_patch(
        _template(), _channel(), people or {"u1": {"主角信息": {"穿着": "便服", "隐私": "无"}}}, patch, policy
    )


class TestValidator:
    def test_replace_and_delta(self) -> None:
        outcome = _apply(
            [{"op": "replace", "path": "/当前时间", "value": "晚上"}, {"op": "delta", "path": "/血量", "value": 50}]
        )

        assert outcome.channel["当前时间"] == "晚上"
        assert outcome.channel["血量"] == 100  # 截到上限
        assert len(outcome.applied) == 2 and outcome.rejected == []

    def test_original_trees_are_not_touched(self) -> None:
        channel = _channel()
        apply_patch(
            _template(), channel, {}, [{"op": "replace", "path": "/当前时间", "value": "晚上"}], Policy(False, True)
        )

        assert channel["当前时间"] == "早上"

    @pytest.mark.parametrize(
        ("op", "reason"),
        [
            ({"op": "replace", "path": "/_显示用", "value": "y"}, "隐藏"),
            ({"op": "replace", "path": "/世界线", "value": "乙"}, "只读"),
            ({"op": "delta", "path": "/好感度", "value": 5}, "好感"),
            ({"op": "replace", "path": "/秘密", "value": "有"}, "私密"),
            ({"op": "remove", "path": "/必填"}, "必填"),
            ({"op": "move", "from": "/a", "path": "/当前时间"}, "只认"),
            ({"op": "replace", "path": "/不存在", "value": 1}, "模板里没有"),
            ({"op": "replace", "path": "当前时间", "value": "x"}, "/ 开头"),
            ({"op": "replace", "path": "/血量", "value": "很多"}, "数字"),
            ({"op": "replace", "path": "/血量", "value": True}, "数字"),
            ({"op": "replace", "path": "/当前时间"}, "value"),
            ({"op": "delta", "path": "/当前时间", "value": 1}, "数字"),
        ],
    )
    def test_rejections(self, op: dict[str, Any], reason: str) -> None:
        outcome = _apply([op, {"op": "replace", "path": "/当前时间", "value": "夜里"}])

        assert len(outcome.rejected) == 1 and reason in outcome.rejected[0]["reason"]
        assert outcome.channel["当前时间"] == "夜里"  # 被拒的那一处不影响别的处

    def test_affection_is_a_normal_field_when_the_system_is_off(self) -> None:
        outcome = _apply([{"op": "delta", "path": "/好感度", "value": 70}], affection=False)

        assert outcome.channel["好感度"] == [100, "说明"]  # 二元组只改第 0 项，截到上限

    def test_private_fields_only_in_private_chat(self) -> None:
        assert _apply([{"op": "replace", "path": "/秘密", "value": "有"}], private=True).channel["秘密"] == "有"

    def test_extensible_map_fills_from_the_template(self) -> None:
        outcome = _apply([{"op": "replace", "path": "/角色/小林/心情", "value": "开心"}])

        assert outcome.channel["角色"]["小林"] == {"心情": "开心", "等级": 1}

    def test_new_entry_only_takes_template_fields(self) -> None:
        ok = _apply([{"op": "add", "path": "/角色/小林", "value": {"心情": "开心"}}])
        bad = _apply([{"op": "add", "path": "/角色/小林", "value": {"心情": "开心", "后门": "x"}}])

        assert ok.channel["角色"]["小林"] == {"心情": "开心", "等级": 1}
        assert bad.rejected and "小林" not in bad.channel["角色"]

    @pytest.mark.parametrize("key", ["_隐藏", "$meta", "a\nb", "<b>", "{{user}}", "x" * 33])
    def test_bad_new_keys(self, key: str) -> None:
        outcome = _apply([{"op": "replace", "path": f"/角色/{key.replace('/', '~1')}/心情", "value": "开心"}])

        assert outcome.rejected and key not in outcome.channel["角色"]

    def test_strings_are_cleaned_limited_and_filtered(self) -> None:
        cleaned = _apply([{"op": "replace", "path": "/当前时间", "value": "晚\u200b上<b>了</b>\x07"}])
        too_long = _apply([{"op": "replace", "path": "/角色/阿青/心情", "value": "长" * 11}])
        filtered = _apply(
            [{"op": "replace", "path": "/当前时间", "value": "禁词"}],
            memory_filter_hit=lambda text: "禁词" in text,
        )

        assert cleaned.channel["当前时间"] == "晚上了"
        assert too_long.rejected and "10 字" in too_long.rejected[0]["reason"]
        assert filtered.rejected and "过滤" in filtered.rejected[0]["reason"]

    def test_arrays(self) -> None:
        outcome = _apply(
            [
                {"op": "add", "path": "/事件/-", "value": "第二件"},
                {"op": "add", "path": "/事件/-", "value": "第三件"},
                {"op": "add", "path": "/事件/-", "value": "第四件"},
                {"op": "remove", "path": "/事件/0"},
            ],
        )

        assert outcome.channel["事件"] == ["第二件", "第三件"]
        assert len(outcome.rejected) == 1 and "3 条" in outcome.rejected[0]["reason"]

    def test_max_ops(self) -> None:
        patch = [{"op": "replace", "path": "/当前时间", "value": f"第{index}"} for index in range(25)]
        outcome = _apply(patch, max_ops=20)

        assert len(outcome.applied) == 20 and len(outcome.rejected) == 5

    def test_people_in_group_go_through_aliases(self) -> None:
        outcome = _apply([{"op": "replace", "path": f"/{PRESENT_KEY}/p1/主角信息/穿着", "value": "礼服"}])
        unknown = _apply([{"op": "replace", "path": f"/{PRESENT_KEY}/p9/主角信息/穿着", "value": "礼服"}])
        direct = _apply([{"op": "replace", "path": "/主角信息/穿着", "value": "礼服"}])
        private_field = _apply([{"op": "replace", "path": f"/{PRESENT_KEY}/p1/主角信息/隐私", "value": "有"}])

        assert outcome.people["u1"]["主角信息"]["穿着"] == "礼服"
        assert outcome.changed_people == {"u1"}
        assert unknown.rejected and direct.rejected and private_field.rejected

    def test_people_in_private_chat_write_the_counterpart(self) -> None:
        outcome = _apply([{"op": "replace", "path": "/主角信息/隐私", "value": "有"}], private=True)

        assert outcome.people["u1"]["主角信息"]["隐私"] == "有"

    def test_tree_size_limit(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(state_tree, "MAX_TREE_BYTES", state_tree.tree_bytes(_channel()) + 100)

        outcome = _apply(
            [
                {"op": "add", "path": "/事件/-", "value": "x" * 150},
                {"op": "replace", "path": "/当前时间", "value": "晚上"},
            ]
        )

        assert len(outcome.rejected) == 1 and "太大" in outcome.rejected[0]["reason"]
        assert outcome.channel["事件"] == ["开场"]
        assert outcome.channel["当前时间"] == "晚上"

    def test_patch_must_be_a_list(self) -> None:
        outcome = _apply({"op": "replace"})

        assert outcome.rejected and outcome.applied == []


# ---------------------------------------------------------------------------
# 整体写对象、列表元素、二元组：每一层都判权限
# ---------------------------------------------------------------------------


def _nested_template() -> Template:
    return Template(
        fields=(
            FieldDef(path=("状态",), type="object"),
            FieldDef(path=("状态", "地点"), type="string"),
            FieldDef(path=("状态", "世界线"), type="string", readonly=True),
            FieldDef(path=("状态", "体力"), type="number", minimum=0, maximum=10),
            FieldDef(path=("档案",), type="object", pair=True),
            FieldDef(path=("档案", "爱好"), type="string"),
            FieldDef(path=("档案", "私密经历"), type="string"),
            FieldDef(path=("事件",), type="array"),
            FieldDef(path=("标签",), type="array"),
            FieldDef(path=("背包",), type="array", pair=True),
            FieldDef(path=("杂项",), type="object", extensible=True),
            FieldDef(path=("杂项", WILDCARD), type="object", extensible=True),
            FieldDef(path=("等级",), type="number"),
        ),
        private_words=("私密", "秘密"),
    )


def _nested_channel() -> dict[str, Any]:
    return {
        "状态": {"地点": "码头", "世界线": "甲", "体力": 5},
        "档案": [{"爱好": "钓鱼", "私密经历": "不该外传"}, "这个人的档案"],
        "事件": [{"名字": "相遇", "秘密": "不该外传"}],
        "标签": ["甲", "乙"],
        "背包": [["药水"], "随身物品"],
        "杂项": {},
        "等级": 1,
    }


def _apply_nested(patch: Any, *, private: bool = False, **kwargs: Any) -> Any:
    policy = Policy(is_private_chat=private, affection_enabled=False, **kwargs)
    return apply_patch(_nested_template(), _nested_channel(), {}, patch, policy)


class TestWholeValues:
    @pytest.mark.parametrize(
        ("op", "reason"),
        [
            # 换整个父对象绕过子字段的只读
            ({"op": "replace", "path": "/状态", "value": {"地点": "屋顶", "世界线": "乙", "体力": 5}}, "受保护"),
            ({"op": "remove", "path": "/状态"}, "受保护"),
            # 子字段的类型、范围照样判
            ({"op": "replace", "path": "/杂项", "value": {}}, ""),
            ({"op": "replace", "path": "/状态/地点", "value": {"x": 1}}, "文字"),
            # 模板没有的键（父级不可扩展）
            ({"op": "add", "path": "/状态/后门", "value": "x"}, "模板里没有"),
        ],
    )
    def test_parent_objects_do_not_bypass_children(self, op: dict[str, Any], reason: str) -> None:
        outcome = _apply_nested([op])

        if reason:
            assert outcome.rejected and reason in outcome.rejected[0]["reason"]
            assert outcome.channel["状态"]["世界线"] == "甲"

    def test_object_values_are_checked_per_child(self) -> None:
        typed = _apply_nested([{"op": "replace", "path": "/杂项", "value": {"新": {"体力": "很多"}}}])
        nested = Template(
            fields=(
                FieldDef(path=("状态",), type="object"),
                FieldDef(path=("状态", "体力"), type="number", minimum=0, maximum=10),
                FieldDef(path=("状态", "地点"), type="string"),
            ),
        )
        clamped = apply_patch(
            nested,
            {"状态": {"体力": 1, "地点": "码头"}},
            {},
            [{"op": "replace", "path": "/状态", "value": {"体力": 99, "地点": "屋顶"}}],
            Policy(False, False),
        )
        unknown = apply_patch(
            nested,
            {"状态": {"体力": 1, "地点": "码头"}},
            {},
            [{"op": "replace", "path": "/状态", "value": {"体力": 1, "后门": "x"}}],
            Policy(False, False),
        )

        assert typed.applied  # 杂项下面没有定义，值按自由值收（文字可以）
        assert clamped.channel["状态"] == {"体力": 10, "地点": "屋顶"}  # 范围按子字段截
        assert unknown.rejected and "模板没有" in unknown.rejected[0]["reason"]

    def test_private_children_inside_a_pair_wrapped_object(self) -> None:
        group = _apply_nested(
            [{"op": "replace", "path": "/档案", "value": [{"爱好": "下棋", "私密经历": "改掉"}, "x"]}]
        )
        own_chat = _apply_nested(
            [{"op": "replace", "path": "/档案", "value": [{"爱好": "下棋", "私密经历": "改掉"}, "x"]}], private=True
        )

        assert group.rejected and group.channel["档案"][0]["私密经历"] == "不该外传"
        assert own_chat.channel["档案"] == [{"爱好": "下棋", "私密经历": "改掉"}, "这个人的档案"]  # 说明不变

    def test_private_keys_inside_list_elements(self) -> None:
        added = _apply_nested([{"op": "add", "path": "/事件/-", "value": {"名字": "离别", "秘密": "写进去"}}])
        replaced = _apply_nested([{"op": "replace", "path": "/事件/0", "value": {"名字": "重逢"}}])
        whole = _apply_nested([{"op": "replace", "path": "/事件", "value": []}])
        plain = _apply_nested([{"op": "add", "path": "/事件/-", "value": {"名字": "离别"}}])

        assert added.rejected and "私密" in added.rejected[0]["reason"]
        assert replaced.rejected and whole.rejected  # 元素里有群聊里碰不得的键，不能整个换掉
        assert plain.channel["事件"][-1] == {"名字": "离别"}

    def test_new_keys_with_private_words_are_private(self) -> None:
        outcome = _apply_nested([{"op": "add", "path": "/杂项/小林", "value": {"私密日记": "x"}}])

        assert outcome.rejected and "私密" in outcome.rejected[0]["reason"]

    @pytest.mark.parametrize(
        "patch",
        [
            [{"op": "add", "path": "/杂项/坏\ud800", "value": {}}],
            [{"op": "add", "path": "/杂项/小林", "value": {"坏\udc00": 1}}],
            [{"op": "add", "path": "/杂项/左⁧右", "value": {}}],
        ],
        ids=["lone-surrogate-key", "lone-surrogate-inner-key", "bidi-isolate-key"],
    )
    def test_new_keys_reject_lone_surrogates_and_bidi_isolates(self, patch: list[dict[str, Any]]) -> None:
        outcome = _apply_nested(patch)

        assert not outcome.applied and "新键名不合法" in outcome.rejected[0]["reason"]
        assert outcome.channel["杂项"] == {}
        # 被拒的记录本身要写得进库：路径里的孤立代理字符已经去掉
        assert str(outcome.rejected).encode("utf-8")

    def test_an_unexpected_error_rejects_only_that_op(self, monkeypatch: pytest.MonkeyPatch) -> None:
        real = validator._apply_one  # noqa: SLF001

        def flaky(raw_op: Any, checker: Any, outcome: Any) -> Any:
            if raw_op.get("path") == "/状态/体力":
                outcome.channel["状态"]["体力"] = 9  # 改到一半出错：这一处要整体退回
                raise RuntimeError("意外")
            return real(raw_op, checker, outcome)

        monkeypatch.setattr(validator, "_apply_one", flaky)
        outcome = _apply_nested(
            [
                {"op": "replace", "path": "/状态/体力", "value": 3},
                {"op": "replace", "path": "/状态/地点", "value": "屋顶"},
            ]
        )

        assert outcome.rejected == [{"op": "replace", "path": "/状态/体力", "reason": "这一处出错（RuntimeError）"}]
        assert [item["path"] for item in outcome.applied] == ["/状态/地点"]
        assert outcome.channel["状态"]["体力"] == 5 and outcome.channel["状态"]["地点"] == "屋顶"

    def test_runtime_words_only_tighten(self) -> None:
        outcome = _apply_nested([{"op": "replace", "path": "/状态/地点", "value": "屋顶"}], private_words=("地点",))

        assert outcome.rejected and "私密" in outcome.rejected[0]["reason"]

    def test_new_keys_and_free_values_pass_the_memory_filter(self) -> None:
        hit = lambda text: "禁词" in text  # noqa: E731
        key = _apply_nested([{"op": "add", "path": "/杂项/禁词", "value": {}}], memory_filter_hit=hit)
        inner_key = _apply_nested([{"op": "add", "path": "/杂项/小林", "value": {"禁词": 1}}], memory_filter_hit=hit)
        inner_value = _apply_nested(
            [{"op": "add", "path": "/杂项/小林", "value": {"a": "禁词"}}], memory_filter_hit=hit
        )

        assert key.rejected and inner_key.rejected and inner_value.rejected

    def test_two_item_arrays_are_pairs_only_when_the_template_says_so(self) -> None:
        outcome = _apply_nested([{"op": "replace", "path": "/标签", "value": ["丙", "丁"]}])

        assert outcome.channel["标签"] == ["丙", "丁"]

    @pytest.mark.parametrize("path", ["/背包/1", "/背包/0"])
    def test_no_element_ops_on_a_pair_wrapped_array(self, path: str) -> None:
        outcome = _apply_nested([{"op": "replace", "path": path, "value": 123}])

        assert outcome.rejected and outcome.channel["背包"] == [["药水"], "随身物品"]

    def test_numbers_must_stay_finite(self) -> None:
        overflow = _apply_nested(
            [{"op": "delta", "path": "/等级", "value": 1e308}, {"op": "delta", "path": "/等级", "value": 1e308}]
        )
        infinite = _apply_nested([{"op": "replace", "path": "/等级", "value": float("inf")}])
        huge = _apply_nested([{"op": "replace", "path": "/等级", "value": 10**400}])

        assert len(overflow.applied) == 1 and "有限" in overflow.rejected[0]["reason"]
        assert infinite.rejected and huge.rejected and huge.channel["等级"] == 1


# ---------------------------------------------------------------------------
# 二元组：包着对象时的整体写与逐字段写、子字段两种写法都收
# ---------------------------------------------------------------------------


class TestPairs:
    def test_a_pair_wrapped_object_cannot_be_replaced_around_its_protected_children(self) -> None:
        dropped = _apply_nested([{"op": "replace", "path": "/档案", "value": {"爱好": "下棋"}}])
        added = _apply_nested([{"op": "add", "path": "/档案", "value": [{"爱好": "下棋"}, "x"]}])

        assert dropped.rejected and "受保护" in dropped.rejected[0]["reason"]  # 新值里省掉了私密子字段
        assert added.rejected and dropped.channel["档案"][0]["私密经历"] == "不该外传"

    def test_new_values_cannot_bring_in_protected_children(self) -> None:
        channel = _nested_channel()
        channel["档案"] = [{"爱好": "钓鱼"}, "这个人的档案"]  # 现有值里没有私密子字段
        policy = Policy(is_private_chat=False, affection_enabled=False)

        outcome = apply_patch(
            _nested_template(),
            channel,
            {},
            [{"op": "replace", "path": "/档案", "value": {"爱好": "下棋", "私密经历": "群里写进去"}}],
            policy,
        )

        assert outcome.rejected and "私密" in outcome.rejected[0]["reason"]
        assert outcome.channel["档案"] == [{"爱好": "钓鱼"}, "这个人的档案"]

    def test_fields_of_a_pair_wrapped_object_can_be_written_one_by_one(self) -> None:
        outcome = _apply_nested([{"op": "replace", "path": "/档案/爱好", "value": "下棋"}])
        index = _apply_nested([{"op": "replace", "path": "/档案/0/爱好", "value": "下棋"}])
        note = _apply_nested([{"op": "replace", "path": "/档案/1", "value": "改说明"}])
        private = _apply_nested([{"op": "replace", "path": "/档案/私密经历", "value": "改掉"}])

        assert outcome.channel["档案"] == [{"爱好": "下棋", "私密经历": "不该外传"}, "这个人的档案"]
        assert index.rejected and note.rejected and private.rejected
        assert note.channel["档案"][1] == "这个人的档案"

    def test_profile_roots_that_are_pairs(self) -> None:
        template = Template(
            fields=(
                FieldDef(path=("主角信息",), type="object", person=True, pair=True),
                FieldDef(path=("主角信息", "穿着"), type="string"),
                FieldDef(path=("主角信息", "隐私"), type="string", private=True),
            ),
        )
        people = {"u1": {"主角信息": [{"穿着": "便服", "隐私": "私聊里写的"}, "主角"]}}

        def run(path: str, value: Any, private: bool) -> Any:
            policy = Policy(private, False, aliases={"p1": "u1"}, counterpart="u1" if private else "")
            return apply_patch(template, {}, people, [{"op": "replace", "path": path, "value": value}], policy)

        wiped = run(f"/{PRESENT_KEY}/p1/主角信息", {"穿着": "礼服"}, False)
        one_field = run(f"/{PRESENT_KEY}/p1/主角信息/穿着", "礼服", False)

        assert wiped.rejected and wiped.people["u1"]["主角信息"][0]["隐私"] == "私聊里写的"
        assert one_field.people["u1"]["主角信息"] == [{"穿着": "礼服", "隐私": "私聊里写的"}, "主角"]

    def test_whole_writes_keep_required_keys(self) -> None:
        template = Template(
            fields=(
                FieldDef(path=("状态",), type="object"),
                FieldDef(path=("状态", "地点"), type="string", required=True),
                FieldDef(path=("状态", "心情"), type="string"),
            ),
        )
        policy = Policy(False, False)
        dropped = apply_patch(
            template,
            {"状态": {"地点": "码头", "心情": "好"}},
            {},
            [{"op": "replace", "path": "/状态", "value": {"心情": "坏"}}],
            policy,
        )
        kept = apply_patch(
            template,
            {"状态": {"地点": "码头", "心情": "好"}},
            {},
            [{"op": "replace", "path": "/状态", "value": {"地点": "屋顶", "心情": "坏"}}],
            policy,
        )

        assert dropped.rejected and "必填" in dropped.rejected[0]["reason"]
        assert kept.channel["状态"] == {"地点": "屋顶", "心情": "坏"}

    def _roles(self) -> tuple[Template, dict[str, Any]]:
        tree = {
            "角色": {
                "$meta": {"extensible": True},
                "阿青": {"心情": ["平静", "当前心情"], "等级": [1, "等级"]},
            },
        }
        return initvar.derive_template(tree)

    def _apply_roles(self, patch: list[dict[str, Any]]) -> Any:
        template, channel = self._roles()
        return apply_patch(template, channel, {}, patch, Policy(False, False))

    def test_a_new_entry_takes_bare_values_and_pairs(self) -> None:
        outcome = self._apply_roles(
            [
                {"op": "add", "path": "/角色/小林", "value": {"心情": "开心"}},
                {"op": "add", "path": "/角色/小红", "value": {"心情": ["难过", "模型给的说明"], "等级": [2, "x"]}},
            ]
        )

        assert outcome.rejected == []
        assert outcome.channel["角色"]["小林"] == {"心情": ["开心", "当前心情"], "等级": [1, "等级"]}
        assert outcome.channel["角色"]["小红"] == {"心情": ["难过", "当前心情"], "等级": [2, "等级"]}

    def test_whole_and_single_writes_keep_pairs(self) -> None:
        outcome = self._apply_roles(
            [
                {"op": "replace", "path": "/角色/阿青", "value": {"心情": "生气", "等级": [3, "x"]}},
                {"op": "replace", "path": "/角色/阿青/等级", "value": 4},
                {"op": "replace", "path": "/角色/新人/心情", "value": "紧张"},
            ]
        )

        assert outcome.rejected == []
        assert outcome.channel["角色"]["阿青"] == {"心情": ["生气", "当前心情"], "等级": [4, "等级"]}
        assert outcome.channel["角色"]["新人"] == {"心情": ["紧张", "当前心情"], "等级": [1, "等级"]}

    def test_pair_wrapped_entries_of_an_extensible_map(self) -> None:
        tree = {"角色": {"$meta": {"extensible": True}, "阿青": [{"心情": "平静"}, "一个角色"]}}
        template, channel = initvar.derive_template(tree)

        outcome = apply_patch(
            template,
            channel,
            {},
            [
                {"op": "replace", "path": "/角色/阿青/心情", "value": "开心"},
                {"op": "add", "path": "/角色/小林", "value": {"心情": "紧张"}},
            ],
            Policy(False, False),
        )

        assert outcome.rejected == []
        assert outcome.channel["角色"]["阿青"] == [{"心情": "开心"}, "一个角色"]
        assert outcome.channel["角色"]["小林"] == [{"心情": "紧张"}, "一个角色"]


class TestListElements:
    def test_only_elements_that_hold_private_values_are_locked(self) -> None:
        template = Template(
            fields=(
                FieldDef(path=("事件",), type="array", max_items=2),
                FieldDef(path=("事件", WILDCARD, "秘密"), type="string", private=True),
            ),
        )
        channel = {"事件": [{"名字": "相遇"}, {"名字": "约定", "秘密": "不该外传"}]}

        def run(op: dict[str, Any]) -> Any:
            return apply_patch(template, channel, {}, [op], Policy(False, False))

        assert run({"op": "remove", "path": "/事件/0"}).channel["事件"] == [{"名字": "约定", "秘密": "不该外传"}]
        assert run({"op": "replace", "path": "/事件/0", "value": {"名字": "重逢"}}).channel["事件"][0] == {
            "名字": "重逢"
        }
        assert run({"op": "remove", "path": "/事件/1"}).rejected
        assert run({"op": "replace", "path": "/事件/1", "value": {"名字": "x"}}).rejected


class TestStorableTemplates:
    def test_keys_the_template_cannot_store_are_dropped(self) -> None:
        deep: dict[str, Any] = {"值": 1}
        for index in range(20):
            deep = {f"层{index}": deep}
        tree = {"长" * 65: 1, "": 2, "深": deep, "正常": 3}

        template, cleaned = initvar.derive_template(tree)

        assert Template.from_json(template.to_json()) == template  # 存下的模板读得回来
        assert set(cleaned) == {"深", "正常"}
        assert all(len(item.path) <= 16 for item in template.fields)

    def test_meta_inside_a_pair_is_read_not_kept(self) -> None:
        tree = {"档案": [{"$meta": {"extensible": True}, "名字": "甲"}, "人物档案"]}

        template, cleaned = initvar.derive_template(tree)

        assert all("$meta" not in item.path and "extensible" not in item.path for item in template.fields)
        profile = template.match(("档案",))
        assert profile is not None and profile.pair and profile.extensible
        entry = template.match(("档案", "新键"))
        assert entry is not None and entry.type == "string"  # 通配定义照「名字」推，不是照 $meta
        assert cleaned == {"档案": [{"名字": "甲"}, "人物档案"]}

    def test_match_prefers_exact_segments_and_fewest_wildcards(self) -> None:
        template = Template(
            fields=(
                FieldDef(path=("角色", WILDCARD, "心情"), type="string"),
                FieldDef(path=("角色", "阿青", WILDCARD), type="number"),
                FieldDef(path=(WILDCARD, WILDCARD, WILDCARD), type="boolean"),
                FieldDef(path=("角色", "阿青", "心情"), type="object"),
            ),
        )

        assert template.match(("角色", "阿青", "心情")).type == "object"  # type: ignore[union-attr]
        assert template.match(("角色", "阿青", "等级")).type == "number"  # type: ignore[union-attr]
        assert template.match(("角色", "小林", "心情")).type == "string"  # type: ignore[union-attr]
        assert template.match(("别的", "小林", "心情")).type == "boolean"  # type: ignore[union-attr]
        assert template.match(("角色", "小林")) is None
