"""排查/问答 system prompt 的受众与排查方向单元测试

背景：机器人主要给交付、运维排查问题。标准版产品 bug 基本已及时修复，
用户问题绝大多数是配置、数据、环境或咨询问题。

复现的真实反例：用户问 /ts-service/court/list/tree 空指针，AI 正确查到根因是
ifc_court_info 表缺少河南省法院数据（数据问题），但回复大段讲代码第几行做了什么，
第 1 条修复建议是"修复代码防护"并贴 Java 补丁。交付/运维看不懂也改不了代码。

根因在提示词：角色是"定位代码问题"，工作流程要求"代码定位（文件:行号）、修复建议"，
注意事项要求"代码定位要精确到文件名和行号"，模型被一路引向代码缺陷。

这里驱动真实路径、捕获传给 client.messages.create 的 system 参数，
断言新指引存在、旧的冲突语句不在，防止有人把旧句子改回来。

桩模块只挂导入真正需要的 anthropic（provider 顶层导入）和 paramiko
（Agentic 路径延迟导入 git_tools → tools/__init__ → operations_commands）。
用 fixture + monkeypatch 限定在本文件的测试执行期间，不在模块顶层挂：
sys.modules 是进程级的，顶层挂桩会让后收集的测试文件拿到假模块，改变全量结果。
"""

import importlib
import sys
import types
from datetime import datetime
from unittest.mock import Mock

import pytest


def _stub_module(name: str) -> types.ModuleType:
    stub = types.ModuleType(name)
    stub.__getattr__ = lambda _attr: Mock()  # type: ignore[attr-defined]
    return stub


@pytest.fixture
def provider_and_calls(monkeypatch):
    """
    构造 provider，messages.create 记录入参并直接返回 end_turn 的文本回复

    依赖未安装时临时挂桩；测试结束后撤掉桩，并移除本次新导入的 ai_assistant 模块，
    避免它们持有对桩模块的引用、影响后续测试。
    """
    for name in ("anthropic", "paramiko"):
        try:
            importlib.import_module(name)
        except ImportError:
            monkeypatch.setitem(sys.modules, name, _stub_module(name))

    preloaded = set(sys.modules)
    from ai_assistant.core.models import Content, Message
    from ai_assistant.providers.anthropic_provider import AnthropicProvider

    provider = AnthropicProvider(api_key="test-key")
    calls = []

    def _create(**kwargs):
        calls.append(kwargs)
        return Mock(
            stop_reason="end_turn",
            content=[Mock(type="text", text="结论：数据问题")],
            usage=Mock(input_tokens=1, output_tokens=1),
        )

    provider.client = Mock()
    provider.client.messages.create = _create
    # 生产环境 Agentic 模式总带着 Git 工具；不设的话无工具时会改走标准模式，
    # 捕获到的就成了 RAG 提示词。模型直接 end_turn，工具不会被真正调用。
    provider.git_tools = Mock()
    provider.git_tools_enabled = True

    def user_msg(text: str):
        return Message(
            role="user",
            content=[Content(type="text", data=text)],
            timestamp=datetime.now(),
            metadata={},
        )

    yield provider, calls, user_msg

    for name in set(sys.modules) - preloaded:
        if name.startswith("ai_assistant"):
            sys.modules.pop(name, None)


def test_agentic_system_prompt_prefers_config_data_and_ops_audience(provider_and_calls):
    """Agentic 排查提示词：先怀疑配置/数据，回复面向交付运维，旧的代码导向语句已移除"""
    provider, calls, user_msg = provider_and_calls

    _, metadata = provider._send_with_context_agentic(
        [user_msg("/ts-service/court/list/tree 接口空指针")],
        doc_context="",
        timeout_mode="rounds",
        max_rounds=1,
    )

    assert metadata["mode"] == "agentic"
    system = calls[0]["system"]

    # 排查方向的先验
    assert "绝大多数是配置、数据、环境或用法问题" in system
    assert "只有排除了以上原因、并且有代码证据时，才下\"代码 bug\"的结论" in system
    # 版本与数据库方言视角（绑条目前缀，避免被其他段落的同类词蒙过）
    assert "· 版本：产品版本与配置/脚本不匹配，升级后漏执行数据库升级脚本" in system
    assert "· 数据库方言/版本：现场数据库有达梦（DM）、MySQL、人大金仓（Kingbase）" in system
    assert "给 SQL 时注明适用哪种库" in system
    # 代码改动时间判据：带当天日期，老代码优先查现场；确认 bug 转研发
    assert f"看报错处代码的最后改动时间（今天是 {datetime.now().strftime('%Y-%m-%d')}）" in system
    assert "3 个月以上没改过：这段代码已在各现场稳定运行，大概率不是代码问题" in system
    assert "排查建议必须结合现场日志：指出日志里哪一行、哪个参数值说明了问题" in system
    assert "结论写明\"疑似代码问题，请联系研发处理\"" in system
    # 读代码是手段
    assert "读代码是为了查清是哪个配置项、哪张表、哪条数据导致的" in system
    # 回复结构：伪代码展示取数逻辑、给现场查询 SQL
    assert "回复写给交付、运维看" in system
    assert "用 3~8 行伪代码展示程序按什么条件取数据" in system
    # 绑定结构说明那一句；"请在现场执行以下 SQL 并反馈结果"在示例里也出现，单独断言它是假绿灯
    assert "查询 SQL：根据读过的配置和代码给出查询 SQL，写成\"请在现场执行以下 SQL 并反馈结果\"" in system
    # SQL 安全
    assert "用于确认现状的 SQL 只写 SELECT" in system
    assert "必须单独标明\"修改操作\"，并提示先备份、确认是正确的环境再执行" in system
    assert "表名、字段名必须来自读过的代码或配置，不能猜" in system
    # 代码篇幅
    assert "不要大段贴 Java 代码或代码补丁" in system
    assert "不要把\"改代码\"当成首选处理建议" in system
    assert "不要在回复里单独列文件名和行号" in system
    # 研发明确要代码时的例外，防止矫枉过正
    assert "用户明确要求看代码或代码细节时" in system

    # 旧的冲突语句、已撤销的"供研发参考"段不得再出现
    for old in (
        "你是一个智能代码排查助手",
        "给出根因分析、代码定位（文件:行号）、修复建议",
        "给结论时要落到具体文件:行号",
        "+\"怎么改\"）就停",
        "代码定位要精确到文件名和行号",
        "供研发参考",
    ):
        assert old not in system, ascii(old)


def test_rag_system_prompt_targets_ops_audience(provider_and_calls):
    """RAG 问答提示词：咨询类问题面向交付运维回答，不大段贴代码"""
    provider, calls, user_msg = provider_and_calls

    _, metadata = provider._send_with_context_standard(
        [user_msg("法院基础数据怎么初始化")],
        doc_context="",
    )

    assert metadata["mode"] == "rag"
    system = calls[0]["system"]
    # 与 OpenAI 通道共用同一常量，防止两边文案漂移
    from ai_assistant.core.ai_provider import ANSWER_PERSPECTIVE_PROMPT
    assert ANSWER_PERSPECTIVE_PROMPT in system
    assert "提问的多是交付、运维人员，问题大多是配置、版本、数据或使用咨询" in system
    assert "不要轻易归结为产品 bug" in system
    assert "不要大段贴代码；用户明确要求看代码时可按需给出" in system
