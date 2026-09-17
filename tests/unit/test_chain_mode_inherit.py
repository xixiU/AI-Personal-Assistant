"""
引用链模式继承测试

背景 bug：/查代码 后引用追问，若追问文本不含触发词，会掉回普通 RAG 模式，
不再调用 git 工具。根因是 _should_use_agentic_mode 只看 _extract_last_user_text
（末尾那条 user 消息），看不到引用链根消息里的 /查代码。
早期版本用"会话级粘性"解决，但粘性绑 session_id（群 chat_id）造成跨用户污染，
被删除后替代方案（引用链继承）一直没补上。

核心规则（回归重点）：
1. 当前消息有触发词 → 以当前消息为准（可双向切换 agentic ↔ operations）
2. 当前消息无触发词 → 继承引用链里最近一次用过的模式
3. 纯 RAG 链不会被误升级；无引用的全新对话不继承任何模式
4. 回溯拉来的历史图片不触发排查模式（否则群里无关截图会污染普通提问）
"""
import sys
import types
from unittest.mock import Mock

import pytest

# anthropic SDK 可能未安装（本项目按规范只做静态测试，不要求装齐运行时依赖），
# 注入最小桩模块，保证被测的模式判定逻辑可独立测试。
if "anthropic" not in sys.modules:
    _stub = types.ModuleType("anthropic")

    class _APIError(Exception):
        pass

    class _APIStatusError(_APIError):
        def __init__(self, message, status_code=500, response=None):
            super().__init__(message)
            self.message = message
            self.status_code = status_code
            self.response = response

    _stub.APIError = _APIError
    _stub.APITimeoutError = _APIError
    _stub.APIConnectionError = _APIError
    _stub.APIStatusError = _APIStatusError
    _stub.Anthropic = lambda **kw: Mock(base_url=kw.get("base_url"), messages=Mock())
    sys.modules["anthropic"] = _stub

from ai_assistant.core.models import Content, Message
from ai_assistant.providers.anthropic_provider import AnthropicProvider


@pytest.fixture
def provider():
    """两种工具模式都启用的 provider"""
    p = AnthropicProvider(api_key="k", base_url="http://fake", model="m")
    p.git_tools = Mock()
    p.git_tools_enabled = True
    p.operations_tools = Mock()
    p.operations_enabled = True
    return p


def _user(text):
    return Message(role="user", content=[Content(type="text", data=text)], timestamp=None)


def _assistant(text, mode):
    return Message(
        role="assistant",
        content=[Content(type="text", data=text)],
        timestamp=None,
        metadata={"mode": mode},
    )


def _resolve(provider, messages):
    """复刻 _send_with_context 的判定顺序，返回最终模式名"""
    if provider._has_explicit_operations_command(messages):
        return "operations"
    if provider._should_use_agentic_mode(messages):
        return "agentic"
    if provider._should_use_operations_mode(messages):
        return "operations"
    return "rag"


# ============ 核心场景：无触发词的引用追问应继承 ============

def test_agentic_chain_inherited_without_trigger(provider):
    """回归：/查代码 根消息 + 无触发词引用追问 → 仍走 Agentic"""
    msgs = [
        _user("/查代码 补充材料特定角色不显示二维码是什么原因"),
        _assistant("配置项在 xxx.yaml", "agentic"),
        _user("那这个配置默认值是多少"),
    ]
    assert _resolve(provider, msgs) == "agentic"


def test_operations_chain_inherited_without_trigger(provider):
    """/运维 链的无触发词引用追问 → 仍走 Operations"""
    msgs = [
        _user("/运维 查服务状态"),
        _assistant("服务正常", "operations"),
        _user("那内存占用呢"),
    ]
    assert _resolve(provider, msgs) == "operations"


def test_multi_turn_chain_inherits_from_latest(provider):
    """多轮链取最近一次模式：链上从排查转到运维后，应继承运维"""
    msgs = [
        _user("/查代码 xx"),
        _assistant("代码在 A.java", "agentic"),
        _user("/运维 重启下服务"),
        _assistant("已重启", "operations"),
        _user("现在状态怎么样"),
    ]
    assert _resolve(provider, msgs) == "operations"


# ============ 优先级：当前消息胜过继承 ============

def test_current_trigger_beats_inherited_operations(provider):
    """运维链中显式写 /查代码 → 必须切到 Agentic，不能被运维链吃掉"""
    msgs = [
        _user("/运维 查状态"),
        _assistant("正常", "operations"),
        _user("/查代码 这个状态码在哪定义的"),
    ]
    assert _resolve(provider, msgs) == "agentic"


def test_current_trigger_beats_inherited_agentic(provider):
    """排查链中显式写 /运维 → 切到 Operations"""
    msgs = [
        _user("/查代码 xx"),
        _assistant("代码在...", "agentic"),
        _user("/运维 重启服务"),
    ]
    assert _resolve(provider, msgs) == "operations"


def test_rag_chain_upgraded_by_explicit_trigger(provider):
    """纯 RAG 链在引用时显式带触发词 → 升级到 Agentic"""
    msgs = [
        _user("需求文档写了什么"),
        _assistant("文档说...", "rag"),
        _user("/查代码 那代码里怎么实现的"),
    ]
    assert _resolve(provider, msgs) == "agentic"


# ============ 不应误触发 ============

def test_rag_chain_not_upgraded_without_trigger(provider):
    """纯 RAG 链无触发词 → 保持 RAG，不误升级"""
    msgs = [
        _user("需求文档写了什么"),
        _assistant("文档说...", "rag"),
        _user("再详细点"),
    ]
    assert _resolve(provider, msgs) == "rag"


def test_new_conversation_inherits_nothing(provider):
    """无引用的全新对话 → RAG"""
    assert _resolve(provider, [_user("你好")]) == "rag"


def test_inherit_ignores_assistant_without_mode(provider):
    """历史 assistant 无 metadata.mode（旧数据）→ 不继承，不报错"""
    msgs = [
        _user("问题"),
        Message(role="assistant", content=[Content(type="text", data="回答")], timestamp=None),
        _user("追问"),
    ]
    assert _resolve(provider, msgs) == "rag"


def test_inherit_disabled_when_tools_off():
    """工具未启用时，即使链里有 agentic 历史也不进入该模式"""
    p = AnthropicProvider(api_key="k", base_url="http://fake", model="m")
    p.git_tools_enabled = False
    p.operations_enabled = False
    msgs = [
        _user("/查代码 xx"),
        _assistant("代码在...", "agentic"),
        _user("追问"),
    ]
    assert _resolve(p, msgs) == "rag"


# ============ 附件触发 ============

def test_current_image_triggers_agentic(provider):
    """当前消息带图 → 进入 Agentic"""
    msgs = [Message(
        role="user",
        content=[Content(type="image", data={"data": "b64", "media_type": "image/png"})],
        timestamp=None,
    )]
    assert _resolve(provider, msgs) == "agentic"


def test_history_image_does_not_trigger_agentic(provider):
    """回溯拉来的历史图片（from_history 标记）不触发排查模式"""
    msgs = [Message(
        role="user",
        content=[
            Content(type="image", data={"data": "b64", "media_type": "image/png", "from_history": True}),
            Content(type="text", data="今天天气怎么样"),
        ],
        timestamp=None,
    )]
    assert _resolve(provider, msgs) == "rag"


def test_mixed_history_and_current_image_triggers(provider):
    """历史图 + 当前图混在同一条消息里 → 当前图仍应触发"""
    msgs = [Message(
        role="user",
        content=[
            Content(type="image", data={"data": "b64", "media_type": "image/png", "from_history": True}),
            Content(type="image", data={"data": "b64", "media_type": "image/png"}),
            Content(type="text", data="看下这个报错"),
        ],
        timestamp=None,
    )]
    assert _resolve(provider, msgs) == "agentic"


def test_log_file_triggers_agentic(provider):
    """当前消息带日志文件 → 进入 Agentic"""
    msgs = [Message(
        role="user",
        content=[
            Content(type="log_file", data={"name": "app.log", "path": "/tmp/app.log", "text": "ERROR x", "note": ""}),
            Content(type="text", data="看下这个日志"),
        ],
        timestamp=None,
    )]
    assert _resolve(provider, msgs) == "agentic"


def test_history_turn_image_does_not_keep_agentic(provider):
    """
    引用链历史轮里的图片不应让链永久停在排查模式——
    历史轮模式继承走 metadata.mode，不靠扫描历史图片。
    """
    msgs = [
        Message(role="user", content=[
            Content(type="image", data={"data": "b64", "media_type": "image/png"}),
        ], timestamp=None),
        _assistant("图里的报错是...", "rag"),
        _user("那这个功能的需求是什么"),
    ]
    assert _resolve(provider, msgs) == "rag"


# ============ log_file 内容注入 ============

def test_log_file_converted_to_text_part(provider):
    """log_file 必须转成 text part 注入，不能被静默丢弃"""
    c = Content(type="log_file", data={
        "name": "app.log", "path": "/tmp/app.log", "text": "ERROR NullPointer", "note": "",
    })
    parts = provider._content_to_api_parts(c)
    assert len(parts) == 1
    assert parts[0]["type"] == "text"
    assert "app.log" in parts[0]["text"]
    assert "/tmp/app.log" in parts[0]["text"]
    assert "ERROR NullPointer" in parts[0]["text"]


def test_log_file_zip_only_gives_path(provider):
    """zip 包不内联正文，只给路径与说明，避免塞爆上下文"""
    c = Content(type="log_file", data={
        "name": "logs.zip", "path": "/tmp/logs_unzipped", "text": "",
        "note": "这是一个日志压缩包，已解压到本地目录 /tmp/logs_unzipped。",
    })
    parts = provider._content_to_api_parts(c)
    assert "/tmp/logs_unzipped" in parts[0]["text"]


def test_image_still_converted(provider):
    """回归：图片转换不受 log_file 改动影响"""
    c = Content(type="image", data={"data": "BASE64", "media_type": "image/jpeg"})
    parts = provider._content_to_api_parts(c)
    assert parts[0]["type"] == "image"
    assert parts[0]["source"]["media_type"] == "image/jpeg"
    assert parts[0]["source"]["data"] == "BASE64"
