"""
Anthropic 请求体诊断与 400 容错的防回归测试

=== Bug 背景（2026-10-27 10:38 线上故障，trace_id=1e1e270851bb）===
用户私聊发了 3442 字符 Java 异常堆栈 + "这里的错误"，触发 Agentic 排查模式。
第 1、2 轮工具调用（find_files → read_file）都正常，第 3 轮请求被上游拒绝：

    10:39:53 ERROR Anthropic API 错误: status=400, message=Error code: 400 -
    {'error': {'type': '<nil>', 'message': 'Invalid tool use format. |
    REQUEST_BODY_INVALID (request id: 20261027103951826841861iT2rjM5r)
    (request id: 202610270239209319572428268d9d6AX7JXABB)'}}

两个问题叠加，导致这次失败既无法定位、也没被兜住：

1. 【日志零信息】旧实现只打 `status` 和 `message`，请求体一个字节都没打。
   "Invalid tool use format" 没说是哪一轮、哪条消息、哪个字段不合法，
   运维拿到这行日志无从下手，只能猜。

2. 【容错分支从未进入】旧回退条件是
       e.status_code == 400 and "tool_result" in str(e.message).lower() and round_num > 1
   而现场真实文案是 "Invalid tool use format. | REQUEST_BODY_INVALID"，
   **不含 `tool_result` 子串** → 整段 400 回退重试代码从来没有执行过，
   一次 400 就直接放弃整次排查。

=== 复现步骤（修复前必现）===
1. 私聊发一段较长的 Java 异常堆栈 + "这里的错误"，命中 Agentic 模式关键词
2. AI 第 1 轮 find_files、第 2 轮 read_file，均正常返回 tool_result
3. 第 3 轮请求被上游返回 400 "Invalid tool use format. | REQUEST_BODY_INVALID"
4. 日志只有一行 status + message，请求体结构完全看不到（问题 1）
5. 因文案不含 `tool_result`，回退分支跳过，直接返回错误给用户（问题 2）

=== 第二轮评审查出的 2 个缺陷（2026-10-27，均在重试裁剪路径上）===

P1-A【并行工具乱序 → 自检假阳性 → 误触发裁剪】
  "tool_result 顺序与 tool_use 不一致"这条观察按 Anthropic 规范是**合法**的
  （并行工具用 concurrent.futures.as_completed 收结果，顺序天然按完成先后），
  但它原先混在 audit_api_messages 的返回值里，而裁剪条件是
  `(tool_format_hit or post_issues)` —— 非空即成立。于是只要并行乱序 +
  来任何一个与工具格式无关的 400（max_tokens 超限 / 模型名写错 / 网关额度
  耗尽回成 400），就被判成"本地自检发现结构问题"，白扔一轮正常工具交互、
  扣一份预算，日志还写下误导性结论。
  修复：拆出 audit_api_messages_advisory，诊断价值保留、决策权剥离。

P1-B【[:-2] 能裁出必然再次 400 的畸形体】
  `[:-2]` 隐含假设严格交替，历史不交替时裁完就是畸形体：
      [user, assistant, assistant(tool_use), user(tool_result)] → 末条 assistant
      [user, user,      assistant(tool_use), user(tool_result)] → 连续两条 user
  两者 len=4 都过了 `>= 3` 的门槛（那个门槛只管长度不管角色）。裁出畸形体
  比不裁更糟：裁完必然再 400，自检非空又触发第二次裁剪，两份预算全废。
  修复：try_trim_last_tool_round 裁完先自检，不合法就如实放弃且不耗预算。

=== 回归关注点（改动 anthropic_provider.py 诊断/容错逻辑时务必保证）===
1. `is_tool_format_400` 必须认得现场那条不含 `tool_result` 的真实文案，
   否则 400 兜底会再次变成死代码
2. `audit_api_messages` 默认（expects_prefill=False）必须坚持"末条为 user"；
   只有 AI 子调用的 prefill 路径才放宽，且放宽的仅限"角色"这一点
3. 裁剪重试必须有预算上限，不能一路裁到空列表再烧满 600s 超时
4. 上游 request id 必须留下，那是找网关方对账的唯一凭据
5. 诊断日志只打结构骨架，不得打用户消息原文 / 工具结果全文
6. audit_api_messages 的返回值**只许含规范上确定非法的项**。任何"合法但
   可疑"的观察都必须走 advisory，否则 P1-A 立刻复发
7. 裁剪自救必须验证裁剪结果，禁止退回闭眼 `[:-2]`，否则 P1-B 立刻复发

注意：所有被测新符号都在**测试函数内部**延迟导入。若写在模块顶层，
把修复改回原样做红灯验证时会退化成 collection error / ImportError，
而那两种都不算红灯（见项目规范）。
"""
import json
import sys
import types
from unittest.mock import Mock

# 运行环境未安装的依赖用桩模块替换（与项目既有单测一致，仅做静态验证）。
#
# 注意 anthropic 的桩必须把三个异常属性建成【真异常类】，不能只给一个
# 返回 Mock 的 __getattr__：被测代码里有 `except anthropic.APIStatusError`，
# Mock 不是异常类，Python 求值 except 子句时会抛 TypeError，掩盖真正的行为。
# 另外本文件名按字母序排在 tests/unit 最前，桩模块一旦注册就被后续测试文件
# 共用（`if name not in sys.modules` 会让它们跳过自己的桩）。所以这里必须
# 提供"不弱于其他文件所需"的桩，否则会把别人的用例带崩 —— 这正是本文件
# 第一版造成 test_doc_indexing_error.py 三个用例失败的原因。
# 桩清单 = anthropic + paramiko，刻意【不】桩 jieba / pywinauto：
#
# paramiko 必须桩：TestRetryBudget 驱动真实的 _send_with_context_agentic，
# 它在 anthropic_provider.py:1120 `from ai_assistant.tools.git_tools import ...`，
# 触发 tools/__init__.py:7 → operations_commands.py:14 `import paramiko`。
# 不桩的话这条用例单跑必红，全量跑却绿（因为 test_operations_operator_identity.py:52
# 先桩了）——"靠别人的桩才绿"比永久红更隐蔽：那个文件哪天改了桩清单，
# 本用例会无声变红，且没人会联想到桩。
#
# jieba 刻意不桩：test_doc_indexing_error.py 在本环境本来是 collection error
# （缺 jieba），属于存量问题。本文件若顺手桩了 jieba，那个文件就变成可收集，
# 于是暴露出 3 个原先根本没运行的失败用例，看上去像是"我把测试搞坏了"
# （本文件第一版正是如此）。
#
# pywinauto 刻意不桩：已核过上面那条 import 链不经过它
# （tools/__init__ → operations_commands / operations_tools → core.operations_models），
# 桩了只是扩大影响面。
for _dep in ("anthropic", "paramiko"):
    if _dep in sys.modules:
        continue
    if _dep == "paramiko":
        _stub = types.ModuleType("paramiko")
        _stub.__getattr__ = lambda _attr: Mock()  # type: ignore[attr-defined]
        sys.modules["paramiko"] = _stub
        continue

    _anthropic = types.ModuleType("anthropic")

    class _APIError(Exception):
        pass

    class _APIStatusError(_APIError):
        def __init__(self, message="", status_code=500, response=None):
            super().__init__(message)
            self.message = message
            self.status_code = status_code
            self.response = response
            self.request_id = None

    _anthropic.APIError = _APIError
    _anthropic.APITimeoutError = type("APITimeoutError", (_APIError,), {})
    _anthropic.APIConnectionError = type("APIConnectionError", (_APIError,), {})
    _anthropic.APIStatusError = _APIStatusError
    _anthropic.Anthropic = lambda **kw: Mock(base_url=kw.get("base_url"), messages=Mock())
    sys.modules["anthropic"] = _anthropic


# ==================== 测试夹具 ====================

# 现场那条 400 的完整文案（含两个嵌套 request id，原样保留用于判定与取 id）
LIVE_400_MESSAGE = (
    "Error code: 400 - {'error': {'type': '<nil>', 'message': "
    "'Invalid tool use format. | REQUEST_BODY_INVALID "
    "(request id: 20261027103951826841861iT2rjM5r) "
    "(request id: 202610270239209319572428268d9d6AX7JXABB)'}}"
)


def _live_round3_messages():
    """
    按现场时间线重建第 3 轮的 api_messages（正常路径）。

    对应日志：
      10:38:47 执行工具: find_files({'name_pattern': 'CourtTrialStatusUploadService'}) → files=1
      10:39:20 执行工具: read_file({...CourtTrialStatusUploadService.java, 1-170}) → size=6677
    """
    return [
        {"role": "user", "content": [{"type": "text", "text": "X" * 3442}]},
        {"role": "assistant", "content": [{
            "type": "tool_use",
            "id": "toolu_01FindFiles",
            "name": "find_files",
            "input": {"name_pattern": "CourtTrialStatusUploadService"},
        }]},
        {"role": "user", "content": [{
            "type": "tool_result",
            "tool_use_id": "toolu_01FindFiles",
            "content": json.dumps(
                {"files": ["src/main/java/CourtTrialStatusUploadService.java"]},
                ensure_ascii=False,
            ),
        }]},
        {"role": "assistant", "content": [{
            "type": "tool_use",
            "id": "toolu_01ReadFile",
            "name": "read_file",
            # 第 3 轮首次出现整数参数（start_line / end_line），与第 1、2 轮
            # 的纯字符串参数不同，是这一轮唯一的结构性差异
            "input": {
                "path": "src/main/java/CourtTrialStatusUploadService.java",
                "start_line": 1,
                "end_line": 170,
            },
        }]},
        {"role": "user", "content": [{
            "type": "tool_result",
            "tool_use_id": "toolu_01ReadFile",
            "content": "Y" * 6677,
        }]},
    ]


def _prefill_messages():
    """
    复刻 AI 子调用的消息形态（core/ai_provider.py 的 build_*_messages）：
    刻意以 assistant prefill 结尾，用来钉死输出格式。
    """
    return [
        {"role": "user", "content": "<user_input>订单服务起不来</user_input>"},
        {"role": "assistant", "content": '{"is_attack":'},
    ]


# ==================== 1. 400 文案判定（旧条件漏掉的那条） ====================


class TestIsToolFormat400:
    """
    旧条件 `"tool_result" in message.lower()` 漏掉了现场真实文案，
    导致 400 兜底成为死代码。这是本次故障最确凿的缺陷。
    """

    def test_live_message_recognized(self):
        """现场那条 'Invalid tool use format. | REQUEST_BODY_INVALID' 必须被认出来"""
        from ai_assistant.providers.anthropic_provider import is_tool_format_400

        assert is_tool_format_400(LIVE_400_MESSAGE) is True, (
            "现场真实文案未被识别为工具格式类 400 —— 旧条件只匹配 'tool_result' "
            "子串时就是这个结果，400 兜底会再次变成死代码"
        )

    def test_live_message_has_no_tool_result_substring(self):
        """
        固化"为什么旧条件会漏"：文案里确实不含 tool_result 子串。
        这条断言是上一条的解释，防止有人把判定改回子串匹配还以为等价。
        """
        assert "tool_result" not in LIVE_400_MESSAGE.lower()

    def test_anthropic_native_wording_recognized(self):
        """Anthropic 原生的 tool_result 文案同样要认（不能为了新文案丢了旧的）"""
        from ai_assistant.providers.anthropic_provider import is_tool_format_400

        assert is_tool_format_400(
            "messages.2: tool_result block(s) provided when previous message "
            "does not contain any tool_use blocks"
        ) is True

    def test_unrelated_error_not_misjudged(self):
        """与工具格式无关的错误不能误判，否则会去裁剪本来正常的请求体"""
        from ai_assistant.providers.anthropic_provider import is_tool_format_400

        assert is_tool_format_400("insufficient balance, please recharge") is False
        assert is_tool_format_400("") is False
        assert is_tool_format_400(None) is False


# ==================== 2~4. prefill 耦合（末条角色规则的分叉） ====================


class TestPrefillCoupling:
    """
    本项目有两类消息列表，对"末条角色"要求正好相反：
      - 工具循环：必须以 user 结尾，末条是 assistant 说明漏了 tool_result
      - AI 子调用：刻意以 assistant prefill 结尾（core/ai_provider.py:573/687/744）
    若不区分，把自检接到子调用路径会产生假的"末条消息 role='assistant'"，
    而裁剪条件是 `(tool_format_hit or post_issues)`，一个假阳性就足以触发
    `api_messages[:-2]`，把正常的 prefill 请求剪坏。
    """

    def test_strict_mode_flags_trailing_assistant(self):
        """默认（工具循环语义）必须坚持"末条为 user"，否则真实畸形体会漏过"""
        from ai_assistant.providers.anthropic_provider import audit_api_messages

        issues = audit_api_messages(_prefill_messages())
        assert issues, "默认模式下末条 assistant 必须被报出来"
        assert any("末条" in x for x in issues), f"问题清单里没有末条相关项: {issues}"

    def test_prefill_mode_allows_trailing_assistant(self):
        """
        显式声明走 prefill 路径时，不得再报"末条角色"问题。
        这是假阳性的收口点：报了就会触发裁剪，把正常请求剪坏。
        """
        from ai_assistant.providers.anthropic_provider import audit_api_messages

        issues = audit_api_messages(_prefill_messages(), expects_prefill=True)
        assert not any("末条" in x for x in issues), (
            f"prefill 路径仍被报末条角色问题，会导致假阳性裁剪: {issues}"
        )

    def test_prefill_mode_still_flags_dangling_tool_use(self):
        """
        放宽的只是"角色"这一点，不是整条尾部约束：
        末条 assistant 里带 tool_use（没有后续 tool_result）仍必须报。
        没有这条测试，docstring 里那句边界承诺只是一句注释。
        """
        from ai_assistant.providers.anthropic_provider import audit_api_messages

        messages = [
            {"role": "user", "content": [{"type": "text", "text": "查一下"}]},
            {"role": "assistant", "content": [{
                "type": "tool_use",
                "id": "toolu_01Dangling",
                "name": "read_file",
                "input": {"path": "a.java"},
            }]},
        ]
        issues = audit_api_messages(messages, expects_prefill=True)
        assert issues, "末条 assistant 带 tool_use 却没有 tool_result，必须报问题"
        assert any("tool_result" in x for x in issues), (
            f"问题清单未指出缺少 tool_result: {issues}"
        )


# ==================== 5. 现场第 3 轮请求体自检 ====================


class TestLiveRound3Body:
    """
    固化排查结论：按现场时间线重建的第 3 轮请求体，本地自检是干净的。
    这是"我们自己拼了畸形请求体"这一方向被排除的依据，必须可复验 ——
    否则下次有人改了拼装逻辑，这个结论就失效了而没人知道。
    """

    def test_round3_body_passes_audit(self):
        """合法的工具循环列表必须返回空问题清单"""
        from ai_assistant.providers.anthropic_provider import audit_api_messages

        issues = audit_api_messages(_live_round3_messages(), expects_prefill=False)
        assert issues == [], (
            f"重建的第 3 轮请求体被判为非法，排查结论需要重新评估: {issues}"
        )

    def test_skeleton_exposes_param_types_without_content(self):
        """
        骨架必须给出定位所需的结构信息，且不含正文：
        3442 字符的用户堆栈和 6677 字符的源码只能以长度出现。
        参数值类型要打出来 —— 第 3 轮首次出现 int 参数是它与前两轮唯一的差异。
        """
        from ai_assistant.providers.anthropic_provider import describe_api_messages

        skeleton = describe_api_messages(_live_round3_messages())
        assert "msgs=5" in skeleton
        assert "text(3442)" in skeleton
        assert "tool_result(id=toolu_01ReadFi..,str,6677)" in skeleton
        # 参数名:类型，int 参数可见
        assert "start_line:int" in skeleton
        assert "end_line:int" in skeleton
        # 正文绝不出现
        assert "X" * 100 not in skeleton
        assert "Y" * 100 not in skeleton
        # 单行，便于日志采集按行切分
        assert "\n" not in skeleton

    def test_audit_catches_empty_text_block(self):
        """
        空 text block 是最隐蔽的一类：当轮无害，回传到下一轮才触发 400。
        （验证自检有能力抓到它，即使现场时间线并未产生这种形态）
        """
        from ai_assistant.providers.anthropic_provider import audit_api_messages

        messages = _live_round3_messages()
        messages[3] = {"role": "assistant", "content": [
            {"type": "text", "text": ""},
            {"type": "tool_use", "id": "toolu_01ReadFile", "name": "read_file",
             "input": {"path": "a.java"}},
        ]}
        issues = audit_api_messages(messages)
        assert any("空 text block" in x for x in issues), f"未抓到空 text block: {issues}"

    def test_audit_catches_tool_id_mismatch(self):
        """tool_use / tool_result 的 id 配不上，必须双向报出来"""
        from ai_assistant.providers.anthropic_provider import audit_api_messages

        messages = _live_round3_messages()
        messages[4] = {"role": "user", "content": [{
            "type": "tool_result", "tool_use_id": "toolu_WRONG", "content": "x",
        }]}
        issues = audit_api_messages(messages)
        assert any("没有对应的 tool_result" in x for x in issues), f"未报缺失配对: {issues}"
        assert any("找不到对应的 tool_use" in x for x in issues), f"未报孤立结果: {issues}"


# ==================== 6. 上游 request id 提取 ====================


class TestUpstreamRequestIds:
    """
    request id 是找网关方对账的唯一凭据。现场那条 400 里嵌了两个不同格式、
    不同时间戳的 id，说明请求穿过了多级代理，两个都必须留下。
    """

    def test_extracts_both_ids_from_live_message(self):
        from ai_assistant.providers.anthropic_provider import extract_upstream_request_ids

        ids = extract_upstream_request_ids(LIVE_400_MESSAGE)
        assert ids == [
            "20261027103951826841861iT2rjM5r",
            "202610270239209319572428268d9d6AX7JXABB",
        ], f"未取出现场那两个 request id: {ids}"

    def test_no_id_returns_empty(self):
        from ai_assistant.providers.anthropic_provider import extract_upstream_request_ids

        assert extract_upstream_request_ids("plain failure") == []
        assert extract_upstream_request_ids(None) == []


# ==================== 7. 裁剪重试预算上限 ====================


class TestRetryBudget:
    """
    旧代码的回退没有上限：持续 400 会一路裁到只剩首条 user 消息，
    白跑满 600s 超时，用户等 10 分钟拿到空结论。

    这里直接驱动真实的 _send_with_context_agentic：让 client 每次都抛 400，
    断言"调用次数有限 + 没把消息裁空 + 最终如实返回错误"。
    不用 stub 复刻循环结构，避免假绿灯。
    """

    @staticmethod
    def _provider_alternating_400():
        """
        构造"成功一轮 → 400 一轮 → 成功一轮 → 400 一轮…"的上游。

        为什么必须是交替而不是"一直 400"：裁剪分支本身带 `len(api_messages) >= 3`
        的下界保护，持续 400 时消息只会变短，裁两次就因长度不足而自然退出 ——
        这种场景下有没有预算上限都一样，拿它做断言就是个假绿灯（本测试第一版
        正是如此：去掉预算上限后仍然 17 passed）。
        真正的失控是**交替**：裁剪后重试成功 → 又追加一轮 assistant+tool_result
        把列表重新撑长 → 再 400 → 再裁……长度永远够，循环只能靠轮数/600s 超时收口。
        预算上限是唯一能打断这个振荡的东西。
        """
        from unittest.mock import patch

        from ai_assistant.providers import anthropic_provider as ap
        from ai_assistant.providers.anthropic_provider import AnthropicProvider

        # 必须从【被测模块自己持有的】anthropic 引用上取异常基类，不能自己
        # `import anthropic`：
        # test_doc_indexing_error.py 在模块顶层无条件执行
        # `sys.modules['anthropic'] = MagicMock()`，而 pytest 会先收集所有模块
        # 再跑用例，所以用例运行时 sys.modules['anthropic'] 已经被换成另一个对象。
        # 被测模块在 import 时就绑定了原来那个模块对象，两边的 APIStatusError
        # 不是同一个类 → provider 的 `except anthropic.APIStatusError` 抓不到
        # 我们抛的异常，用例在全量跑时失败、单跑时通过（典型的测试间串扰）。
        anthropic = ap.anthropic

        # 桩模块的属性可能是 Mock 而非异常类，except 子句求值时会抛 TypeError，
        # 掩盖真正要验证的行为（与 test_operations_operator_identity.py 一致）
        for name in ("APITimeoutError", "APIConnectionError", "APIStatusError"):
            exc = getattr(anthropic, name, None)
            if not (isinstance(exc, type) and issubclass(exc, BaseException)):
                setattr(anthropic, name, type(name, (Exception,), {}))

        class _Status400(anthropic.APIStatusError):
            def __init__(self):
                super().__init__(LIVE_400_MESSAGE)
                self.status_code = 400
                self.message = LIVE_400_MESSAGE
                self.response = Mock(text=LIVE_400_MESSAGE)
                self.request_id = None

        with patch("ai_assistant.providers.anthropic_provider.anthropic.Anthropic"):
            provider = AnthropicProvider(api_key="test-key", base_url="http://gw.invalid")
        provider.client.base_url = "http://gw.invalid"
        provider.git_tools = Mock()
        provider.git_tools_enabled = True
        provider.repo_manager = None
        # 工具恒成功，保证"成功轮"能把列表重新撑长
        provider._execute_tool = Mock(return_value={"files": ["a.java"]})

        calls = {"n": 0, "sizes": []}
        seq = {"i": 0}

        def _create(**kwargs):
            calls["n"] += 1
            calls["sizes"].append(len(kwargs.get("messages") or []))
            seq["i"] += 1
            # 奇数次成功并返回一个 tool_use（撑长列表），偶数次 400
            if seq["i"] % 2 == 1:
                block = Mock(
                    type="tool_use",
                    id=f"toolu_{seq['i']:03d}",
                    name="find_files",
                    input={"name_pattern": "X"},
                )
                return Mock(
                    stop_reason="tool_use",
                    content=[block],
                    usage=Mock(input_tokens=4, output_tokens=48),
                )
            raise _Status400()

        provider.client.messages.create = Mock(side_effect=_create)
        return provider, calls

    def test_budget_caps_trimming(self):
        """
        成功/400 交替时，裁剪重试必须被预算打断，不能一路振荡到超时。

        预算为 2，所以最多裁两次就该放弃并如实返回错误。
        给 max_rounds=50 作为"没有上限会怎样"的上界：去掉 body_retry_budget
        后这里会一路跑到轮数耗尽，请求次数远超阈值。
        """
        from ai_assistant.core.models import Content, Message

        provider, calls = self._provider_alternating_400()

        messages = [Message(
            role="user",
            content=[Content(type="text", data="X" * 3442)],
            timestamp=None,
        )]
        _reply, metadata = provider._send_with_context_agentic(
            messages, "", "session-1", timeout_mode="rounds", max_rounds=50,
        )

        # 预算 2 → 2 次裁剪重试；加上首次成功轮和最终放弃那次，总数是个小常数。
        # 没有上限时会一路振荡到 max_rounds=50，请求次数在 10 以上。
        assert calls["n"] <= 8, (
            f"成功/400 交替时请求了 {calls['n']} 次，说明裁剪没有预算上限，"
            f"线上会一路振荡到 600s 超时（各次消息数: {calls['sizes']}）"
        )
        assert all(n >= 1 for n in calls["sizes"]), (
            f"消息列表被裁空，裁剪缺少下界保护: {calls['sizes']}"
        )
        assert metadata.get("error") == "api", f"未如实返回错误: {metadata}"
        assert "400" in metadata.get("error_detail", "")


# ==================== 诊断日志不泄露内容 ====================


class TestDiagnosticLogHygiene:
    """
    诊断日志只能打结构骨架。把用户堆栈或工具结果全文倒进日志既违反
    CLAUDE.md「不得记录完整用户消息」，也会把真正有用的流程日志冲掉。
    本项目用 loguru，pytest caplog 抓不到，必须自己 add(sink)。
    """

    def test_error_log_keeps_ids_without_leaking_body(self):
        from loguru import logger

        from ai_assistant.providers.anthropic_provider import log_api_status_error

        secret_body = "Z" * 5000
        error = Mock(
            status_code=400,
            message=LIVE_400_MESSAGE,
            response=Mock(text=secret_body),
            request_id=None,
        )

        lines = []
        sink_id = logger.add(lambda m: lines.append(m), level="DEBUG", format="{message}")
        try:
            log_api_status_error(
                mode="agentic",
                round_num=3,
                error=error,
                api_messages=_live_round3_messages(),
                base_url="http://gw.invalid",
                model="claude-sonnet-4",
            )
        finally:
            logger.remove(sink_id)

        blob = "".join(lines)
        # 定位必需信息都在
        assert "round=3" in blob
        assert "20261027103951826841861iT2rjM5r" in blob, "丢了上游 request id"
        assert "msgs=5" in blob, "没打请求体骨架"
        assert "未发现结构问题" in blob, "自检结论未落地，排查时无法判断该找谁"
        # 正文被截断，没有整段落盘
        assert secret_body not in blob, "响应体全文进了日志"
        assert "X" * 200 not in blob, "用户消息原文进了日志"
        assert "Y" * 200 not in blob, "工具结果原文进了日志"


# ==================== 空 text block 的源头拦截 ====================


class TestCollectAssistantContent:
    """
    空/纯空白 text block 在当轮无害，只在回传进下一轮请求时才触发 400。
    collect_assistant_content 是唯一能在源头拦住它的位置。
    """

    def test_blank_text_block_dropped_with_note(self):
        from ai_assistant.providers.anthropic_provider import collect_assistant_content

        blocks = [
            Mock(type="text", text="   "),
            Mock(type="tool_use", id="toolu_01A", name="read_file", input={"path": "a.java"}),
        ]
        content, tool_uses, notes = collect_assistant_content(blocks)

        assert all(b.get("type") != "text" for b in content), (
            f"纯空白 text block 被回传，下一轮必然 400: {content}"
        )
        assert len(tool_uses) == 1
        assert any("空/纯空白" in n for n in notes), f"丢弃动作没有留痕: {notes}"

    def test_normal_text_preserved(self):
        from ai_assistant.providers.anthropic_provider import collect_assistant_content

        blocks = [Mock(type="text", text="我先找一下这个类")]
        content, tool_uses, notes = collect_assistant_content(blocks)

        assert content == [{"type": "text", "text": "我先找一下这个类"}]
        assert tool_uses == []
        assert notes == []

    def test_tool_use_always_yields_content(self):
        """
        tool_uses 非空 ⇒ assistant_content 必非空（含 input 不是 dict 的极端情况）。

        这条固化的是 :1329 那段"有 tool_use 但 content 为空 → 响应结构异常"
        分支为何可以删：tool_use 分支对 content 是无条件 append 的，input 非
        dict 时也只记 note 不跳过，所以那个分支逻辑上不可达。
        将来谁把 tool_use 改成"input 非法就跳过 append"，本条会红，提醒他
        必须同时把那段兜底加回去。
        """
        from ai_assistant.providers.anthropic_provider import collect_assistant_content

        for bad_input in (None, "not-a-dict", 123, []):
            blocks = [Mock(type="tool_use", id="toolu_01A", name="f", input=bad_input)]
            content, tool_uses, _notes = collect_assistant_content(blocks)
            assert tool_uses and content, (
                f"input={bad_input!r} 时 tool_uses={len(tool_uses)} 但 content 为空，"
                f"说明 :1329 的不可达分支变可达了，必须恢复那段兜底"
            )


# ==================== P1-A 合法但可疑的观察不得参与决策 ====================


def _parallel_messages(result_order):
    """
    构造一轮并行工具交互：assistant 发 A、B 两个 tool_use，
    user 按 result_order 给回 tool_result。

    乱序不是边角情况：并行分支用 concurrent.futures.as_completed 收集结果，
    tool_result 按完成先后排列，与调用顺序天然不一致。
    """
    return [
        {"role": "user", "content": [{"type": "text", "text": "查一下这个类"}]},
        {"role": "assistant", "content": [
            {"type": "tool_use", "id": "toolu_A", "name": "find_files", "input": {"name_pattern": "X"}},
            {"type": "tool_use", "id": "toolu_B", "name": "read_file", "input": {"path": "a.java"}},
        ]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": f"toolu_{x}", "content": "ok"}
            for x in result_order
        ]},
    ]


class TestAdvisoryNotADecisionInput:
    """
    P1-A 防回归。

    并行乱序是常态，而"顺序不一致"按 Anthropic 规范合法。它一旦混进
    audit_api_messages 的返回值，就会让任何与工具格式无关的 400
    （max_tokens 超限 / 模型名写错 / 网关额度耗尽回成 400）稳定误触发裁剪。

    复现（修复前）：
        audit_api_messages(_parallel_messages("BA"))
          -> ['msg[2] 的 tool_result 顺序与 msg[1] 的 tool_use 不一致…']
        该非空列表进 post_issues → `(tool_format_hit or post_issues)` 成立
        → 白裁一轮正常交互 + 扣一份预算
    """

    def test_parallel_out_of_order_is_not_an_audit_issue(self):
        """乱序不得出现在参与决策的 audit 结果里（P1-A 的核心断言）"""
        from ai_assistant.providers.anthropic_provider import audit_api_messages

        issues = audit_api_messages(_parallel_messages("BA"))

        assert issues == [], (
            f"并行乱序被 audit 当成结构问题，会让无关的 400 误触发裁剪: {issues}"
        )

    def test_parallel_out_of_order_still_recorded_as_advisory(self):
        """诊断价值必须保留：剥离决策权不等于丢掉这条观察"""
        from ai_assistant.providers.anthropic_provider import audit_api_messages_advisory

        notes = audit_api_messages_advisory(_parallel_messages("BA"))

        assert len(notes) == 1, f"乱序观察丢失，严格网关的问题将无法排查: {notes}"
        assert "顺序" in notes[0]

    def test_in_order_parallel_has_no_advisory(self):
        """顺序一致时不该有任何噪声"""
        from ai_assistant.providers.anthropic_provider import audit_api_messages_advisory

        assert audit_api_messages_advisory(_parallel_messages("AB")) == []

    def test_real_pairing_defects_still_go_to_audit(self):
        """
        边界：拆分不能把真缺陷一起拆走。

        缺失 / 孤立的 tool_result 是确定非法的，必须留在 audit（参与决策），
        且不该同时出现在 advisory 里造成重复刷屏。
        """
        from ai_assistant.providers.anthropic_provider import (
            audit_api_messages,
            audit_api_messages_advisory,
        )

        missing = _parallel_messages("A")  # B 的 tool_result 缺失

        issues = audit_api_messages(missing)
        assert any("没有对应的 tool_result" in x for x in issues), (
            f"真实的配对缺失没被 audit 抓住: {issues}"
        )
        assert audit_api_messages_advisory(missing) == [], (
            "配对缺失同时进了 advisory，日志会重复报同一件事"
        )


# ==================== P1-B 裁剪结果必须先验证 ====================


def _tool_round(uid="toolu_A"):
    """一轮完整的工具交互：assistant(tool_use) + user(tool_result)"""
    return [
        {"role": "assistant", "content": [
            {"type": "tool_use", "id": uid, "name": "read_file", "input": {"path": "a.java"}},
        ]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": uid, "content": "ok"},
        ]},
    ]


class TestTrimValidatesResult:
    """
    P1-B 防回归。

    `[:-2]` 隐含假设严格交替。历史不交替时裁完就是必然再次 400 的畸形体，
    而自检非空又会触发第二次裁剪 —— 两份预算全废，还多烧两轮超时。
    `len(api_messages) >= 3` 这个门槛只管长度不管角色，防不住。

    两个反例的来源都是真实路径：
      - 历史末条是 assistant：引用链历史重建（按 context_key 重建完整历史）
      - 连续两条 user：main.py 的 deferred / pending_lone_images 合并

    复现（修复前）：
        [user, assistant, assistant(tool_use), user(tool_result)]
          --[:-2]--> [user, assistant]  → 末条 assistant，必然再 400
    """

    def test_trailing_assistant_history_refuses_trim(self):
        """历史末条是 assistant 时，裁剪结果非法，必须放弃而不是闭眼重试"""
        from ai_assistant.providers.anthropic_provider import try_trim_last_tool_round

        messages = [
            {"role": "user", "content": "原始提问"},
            {"role": "assistant", "content": "历史回复"},
        ] + _tool_round()

        trimmed, reason = try_trim_last_tool_round(messages)

        assert trimmed is None, (
            f"裁出了 {trimmed} —— 末条是 assistant，这个请求体必然再次 400，"
            f"还会吃掉第二份重试预算"
        )
        assert reason and "不合法" in reason, f"放弃原因未写明，告警里看不出为什么: {reason}"

    def test_consecutive_user_history_refuses_trim(self):
        """连续两条 user 的历史，裁完角色仍不交替，同样必须放弃"""
        from ai_assistant.providers.anthropic_provider import try_trim_last_tool_round

        messages = [
            {"role": "user", "content": "第一段"},
            {"role": "user", "content": "第二段（合并路径产出）"},
        ] + _tool_round()

        trimmed, reason = try_trim_last_tool_round(messages)

        assert trimmed is None, f"裁出了角色不交替的请求体: {trimmed}"
        assert reason and "不合法" in reason

    def test_wellformed_history_still_trims(self):
        """
        边界：不能因为加了验证就把正常自救也堵死。

        正常交替的历史裁完仍然合法，必须照旧放行 —— 否则 400 容错又变回
        死代码，等于把上一轮的修复推翻。
        """
        from ai_assistant.providers.anthropic_provider import (
            audit_api_messages,
            try_trim_last_tool_round,
        )

        messages = [{"role": "user", "content": "原始提问"}] + _tool_round()

        trimmed, reason = try_trim_last_tool_round(messages)

        assert trimmed is not None, f"正常请求体的自救被堵死了: {reason}"
        assert len(trimmed) == 1
        assert audit_api_messages(trimmed) == [], "放行了一个自身不合法的裁剪结果"

    def test_refused_trim_does_not_consume_budget(self):
        """
        裁剪被拒时不该扣预算：没真的重试，扣了等于凭空少一次自救机会。

        直接驱动真实循环：第 2 轮起持续 400，而历史末条是 assistant 导致
        裁剪必然被拒。断言只发生一次失败调用就如实返回，且告警里带上
        trim_rejected 原因。
        """
        from unittest.mock import patch

        from ai_assistant.core.models import Content, Message
        from ai_assistant.providers import anthropic_provider as ap
        from ai_assistant.providers.anthropic_provider import AnthropicProvider

        # 异常基类必须取自被测模块自己的引用，理由见 _provider_alternating_400
        anthropic = ap.anthropic
        for name in ("APITimeoutError", "APIConnectionError", "APIStatusError"):
            exc = getattr(anthropic, name, None)
            if not (isinstance(exc, type) and issubclass(exc, BaseException)):
                setattr(anthropic, name, type(name, (Exception,), {}))

        class _Status400(anthropic.APIStatusError):
            def __init__(self):
                super().__init__(LIVE_400_MESSAGE)
                self.status_code = 400
                self.message = LIVE_400_MESSAGE
                self.response = Mock(text=LIVE_400_MESSAGE)
                self.request_id = None

        with patch("ai_assistant.providers.anthropic_provider.anthropic.Anthropic"):
            provider = AnthropicProvider(api_key="test-key", base_url="http://gw.invalid")
        provider.client.base_url = "http://gw.invalid"
        provider.git_tools = Mock()
        provider.git_tools_enabled = True
        provider.repo_manager = None
        provider._execute_tool = Mock(return_value={"files": ["a.java"]})

        calls = {"n": 0}

        def _create(**kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                block = Mock(
                    type="tool_use", id="toolu_A", name="find_files",
                    input={"name_pattern": "X"},
                )
                return Mock(
                    stop_reason="tool_use", content=[block],
                    usage=Mock(input_tokens=4, output_tokens=48),
                )
            raise _Status400()

        provider.client.messages.create = Mock(side_effect=_create)

        # 历史末条是 assistant → 第 2 轮 400 后 [:-2] 会裁出 [user, assistant]
        messages = [
            Message(role="user", content=[Content(type="text", data="原始提问")], timestamp=None),
            Message(role="assistant", content=[Content(type="text", data="历史回复")], timestamp=None),
        ]
        _reply, metadata = provider._send_with_context_agentic(
            messages, "", "session-1", timeout_mode="rounds", max_rounds=50,
        )

        assert calls["n"] == 2, (
            f"请求了 {calls['n']} 次。裁剪结果不合法时应当立刻放弃，"
            f"不该拿着畸形体再撞一次 400"
        )
        assert metadata.get("error") == "api"
        assert "trim_rejected" in metadata.get("error_detail", ""), (
            f"放弃原因没进告警，维护者只看到一句 400: {metadata.get('error_detail')}"
        )


# ==================== error_detail 不得整段回显上游文案 ====================


def _provider_raising(exc_factory):
    """构造一个"首轮就抛指定异常"的 provider，用于驱动真实的 except 分支"""
    from unittest.mock import patch

    from ai_assistant.providers import anthropic_provider as ap
    from ai_assistant.providers.anthropic_provider import AnthropicProvider

    anthropic = ap.anthropic
    for name in ("APITimeoutError", "APIConnectionError", "APIStatusError"):
        exc = getattr(anthropic, name, None)
        if not (isinstance(exc, type) and issubclass(exc, BaseException)):
            setattr(anthropic, name, type(name, (Exception,), {}))

    with patch("ai_assistant.providers.anthropic_provider.anthropic.Anthropic"):
        provider = AnthropicProvider(api_key="test-key", base_url="http://gw.invalid")
    provider.client.base_url = "http://gw.invalid"
    provider.git_tools = Mock()
    provider.git_tools_enabled = True
    provider.repo_manager = None
    provider.client.messages.create = Mock(side_effect=lambda **kw: (_ for _ in ()).throw(exc_factory()))
    return provider


class TestErrorDetailDoesNotEchoBody:
    """
    error_detail 会经 core/ai_provider.py 的单点上报进飞书告警
    （`detail=metadata.get("error_detail") or reply`）。

    === Bug 背景 ===
    三处 `detail_parts.append(f"message={e.message}")` 原先完全不截断。
    下游 utils/ai_alert.py 确实有 `redact(detail)[:500]` 兜底，所以不是裸
    泄漏，但 redact 走形态匹配（IP / AK-SK / token），**挡不住用户提问正文**。
    而本项目跑在兼容网关上，网关是否把请求片段回显进 400 文案无法假设。

    === 复现步骤（修复前必现）===
    1. 构造一个 message 里含长段用户正文的 APIStatusError（模拟网关回显）
    2. 驱动 _send_with_context_agentic / _send_with_context_operations /
       _send_with_context_standard 的 APIStatusError 分支
    3. 修复前：metadata["error_detail"] 含该正文的整段原文
       修复后：只含 summarize_text 摘要（len / sha1 / 首尾片段）

    这里刻意**驱动真实的 except 分支**而不是只断言 summarize_text 本身 ——
    只测 helper 的话，调用方哪天把 summarize_text 去掉，测试照样绿。
    """

    # 模拟网关把用户提问正文回显进 400 文案
    ECHOED_BODY = "用户私聊发的异常堆栈正文" * 60

    def _echo_400(self):
        from ai_assistant.providers import anthropic_provider as ap

        message = (
            "Error code: 400 - {'error': {'type': '<nil>', 'message': "
            "'Invalid tool use format. | REQUEST_BODY_INVALID "
            f"rejected body: {self.ECHOED_BODY}'}}}}"
        )

        class _Echo400(ap.anthropic.APIStatusError):
            def __init__(self):
                super().__init__(message)
                self.status_code = 400
                self.message = message
                self.response = Mock(text=message)
                self.request_id = None

        return _Echo400

    def test_agentic_error_detail_is_summarized(self):
        from ai_assistant.core.models import Content, Message

        provider = _provider_raising(self._echo_400())
        messages = [Message(
            role="user", content=[Content(type="text", data="排查一下")], timestamp=None,
        )]
        _reply, metadata = provider._send_with_context_agentic(
            messages, "", "s1", timeout_mode="rounds", max_rounds=3,
        )

        detail = metadata.get("error_detail", "")
        assert self.ECHOED_BODY not in detail, (
            "上游回显的用户正文整段进了 error_detail，会经单点上报送进飞书告警"
        )
        # 摘要形态仍在，排查信息没丢
        assert "upstream_msg(" in detail and "sha1=" in detail, (
            f"message 没走摘要，或摘要形态变了: {detail[:200]}"
        )
        # 错误分类的关键短语必须留在窗口内，否则告警看不出是什么 400
        assert "REQUEST_BODY_INVALID" in detail, (
            f"摘要窗口太窄，关键短语被切掉，告警里只剩 'Error code: 400': {detail[:200]}"
        )

    def test_operations_error_detail_is_summarized(self):
        """
        第二处（_send_with_context_operations，mode=operations）同样要收口。

        === 为什么单独补这条 ===
        三处出口里只有 agentic(:1691) 和 rag(:2553) 有看守，中间这处
        （:2183）零覆盖。实测把 :2183 改回裸 `f"message={e.message}"`：
            :1691 改回 -> 1 failed（agentic 那条红）
            :2183 改回 -> 33 passed  ← 全绿放行，270+ 字符正文整段进告警
            :2553 改回 -> 1 failed（rag 那条红）
        这是「左右对照漏抄」的同一类缺口：防护抄了三处里的两处，测试也
        只钉了那两处。运维模式恰恰是正文最敏感的通道 —— 运维指令里常带
        主机名、内网 IP、账号，而下游 redact 走形态匹配挡不住自然语言正文。

        === 复现步骤（把 :2183 改回裸 e.message 后必现）===
        1. 构造 message 里含长段正文回显的 400（模拟网关回显请求体）
        2. 走 _provider_with_ops_raising 驱动运维循环（鉴权必须真过，
           否则停在 unauthorized，压根到不了 except 分支）
        3. 修复前：metadata["error_detail"] 含正文原文（实测 detail 从
           338 字符涨到 363，正文整段在内）
           修复后：只剩 upstream_msg(...sha1=...) 摘要
        """
        provider = _provider_with_ops_raising(self._echo_400())
        _reply, metadata = provider._send_with_context_operations(
            _ops_messages(), "", "session-ops", timeout_mode="rounds", max_rounds=3,
        )

        detail = metadata.get("error_detail", "")
        assert metadata.get("mode") == "operations"
        assert self.ECHOED_BODY not in detail, (
            "运维路径的 error_detail 整段回显了上游文案，会经单点上报送进飞书告警"
        )
        assert "upstream_msg(" in detail and "sha1=" in detail, (
            f"message 没走摘要，或摘要形态变了: {detail[:200]}"
        )
        # 摘要换掉原文之后，排查还要能定位：status 和分类短语都得在
        assert "status=400" in detail, f"丢了 status: {detail[:200]}"
        assert "REQUEST_BODY_INVALID" in detail, (
            f"摘要窗口太窄，关键短语被切掉，告警里只剩 'Error code: 400': {detail[:200]}"
        )

    def test_standard_rag_error_detail_is_summarized(self):
        """第三处（_send_with_context_standard，mode=rag）同样要收口"""
        from ai_assistant.core.models import Content, Message

        provider = _provider_raising(self._echo_400())
        messages = [Message(
            role="user", content=[Content(type="text", data="普通提问")], timestamp=None,
        )]
        _reply, metadata = provider._send_with_context_standard(messages, "", "s1")

        detail = metadata.get("error_detail", "")
        assert metadata.get("mode") == "rag"
        assert self.ECHOED_BODY not in detail, (
            "rag 路径的 error_detail 整段回显了上游文案"
        )
        assert "upstream_msg(" in detail

    def test_alert_window_covers_live_classification_keywords(self):
        """
        _ALERT_MSG_CHARS 必须大到容得下错误分类的关键短语。

        === 注意 summarize_text 是「头窗 + 尾窗」双段输出 ===
            label(len=N,lines=L,sha1=XX):'头 limit 字符'…'尾 40 字符'
        所以 `短语 in out` 这种断言**会被尾窗蒙过去**：短语可能是在尾部
        重复出现才被捞到的，而不是头窗真的覆盖到了它。实测过一条会假绿灯的
        形态 —— 网关把 message 又塞进 'code' 字段时（`...REQUEST_BODY_INVALID',
        'code': 'REQUEST_BODY_INVALID'}}`），默认窗口（80）下
        `"REQUEST_BODY_INVALID" in out` 是 True，但头窗里压根没有它。
        若只用整体 `in` 断言，把 _ALERT_MSG_CHARS 调回 80 这条测试仍会绿。

        因此本条断言「短语必须完整出现在**头窗片段**内」。头窗片段的构造方式
        与 log_summary._preview 一致（`repr(text[:limit])`），不依赖对输出做
        字符串切分。

        下界的真实理由（不是"前缀占 58 字符所以够不到"）：默认窗口下
        "Invalid tool use format" 被截在词中间（止于 `Invalid tool use forma`），
        而另一条短语可见与否取决于上游文案尾部有没有恰好重复它。网关文案格式
        不受我们控制，所以**不能按偏移量去卡一个刚好够的窗口**（照"58 字符"
        那套推导会算出"100 就够"，那是在赌下一种文案形态）。
        """
        from ai_assistant.providers.anthropic_provider import _ALERT_MSG_CHARS
        from ai_assistant.utils.log_summary import summarize_text

        out = summarize_text(LIVE_400_MESSAGE, limit=_ALERT_MSG_CHARS, label="upstream_msg")
        # 头窗片段：与 log_summary._preview 同构，避免按 … 切分（内容本身可能含 …）
        head = repr(LIVE_400_MESSAGE[:_ALERT_MSG_CHARS])

        assert head in out, (
            f"头窗片段的渲染方式变了，本条的头窗断言已失效，请同步修改: {out[:160]}"
        )
        for phrase in ("Invalid tool use format", "REQUEST_BODY_INVALID"):
            assert phrase in out, f"关键短语整体缺失: {phrase!r} / {out}"
            assert phrase in head, (
                f"{phrase!r} 只在尾窗出现（靠上游文案尾部重复捞回来的），"
                f"头窗没覆盖到。换一种不重复该短语的网关文案就看不见了，"
                f"告警会退化成一句 'Error code: 400'。当前 _ALERT_MSG_CHARS="
                f"{_ALERT_MSG_CHARS}"
            )

    # 网关把 message 又塞进 'code' 字段 —— 判别短语在文案尾部重复一次。
    # 这种形态下尾窗会把短语"捞回来"，让整体 `in` 断言失去判别力。
    TAIL_REPEAT_400 = (
        "Error code: 400 - {'error': {'type': '<nil>', 'message': "
        "'Invalid tool use format. | REQUEST_BODY_INVALID', "
        "'code': 'REQUEST_BODY_INVALID'}}"
    )

    def test_alert_window_covers_keywords_even_without_tail_luck(self):
        """
        上一条用的现场文案尾部**不**重复判别短语，所以在它身上「整体 in」和
        「头窗 in」恰好等价（实测扫了 limit 20~208 共 95 个取值，零缺口）——
        也就是说头窗断言在那条上还是备用的。本条换成尾部重复的网关形态，
        让头窗断言真正承重。

        实测在这条文案上，limit 取 20~104 之间任意值（43 个取值）时：
            "REQUEST_BODY_INVALID" in out  -> True   （尾窗捞回来的）
            "REQUEST_BODY_INVALID" in head -> False  （头窗压根没到）
        所以若有人把 _ALERT_MSG_CHARS 调回 80，只有头窗断言会红。
        """
        from ai_assistant.providers.anthropic_provider import _ALERT_MSG_CHARS
        from ai_assistant.utils.log_summary import summarize_text

        out = summarize_text(self.TAIL_REPEAT_400, limit=_ALERT_MSG_CHARS, label="upstream_msg")
        head = repr(self.TAIL_REPEAT_400[:_ALERT_MSG_CHARS])

        assert head in out, (
            f"头窗片段的渲染方式变了，本条的头窗断言已失效: {out[:160]}"
        )
        for phrase in ("Invalid tool use format", "REQUEST_BODY_INVALID"):
            assert phrase in head, (
                f"{phrase!r} 不在头窗内。这条文案尾部重复了判别短语，所以整体 "
                f"`in` 仍会是 True —— 告警看着有信息，换一种不重复的网关文案就"
                f"什么都没有了。当前 _ALERT_MSG_CHARS={_ALERT_MSG_CHARS}"
            )

    def test_tail_window_can_fake_a_green_light(self):
        """
        把上面两条依赖的那个坑本身钉住：证明"整体 in"确实能被尾窗蒙过。

        这条不是测产品代码，是测**测法**。若将来 log_summary 去掉尾窗，
        本条会红 —— 那时头窗断言就成了多余的严格，可以一起简化。
        反过来，只要本条是绿的，就说明"整体 in"这种断言方式仍然不可信。
        """
        from ai_assistant.utils.log_summary import PREVIEW_HEAD_CHARS, summarize_text

        out = summarize_text(self.TAIL_REPEAT_400, limit=None, label="upstream_msg")
        head = repr(self.TAIL_REPEAT_400[:PREVIEW_HEAD_CHARS])

        assert "REQUEST_BODY_INVALID" in out, (
            "尾窗没捞回重复的短语，尾窗行为变了（头窗断言可相应简化）"
        )
        assert "REQUEST_BODY_INVALID" not in head, (
            "头窗已经覆盖到该短语，这条构造不出假绿灯了，请换一条更长的文案"
        )

    def test_alert_window_still_truncates_mid_length_echo(self):
        """
        上界：窗口不能大到把中等长度的回显整段打全。

        limit 同时抬高"整段原样显示"的门槛，所以这条盯的是
        _ALERT_MSG_CHARS 被人"顺手"调大（如调到 600）时的回归 ——
        一条 582 字符的回显文案在 600 下会原样落盘，在 200 下被压成摘要。

        === 前置断言为什么不能写成 `len(echoed) > _ALERT_MSG_CHARS` ===
        log_summary 的"整段显示"门槛**就是** limit（`len <= _head_len(limit)`
        走原样输出分支），所以那样写的话前置条件和正主断言在**整个取值域上
        恒等**，不是"当前取值下恰好同真假"：
            limit=581  len>limit=True  | echoed in out=False
            limit=582  len>limit=False | echoed in out=True
            limit=583  len>limit=False | echoed in out=True
        （limit 取 20~900 步长 7 共 126 个取值全程 `(len>limit) == (echoed
        not in out)`。）前置断言成了正主断言换个写法，后果有两条：
          1. 常量被调到 600 时红的是前置那行裸 assert（无消息体），正主那句
             写得很清楚的"太大，582 字符的回显被整段打全"永远没人看得到；
          2. 正主断言零鉴别力 —— 它在任何 limit 下都不可能单独红。

        所以前置断言只钉**夹具自身的性质**、不碰 `_ALERT_MSG_CHARS`：
          - 夹具被人改短 → 前置红，提示这条不再覆盖"调到 600"那个场景；
          - 常量被人调大 → 前置仍过，让带消息体的正主断言来红。
        500 是对"上界要防到多大"的声明，和被测常量解耦（取 500 而不是 582
        是留余量：夹具长度随 "用户提问内容"*90 的实现微调，不该一动就红）。

        没用「把夹具拉长」或「换成 pytest.skip」这两种改法：
          - 拉长夹具只是换个数字，恒等关系不变，红的还是前置断言
            （实测夹具换成 1242 字符后，limit=1242 时仍是前置先红）；
          - 换成 skip 更糟 —— 常量被调到 600 时这条会"跳过"而不是失败，
            一条上界回归变成零信号。
        """
        from ai_assistant.providers.anthropic_provider import _ALERT_MSG_CHARS
        from ai_assistant.utils.log_summary import summarize_text

        echoed = "Error code: 400 - {'error':{'message':'" + "用户提问内容" * 90 + "'}}"

        # 前置：只钉夹具性质，不引用被测常量（理由见 docstring）
        assert len(echoed) > 500, (
            f"夹具变短了（当前 {len(echoed)} 字符），这条就不再覆盖"
            f"「_ALERT_MSG_CHARS 被调到 600」这个回归场景"
        )

        out = summarize_text(echoed, limit=_ALERT_MSG_CHARS, label="upstream_msg")

        assert echoed not in out, (
            f"_ALERT_MSG_CHARS={_ALERT_MSG_CHARS} 太大，{len(echoed)} 字符的回显被"
            f"整段打全。limit 同时是「整段原样显示」的门槛（见 log_summary 的 "
            f"PREVIEW_HEAD_CHARS 注释），调大它等于放宽泄漏门槛，不只是让摘要更长"
        )
        assert "sha1=" in out, "长内容没有指纹，跨日志串联会断"


def _provider_with_ops_raising(exc_factory):
    """
    装好运维工具（鉴权直接放行）且 messages.create 首轮就抛指定异常的 provider。

    鉴权门必须真过：被 unauthorized 拦下的话压根走不到 except 分支，
    断言会在一个根本没执行被测代码的路径上假通过。machine 的 name /
    tags / ssh_config 要给具体值，因为 system prompt 构建会遍历它们。
    """
    from unittest.mock import patch

    from ai_assistant.providers import anthropic_provider as ap
    from ai_assistant.providers.anthropic_provider import AnthropicProvider

    anthropic = ap.anthropic
    for name in ("APITimeoutError", "APIConnectionError", "APIStatusError"):
        exc = getattr(anthropic, name, None)
        if not (isinstance(exc, type) and issubclass(exc, BaseException)):
            setattr(anthropic, name, type(name, (Exception,), {}))

    with patch("ai_assistant.providers.anthropic_provider.anthropic.Anthropic"):
        provider = AnthropicProvider(api_key="test-key", base_url="http://gw.invalid")
    provider.client.base_url = "http://gw.invalid"

    machine = Mock()
    machine.name = "测试211服务器"
    machine.display_name = "测试211服务器"
    machine.tags = []
    machine.ssh_config = Mock(host="10.0.0.211")

    manager = Mock()
    manager.machines = {"测试211服务器": machine}
    manager.applications = {}
    manager.is_authorized = Mock(return_value=True)

    tools = Mock()
    tools.operations_manager = manager
    provider.operations_tools = tools
    provider.operations_enabled = True

    provider.client.messages.create = Mock(
        side_effect=lambda **kw: (_ for _ in ()).throw(exc_factory())
    )
    return provider


def _ops_messages():
    """带齐运维 metadata 的单条用户消息（缺 metadata 会走未知用户鉴权拒绝）"""
    from ai_assistant.core.models import Content, Message

    return [Message(
        role="user",
        content=[Content(type="text", data="/运维 看下 211 的服务状态")],
        timestamp=None,
        metadata={
            "operator_id": "ou_test_user",
            "operator_name": "测试运维",
            "chat_id": "oc_test_chat",
            "source": "feishu",
        },
    )]


def _status_error(status_code, message):
    """造一个指定 status 的 APIStatusError 子类（异常基类取自被测模块自己的引用）"""
    from ai_assistant.providers import anthropic_provider as ap

    class _Err(ap.anthropic.APIStatusError):
        def __init__(self):
            super().__init__(message)
            self.status_code = status_code
            self.message = message
            self.response = Mock(text=message)
            self.request_id = None

    return _Err


class TestOperationsNon400ExitsStayIntact:
    """
    运维模式 APIStatusError 的**每一条出口**都必须能正常构造 error_detail。

    === Bug 背景（2026-10-27 中间态，已自修，本类是补上的覆盖）===
    `trim_reject_reason` 只在「status==400 且 round>1 且裁剪被拒」这一条
    窄分支里赋值，而 `:2191` 的 `if trim_reject_reason:` 在 except 分支的
    **每一条出口**上都要读它。_send_with_context_operations 顶部漏掉
    `trim_reject_reason = None` 初始化时：

        status=400 round=2 -> 正常          ← 唯一能跑通的 happy path
        status=403 round=2 -> UnboundLocalError
        status=429 round=3 -> UnboundLocalError
        status=500 round=2 -> UnboundLocalError
        status=400 round=1 -> UnboundLocalError   ← 连 400 本身，首轮也炸

    后果是运维模式下**额度耗尽(403/429)、网关 5xx、首轮 400 全部**在构造
    error_detail 时抛 UnboundLocalError。

    注意这个 UnboundLocalError **不会**被同函数末尾的 `except Exception`
    兜住：它是在 `except anthropic.APIStatusError` 处理器【内部】抛出的，
    Python 不会把同级 try 的其他 except 子句再套一遍。实测它一路逃出
    _send_with_context_operations → send_message → ai_provider.call()
    （`core/ai_provider.py:953` 的 `self.send_message(...)` 没有任何 try
    包裹），最后落到 `main.py:780` 的 handler 级 `except Exception` —— 那里
    只打一行日志就返回，**连回复都不会发给用户**，而且 call() 里 `:981`
    的单点告警上报也被跳过了（压根没走到）。所以真实后果比"分类丢失"更重：
    额度耗尽时用户侧完全静默、维护者侧零告警，恰好把本轮修复想解决的
    "失败既无法定位也没被兜住"反向放大。

    === 为什么现有测试一条都抓不到 ===
    已有的 400 容错用例全部打「400 + round>1」，正好落在唯一能跑通的那条
    路径上。所以本类专盯**非 400 / 首轮**这些出口。

    === 复现步骤（删掉 :1988 的初始化后必现）===
    1. 让 messages.create 首轮就抛 403（额度耗尽的常见形态）
    2. 调 _send_with_context_operations
    3. 修复前：UnboundLocalError 直接从函数里逃出来（不是返回错误元组）
       修复后：正常返回，error == "quota"，error_detail 含 "status=403"

    所以本类的断言是"能拿到带分类的返回值"而不是"error 不等于 exception"
    —— 后者在异常逃逸的形态下压根取不到 metadata，断言会死在取值那一步。

    === 这条教训（做 agentic↔operations 左右对照时）===
    逻辑分支照搬对了，**漏了一行变量初始化**。初始化在循环顶部，离被对照的
    那段代码上百行远，肉眼 diff 不在同一屏。可靠办法是 AST 核查：抓每个变量
    的 Store/Load 行号，看有没有 Load 早于首个 Store。
    """

    # 非 400 的三种常见形态 + 400 首轮。前三种在旧代码里压根到不了 400 分支，
    # 第四种进了 400 分支但 round_num==1 不满足 round>1，同样不赋值。
    CASES = [
        (403, "Error code: 403 - {'error': {'type': '<nil>', 'message': 'insufficient quota'}}", "quota"),
        (429, "Error code: 429 - {'error': {'message': 'too many requests'}}", "rate_limit"),
        (500, "Error code: 500 - {'error': {'message': 'upstream gateway failure'}}", "server"),
        (400, "Error code: 400 - {'error': {'message': 'max_tokens exceeded'}}", "api"),
    ]

    @staticmethod
    def _run(status_code, message):
        """
        跑一条出口，并把"异常逃逸"这种形态转成 AssertionError。

        缺初始化时 UnboundLocalError 会直接逃出函数（不是返回错误元组），
        裸跑的话 pytest 报的是 UnboundLocalError 而非 AssertionError。项目
        红灯规范要求 AssertionError，所以这里显式接住并断言 —— 同时这么写
        也顺带覆盖了"将来有人加个 try 把它吞成 error=exception"的形态。
        """
        provider = _provider_with_ops_raising(_status_error(status_code, message))
        try:
            return provider._send_with_context_operations(
                _ops_messages(), "", "session-ops", timeout_mode="rounds", max_rounds=3,
            )
        except Exception as e:  # noqa: BLE001 - 逃逸本身就是被测缺陷
            raise AssertionError(
                f"status={status_code} 的 APIStatusError 出口构造 error_detail 时"
                f"抛了 {type(e).__name__}: {e}。该异常是在 except 处理器【内部】"
                f"抛出的，同级 except Exception 兜不住，会一路逃到 main.py 的 "
                f"handler 级兜底 —— 用户收不到回复，告警也不会上报。"
                f"典型原因：某变量只在窄分支里赋值却在每条出口上被读，"
                f"漏了循环顶部的初始化"
            ) from e

    def test_quota_403_first_round_keeps_classification(self):
        """
        403 额度耗尽（最常见的形态）首轮就抛：必须正常走完 APIStatusError
        出口并带着分类返回，不能把异常漏出去、也不能被吞成笼统报错。
        """
        _reply, metadata = self._run(*self.CASES[0][:2])

        assert metadata.get("error") != "exception", (
            f"APIStatusError 被吞成了笼统报错（error_detail="
            f"{metadata.get('error_detail')!r}），分类和 request_id 全丢"
        )
        assert metadata.get("error") == "quota", (
            f"403 额度文案应归类为 quota，实际 {metadata.get('error')!r}"
        )
        assert "status=403" in metadata.get("error_detail", ""), (
            f"error_detail 没带 status，告警无法分类: {metadata.get('error_detail')!r}"
        )

    def test_all_non_400_and_first_round_exits_intact(self):
        """
        把 4 条出口一次性扫完：403 / 429 / 500 / 400首轮。

        逐条断言而不是只测 403，因为漏初始化影响的是**整个 except 分支**，
        单测一条无法证明别的出口也闭合。
        """
        broken = []
        for status_code, message, expect_type in self.CASES:
            _reply, metadata = self._run(status_code, message)
            detail = metadata.get("error_detail", "")
            if metadata.get("error") != expect_type or f"status={status_code}" not in detail:
                broken.append(
                    f"status={status_code}: error={metadata.get('error')!r} "
                    f"(期望 {expect_type!r}) detail={detail[:120]!r}"
                )

        assert not broken, (
            "以下出口构造 error_detail 时失败（典型原因：某个变量只在窄分支里"
            "赋值而在每条出口上被读，漏了循环顶部的初始化）:\n" + "\n".join(broken)
        )

    def test_agentic_non_400_exit_intact_too(self):
        """
        对称检查 agentic 侧的同一条出口。

        这个缺陷是做 agentic→operations 对照时漏抄初始化造成的，所以两边
        都要钉住：哪天有人反向改动（在 agentic 侧新增一个窄分支变量却忘了
        初始化），这条会红。
        """
        from ai_assistant.core.models import Content, Message

        provider = _provider_raising(_status_error(
            403, "Error code: 403 - {'error': {'message': 'insufficient quota'}}"
        ))
        messages = [Message(
            role="user", content=[Content(type="text", data="排查一下")], timestamp=None,
        )]
        try:
            _reply, metadata = provider._send_with_context_agentic(
                messages, "", "session-1", timeout_mode="rounds", max_rounds=3,
            )
        except Exception as e:  # noqa: BLE001 - 逃逸本身就是被测缺陷
            raise AssertionError(
                f"agentic 侧 403 出口抛了 {type(e).__name__}: {e}（理由同 _run 的注释）"
            ) from e

        assert metadata.get("error") == "quota", (
            f"agentic 侧 403 出口没闭合: error={metadata.get('error')!r} "
            f"detail={metadata.get('error_detail')!r}"
        )
        assert "status=403" in metadata.get("error_detail", "")
