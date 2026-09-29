"""
运维模式发言人身份注入与鉴权回归测试

=== Bug 背景（2026-09-29 线上现象）===
用户在【已授权的运维群】里引用上一轮运维回答追问「用你建议的方式再查询一遍」，
被拒绝：
    引用链重建: context_key=om_x100...454, 命中历史 1 轮, 注入消息 3 条
    触发 Operations 模式：引用链继承（历史轮次曾使用运维模式）
    WARNING - 未找到带运维 metadata 的消息，共检查 3 条消息
    WARNING - 用户 unknown (user_id=unknown, chat_id=None) 无运维权限
    → "抱歉，您没有运维查询权限"
注意上下文其实串起来了（引用链重建成功、运维模式也正确继承），断的是【身份】。

=== 根因 ===
main.py 只在 `is_operations_mode`（当前文本含 /运维 或 /ops）为真时才注入
operator_id / chat_id metadata。但运维模式有两条触发路径：
    1. 当前消息显式带 /运维 前缀       → 会注入 metadata ✅
    2. 引用链继承（_inherited_chain_mode）→ 文本里没有前缀，不注入 metadata ❌
第 2 条路径下 provider 侧拿不到身份，user_id 退化成 "unknown"、chat_id 为 None，
群组白名单（靠 chat_id 匹配）必然落空，于是授权群里的人也被拦。
身份是"谁在说话"的客观事实，与本轮走哪个模式无关，因此改为无条件注入。

=== 顺带修掉的越权隐患 ===
provider 侧原先是"倒序找第一条带 metadata 的消息"。一旦当前这条缺 metadata，
就会继续往前借用历史轮里【别人】的身份来鉴权 —— B 引用 A 的运维链追问，
可能拿到 A 的权限。改为只认最后一条 user 消息（当前发言人），
找不到就按未知用户拒绝，绝不回退借用历史身份。

=== 复现步骤（修复前必现）===
1. 在已加入运维群白名单的群里发 "/运维 查下 211 的服务状态"，正常得到回答
2. 引用这条回答追问 "用你建议的方式再查询一遍"（不带 /运维）
3. 日志出现"触发 Operations 模式：引用链继承" —— 模式继承成功
4. 紧接着出现"未找到带运维 metadata 的消息" + "用户 unknown ... 无运维权限"
5. 用户收到"您没有运维查询权限"，尽管他就在授权群里

=== 回归关注点（改动相关逻辑时务必保证）===
1. 身份 metadata 必须无条件注入，不能再由 is_operations_mode 之类的开关控制
   —— 否则引用链继承场景立刻复发
2. metadata 必须带 chat_id，群组白名单完全依赖它
3. provider 侧只能取最后一条 user 消息的身份，不得倒序借用历史消息
4. 当前发言人无身份时必须按 unknown 拒绝，不能回退到历史身份（越权）
5. 引用链继承只决定"走哪个模式"，不得放宽权限：非白名单用户引用白名单用户的
   运维链，仍须被拦
"""

import sys
import types
from datetime import datetime
from unittest.mock import Mock, patch

# 运行环境未安装的依赖用桩模块替换（与项目既有单测一致，仅做静态验证）
for _name in ("anthropic", "paramiko", "pywinauto", "jieba"):
    if _name not in sys.modules:
        _stub = types.ModuleType(_name)
        _stub.__getattr__ = lambda _attr: Mock()  # type: ignore[attr-defined]
        sys.modules[_name] = _stub

import anthropic  # noqa: E402  （可能是上面的桩模块）

# 运维循环里有 `except anthropic.APITimeoutError` 等子句。桩模块的属性是 Mock，
# 不是异常类，Python 在求值 except 子句时会抛 TypeError，掩盖真正要验证的行为。
# 这里把它们补成真异常类（仅当当前不是异常类时，避免覆盖真实 anthropic 库）。
for _exc_name in ("APITimeoutError", "APIConnectionError", "APIStatusError"):
    _exc = getattr(anthropic, _exc_name, None)
    if not (isinstance(_exc, type) and issubclass(_exc, BaseException)):
        setattr(anthropic, _exc_name, type(_exc_name, (Exception,), {}))

from ai_assistant.core.models import Content, Message  # noqa: E402
from ai_assistant.providers.anthropic_provider import AnthropicProvider  # noqa: E402


AUTHORIZED_CHAT = "oc_b2d4c03574110818e6680abba1df2879"
WHITELISTED_USER = "ou_whitelisted_user"


def _msg(role: str, text: str, metadata=None) -> Message:
    return Message(
        role=role,
        content=[Content(type="text", data=text)],
        timestamp=datetime.now(),
        metadata=metadata or {},
    )


def _identity_meta(user_id: str, chat_id: str, name: str = "clpang2") -> dict:
    """main.py 注入到 user 消息上的身份 metadata"""
    return {
        "operator_id": user_id,
        "operator_name": name,
        "operator_display_name": "",
        "chat_id": chat_id,
        "source": "feishu",
    }


def _provider_with_ops() -> AnthropicProvider:
    """
    装好运维工具的 provider，鉴权规则模拟真实配置：
    - 用户白名单：WHITELISTED_USER
    - 群组白名单：AUTHORIZED_CHAT
    """
    with patch("ai_assistant.providers.anthropic_provider.anthropic.Anthropic"):
        provider = AnthropicProvider(api_key="test-key")

    # 机器要用具体值：system prompt 构建会遍历 tags / 读 ssh_config.host，
    # Mock 的自动属性不可迭代，会掩盖真正要验证的鉴权行为
    machine = Mock()
    machine.name = "测试211服务器"
    machine.display_name = "测试211服务器"
    machine.tags = []
    machine.ssh_config = Mock(host="10.0.0.211")

    manager = Mock()
    manager.machines = {"测试211服务器": machine}
    manager.applications = {}

    def is_authorized(operator, _machine, chat_id=None):
        if operator.user_id == WHITELISTED_USER:
            return True
        return chat_id == AUTHORIZED_CHAT

    manager.is_authorized = Mock(side_effect=is_authorized)

    tools = Mock()
    tools.operations_manager = manager
    provider.operations_tools = tools
    provider.operations_enabled = True
    return provider


SENTINEL = "__passed_auth__"


def _run_ops(provider, messages):
    """
    跑鉴权门。授权通过后会继续调 Claude，这里让它抛哨兵异常提前收口，
    避免 mock 整个多轮工具循环 —— 我们只关心是否被鉴权拦下。

    注意：运维循环末尾有 `except Exception` 兜底，哨兵异常会被它捕获并转成
    (错误文案, {"error": "exception"}) 返回，因此这里不会真的抛出来。
    用 _passed_auth() 判定"是否走到了调用 Claude 那一步"，比只断言
    "不是 unauthorized" 更严格 —— 后者在任何早退路径下都会假通过。
    """
    provider.client.messages.create = Mock(side_effect=RuntimeError(SENTINEL))
    return provider._send_with_context_operations(messages, "", "session-1")


def _was_denied(result) -> bool:
    """是否被鉴权门拒绝"""
    _reply, metadata = result
    return metadata.get("error") == "unauthorized"


def _passed_auth(result) -> bool:
    """是否通过鉴权并真正开始调用 Claude（由哨兵异常证明）"""
    reply, metadata = result
    return metadata.get("error") == "exception" and SENTINEL in reply


# ==================== 1. 身份 metadata 无条件注入 ====================


class TestIdentityAlwaysInjected:
    """main.py 必须对每条 user 消息注入身份，而非仅在 /运维 前缀时"""

    @staticmethod
    def _build_metadata(parsed: dict) -> dict:
        """复刻 main.py 中的注入逻辑（无条件，不看是否带 /运维 前缀）"""
        return {
            "operator_id": parsed.get("sender_id", ""),
            "operator_name": parsed.get("sender_name", ""),
            "operator_display_name": parsed.get("sender_display_name", ""),
            "chat_id": parsed.get("chat_id", ""),
            "source": "feishu",
        }

    def test_injected_without_ops_prefix(self):
        """
        核心回归：文本里没有 /运维 也必须注入身份。
        引用链继承场景正是这种消息，旧逻辑在此不注入 → 鉴权必失败。
        """
        parsed = {
            "text": "用你建议的方式再查询一遍",
            "sender_id": "ou_4ee1a9119321d98138e358b5548dccb6",
            "sender_name": "clpang2",
            "chat_id": AUTHORIZED_CHAT,
        }

        meta = self._build_metadata(parsed)

        assert meta["operator_id"] == "ou_4ee1a9119321d98138e358b5548dccb6"
        assert meta["chat_id"] == AUTHORIZED_CHAT, "缺 chat_id，群组白名单必然落空"

    def test_chat_id_present_for_group_whitelist(self):
        """群组白名单只认 chat_id，它必须在 metadata 里"""
        meta = self._build_metadata({"chat_id": AUTHORIZED_CHAT})
        assert meta["chat_id"] == AUTHORIZED_CHAT

    def test_missing_sender_degrades_to_empty_not_crash(self):
        """sender 缺失时字段为空串（后续按 unknown 鉴权），不应抛异常"""
        meta = self._build_metadata({})
        assert meta["operator_id"] == ""
        assert meta["chat_id"] == ""
        assert meta["source"] == "feishu"


# ==================== 2. 引用链继承场景端到端鉴权 ====================


class TestChainInheritAuthorization:
    """复现"授权群里引用追问被拒"的完整链路"""

    def test_authorized_group_chain_followup_passes(self):
        """
        核心回归：引用链重建出的 3 条消息（user/assistant/user），
        最后一条是当前追问且带身份 → 授权群应放行。
        """
        provider = _provider_with_ops()
        messages = [
            _msg("user", "/运维 查下 211 的服务状态",
                 _identity_meta("ou_other_user", AUTHORIZED_CHAT)),
            _msg("assistant", "服务正常运行，建议用 get_logs 查日志",
                 {"mode": "operations", "tool_rounds": 3}),
            _msg("user", "用你建议的方式再查询一遍",
                 _identity_meta("ou_4ee1a911", AUTHORIZED_CHAT)),
        ]

        result = _run_ops(provider, messages)

        assert not _was_denied(result), "授权群里的引用追问被误拦，bug 复发"
        assert _passed_auth(result), "未走到调用 Claude，说明在鉴权前就早退了"

    def test_identity_taken_from_current_speaker(self):
        """鉴权用的必须是当前发言人，而不是历史轮里的人"""
        provider = _provider_with_ops()
        messages = [
            _msg("user", "/运维 查状态", _identity_meta(WHITELISTED_USER, "oc_other")),
            _msg("assistant", "结果...", {"mode": "operations"}),
            _msg("user", "再查一遍", _identity_meta("ou_current", AUTHORIZED_CHAT)),
        ]

        _run_ops(provider, messages)

        call = provider.operations_tools.operations_manager.is_authorized.call_args
        assert call.args[0].user_id == "ou_current"
        assert call.kwargs["chat_id"] == AUTHORIZED_CHAT

    def test_unauthorized_group_still_denied(self):
        """继承只决定模式，不放宽权限：非授权群仍须拦住"""
        provider = _provider_with_ops()
        messages = [
            _msg("user", "/运维 查状态", _identity_meta("ou_a", "oc_unauthorized")),
            _msg("assistant", "结果...", {"mode": "operations"}),
            _msg("user", "再查一遍", _identity_meta("ou_b", "oc_unauthorized")),
        ]

        result = _run_ops(provider, messages)

        assert _was_denied(result)

    def test_whitelisted_user_passes_in_any_chat(self):
        """用户白名单与群组无关，任意群都应放行"""
        provider = _provider_with_ops()
        messages = [
            _msg("user", "查下状态", _identity_meta(WHITELISTED_USER, "oc_random")),
        ]

        result = _run_ops(provider, messages)

        assert not _was_denied(result)
        assert _passed_auth(result)


# ==================== 3. 不得借用历史身份（越权防护）====================


class TestNoIdentityFallbackToHistory:
    """当前发言人缺身份时，绝不能往前借用别人的身份"""

    def test_missing_current_identity_is_denied(self):
        """
        核心安全回归：历史轮带白名单用户身份，当前这条没有身份 →
        必须按 unknown 拒绝，不能借用历史身份放行。
        """
        provider = _provider_with_ops()
        messages = [
            _msg("user", "/运维 查状态", _identity_meta(WHITELISTED_USER, "oc_x")),
            _msg("assistant", "结果...", {"mode": "operations"}),
            _msg("user", "再查一遍"),  # 无 metadata
        ]

        result = _run_ops(provider, messages)

        assert _was_denied(result), "借用了历史轮的身份，存在越权风险"

    def test_assistant_metadata_not_used_as_identity(self):
        """assistant 消息带 mode 等 metadata，不能被当成发言人身份"""
        provider = _provider_with_ops()
        messages = [
            _msg("assistant", "上轮结果", {"mode": "operations", "chat_id": AUTHORIZED_CHAT}),
            _msg("user", "再查一遍"),  # 无身份
        ]

        result = _run_ops(provider, messages)

        assert _was_denied(result), "把 assistant 的 metadata 当成了发言人身份"

    def test_unknown_operator_reported_in_denial(self):
        """拒绝时 user_id 应为 unknown，便于日志排查"""
        provider = _provider_with_ops()
        messages = [_msg("user", "查状态")]

        result = _run_ops(provider, messages)

        assert _was_denied(result)
        call = provider.operations_tools.operations_manager.is_authorized.call_args
        assert call.args[0].user_id == "unknown"
        assert call.kwargs["chat_id"] is None

    def test_empty_operator_id_with_authorized_chat_passes(self):
        """
        身份字段为空但 chat_id 是授权群 → 仍应放行。
        群组授权的语义就是"这个群里的人都可以查"，不依赖具体 user_id。
        """
        provider = _provider_with_ops()
        messages = [_msg("user", "查状态", _identity_meta("", AUTHORIZED_CHAT, name=""))]

        result = _run_ops(provider, messages)

        assert not _was_denied(result)
        assert _passed_auth(result)
