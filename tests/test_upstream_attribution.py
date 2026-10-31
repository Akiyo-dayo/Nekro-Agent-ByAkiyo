"""「基于 NekroAgent 项目构建」标识（审计 P1：LICENSE 要求在界面里以可点击超链接标明）

LICENSE（"Nekro Agent 开源协议 V1.1"）要求：

- 不得移除或修改项目的版权声明、商标信息或归属标识；
- 所有图形界面必须以清晰可见、可访问的**超链接**保留项目来源；
- 必须标明「基于 NekroAgent 项目构建」并提供项目地址，**字体不小于界面主文字的 80%**，颜色对比度可读。

界面页脚此前只有 `© NekroAgent`，没有「基于……构建」字样。这里把修复钉住。`/info` 命令的作者行是所有者决定保留 fork 作者，不在此列。这份用例在每个分发版本里都必须通过：界面源码里不能依赖只在某一个版本里才有的东西。
"""

import json
import re
from pathlib import Path
from typing import Any, Dict

import pytest

REPO_ROOT = Path(__file__).parent.parent
UPSTREAM = "https://github.com/KroMiose/nekro-agent"
FRONTEND = REPO_ROOT / "frontend" / "src"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


class TestChatCommandsSayWhatTheyAreBasedOn:
    """`na_info` / `na_help` 的输出里各有一行“基于 NekroAgent”的说明。

    作者行用 fork 作者是**所有者的决定**（见 DECISIONS.md「`/info` 署名由所有者决定」），这里不要求恢复上游作者与链接，
    只钉住“基于 NekroAgent”这一行不被删掉。
    """

    async def test_na_info_output(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from types import SimpleNamespace

        from nekro_agent.models import db_chat_channel
        from nekro_agent.services.command.built_in import info
        from nekro_agent.services.command.schemas import CommandExecutionContext
        from nekro_agent.tools.common_util import get_app_version

        async def fake_preset() -> Any:
            return SimpleNamespace(name="示例人设")

        channel = SimpleNamespace(channel_status=SimpleNamespace(value="active"), workspace_id=None, get_preset=fake_preset)

        async def fake_get_channel(*_: Any, **__: Any) -> Any:
            return channel

        async def fake_sandbox(*_: Any, **__: Any) -> Any:
            return ("n/a", list(info.WorkspaceSandboxDisplayStatus)[0])

        monkeypatch.setattr(db_chat_channel.DBChatChannel, "get_channel", staticmethod(fake_get_channel))
        monkeypatch.setattr(info, "_resolve_bound_workspace_sandbox_status", fake_sandbox)
        monkeypatch.setattr(info, "_resolve_channel_agent_runtime_status", lambda *_a, **_k: ("idle", "idle"))

        context = CommandExecutionContext(user_id="u", chat_key="onebot_v11-inst1-group_2000000001", username="t", adapter_key="onebot_v11")
        response = await info.NaInfoCommand().execute(context)
        assert any("NekroAgent" in line and get_app_version() in line for line in str(response.message).splitlines())

    async def test_na_help_output(self) -> None:
        from nekro_agent.services.command.built_in import info
        from nekro_agent.services.command.schemas import CommandExecutionContext
        from nekro_agent.tools.common_util import get_app_version

        context = CommandExecutionContext(user_id="u", chat_key="onebot_v11-inst1-group_2000000001", username="t", adapter_key="onebot_v11")
        response = await info.NaHelpCommand().execute(context)
        assert any("based on NekroAgent" in line and get_app_version() in line for line in str(response.message).splitlines())


FOOTERS = {
    "layouts/MainLayout.tsx": ("navigation", "footer.builtOnPrefix", "footer.builtOnSuffix"),
    "pages/login/index.tsx": ("login", "footer.builtOnPrefix", "footer.builtOnSuffix"),
}


@pytest.mark.parametrize("rel", sorted(FOOTERS))
class TestUiFootersCarryTheBuiltOnNotice:
    def _block(self, rel: str) -> str:
        """从 builtOnPrefix 往前找到它所在的 <Typography …> 开头，往后到 builtOnSuffix 之后的 </Typography>"""
        text = _read(FRONTEND / rel)
        prefix = text.index("footer.builtOnPrefix")
        start = text.rindex("<Typography", 0, prefix)
        end = text.index("</Typography>", prefix)
        return text[start:end]

    def test_it_links_to_the_upstream_project_and_is_clickable(self, rel: str) -> None:
        block = self._block(rel)
        assert f'href="{UPSTREAM}"' in block
        assert "<Link" in block and "NekroAgent" in block
        assert 'target="_blank"' in block and 'rel="noopener noreferrer"' in block
        assert "footer.builtOnSuffix" in block

    def test_the_font_is_not_smaller_than_80_percent_of_the_main_text(self, rel: str) -> None:
        """主文字最大按 1rem 算：附注字号 >= 0.8rem 才满足“不小于 80%”；`caption`（0.75rem）不够"""
        block = self._block(rel)
        assert 'variant="caption"' not in block, "caption 是 0.75rem，小于 1rem 主文字的 80%"
        sizes = re.findall(r"fontSize:\s*'([0-9.]+)rem'", block)
        assert sizes, "必须显式给出字号"
        assert all(float(s) >= 0.8 for s in sizes), sizes

    def test_it_is_not_dimmed_by_an_opacity_of_its_own(self, rel: str) -> None:
        """颜色对比度要可读：标识自己不加 opacity（它所在的容器是否带 opacity 靠评审 / 截图确认，登录页的版权行容器有 opacity 0.8，所以标识放在它外面）"""
        assert "opacity" not in self._block(rel)

    def test_the_copyright_line_is_still_there(self, rel: str) -> None:
        text = _read(FRONTEND / rel)
        assert re.search(r"©\s*\{new Date\(\)\.getFullYear\(\)\}", text)
        assert f'href="{UPSTREAM}"' in text


@pytest.mark.parametrize(("rel", "namespace"), [("layouts/MainLayout.tsx", "navigation"), ("pages/login/index.tsx", "login")])
class TestTheNoticeGoesThroughI18n:
    @staticmethod
    def _locale(lang: str, namespace: str) -> Dict[str, Any]:
        return json.loads(_read(FRONTEND / "locales" / lang / f"{namespace}.json"))

    def test_both_languages_have_the_two_halves(self, rel: str, namespace: str) -> None:
        for lang in ("zh-CN", "en-US"):
            footer = self._locale(lang, namespace)["footer"]
            assert footer["builtOnPrefix"].strip() and footer["builtOnSuffix"].strip(), (lang, namespace)

    def test_the_rendered_sentences_are_what_the_license_asks_for(self, rel: str, namespace: str) -> None:
        zh = self._locale("zh-CN", namespace)["footer"]
        en = self._locale("en-US", namespace)["footer"]
        assert f'{zh["builtOnPrefix"]}NekroAgent{zh["builtOnSuffix"]}'.replace(" ", "") == "基于NekroAgent项目构建"
        assert f'{en["builtOnPrefix"]}NekroAgent{en["builtOnSuffix"]}' == "Built on the NekroAgent project"

    def test_the_component_reads_the_same_keys_from_the_right_namespace(self, rel: str, namespace: str) -> None:
        text = _read(FRONTEND / rel)
        if namespace == "navigation":
            assert "t('footer.builtOnPrefix', { ns: 'navigation' })" in text
            assert "t('footer.builtOnSuffix', { ns: 'navigation' })" in text
        else:
            assert "t('footer.builtOnPrefix')" in text and "t('footer.builtOnSuffix')" in text
