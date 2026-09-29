"""
运维工具调用的参数隔离与 grep 语义回归测试

=== Bug 背景（2026-09-29 线上现象）===
用户在运维模式让 AI 查 SQL 异常日志，AI 从第 7 轮一直循环到超时（600s），
日志里交替出现两种失败，谁都推进不了：
    执行运维工具: get_logs({'grep_pattern': '...', 'lines': 5000, 'log_path': '...'})
    → {'success': False, 'error': '缺少 machine_name 参数'}
    执行运维工具: get_logs({'machine_name': '测试211服务器', 'grep_pattern': '...', ...})
    → {'success': False, 'error': 'Command failed with exit code 1: '}
用户等了 10 分钟，最终拿到超时提示，一条有效信息都没有。

=== 三个叠加的根因 ===
1. 【参数污染，死循环的驱动力】_execute_operations_tool 里
   `tool_input.pop('machine_name')` 改的是 Claude 响应对象 block.input 那个 dict，
   而调用方已把它的【引用】放进了 api_messages 的 assistant 消息。
   于是 AI 明明传了 machine_name，执行后它自己的历史被就地抹成"没传"。
   下一轮回传时，AI 看到自己两次发的都是"没有 machine_name 的同一个调用"，
   一次报缺参数、一次报 exit 1 —— 证据自相矛盾，它永远学不会该怎么传。
   同理 tool_input['jar_file_path'] / ['source'] 会反向污染成"AI 传了
   系统提示词明确要求不要传的参数"。
2. 【grep 语法】get_logs 用裸 grep（BRE），而 AI 写的模式几乎总带 | 和 ()，
   BRE 下这些是字面字符，把整个模式当一个字符串找，必然无匹配。
3. 【grep 无匹配被当错误】grep 无匹配退出码 1 是正常语义（git_tools.py:307
   早已正确处理，运维侧漏了）。旧实现一律按失败返回，且 stderr 为空，
   错误信息就是那句什么都没有的 "Command failed with exit code 1: "，
   AI 无法区分"没匹配到"和"路径/权限有问题"，只能反复换模式重试。

=== 复现步骤（修复前必现）===
1. 运维模式提问，让 AI 查某台机器日志里的异常，如"查下 211 上 ts-service 的 SQL 报错"
2. AI 第一次调用 get_logs 时带上 machine_name，参数被 pop 走
3. 下一轮 AI 复述历史时看到自己"没传 machine_name"，重新传一次
4. 传对了也因为裸 grep + exit 1 被当失败，错误信息为空
5. 2→4 循环，直到 600s 超时

=== 回归关注点（改动本文件对应逻辑时务必保证）===
1. _execute_operations_tool 绝不能就地修改传入的 tool_input（pop/赋值都不行），
   必须先 dict() 拷贝 —— 否则 1 号根因立刻复发
2. get_logs 的 grep 必须是 -E（ERE），否则带 | 的模式永远匹配不到
3. grep 无匹配（exit 1）必须返回 success=True + matched=False，不能当错误
4. 非匹配类失败（exit 2、路径不存在等）仍须如实报错，不能被 3 掩盖
5. stderr 为空时错误信息不能退化成没有信息的空串
6. 完全相同(工具,参数)的重复失败要有兜底提示，但不得拦截同名工具的正常连续调用
"""

import json
import sys
import types
from unittest.mock import Mock, patch

# 运行环境未安装的依赖用桩模块替换（与项目既有单测一致，仅做静态验证）
for _name in ("anthropic", "paramiko", "pywinauto", "jieba"):
    if _name not in sys.modules:
        _stub = types.ModuleType(_name)
        _stub.__getattr__ = lambda _attr: Mock()  # type: ignore[attr-defined]
        sys.modules[_name] = _stub

from ai_assistant.providers.anthropic_provider import AnthropicProvider  # noqa: E402
from ai_assistant.tools.operations_commands import (  # noqa: E402
    CommandResult,
    GetLogsCommand,
)


# ==================== 测试夹具 ====================


def _provider() -> AnthropicProvider:
    """构造一个不真正发请求的 provider"""
    with patch("ai_assistant.providers.anthropic_provider.anthropic.Anthropic"):
        return AnthropicProvider(api_key="test-key")


def _machine(name: str = "测试211服务器"):
    """最小可用的 Machine 替身（只需要 ssh_config 属性被透传）"""
    machine = Mock()
    machine.name = name
    machine.ssh_config = Mock(timeout=10)
    return machine


def _provider_with_tools(tool_impl=None, machine=None):
    """
    装好 operations_tools 的 provider

    Args:
        tool_impl: get_logs 的假实现，签名 (machine, **kwargs)
        machine: find_machine 的返回值，None 表示找不到机器
    """
    provider = _provider()

    manager = Mock()
    manager.machines = {"测试211服务器": _machine(), "测试212服务器": _machine("测试212服务器")}
    manager.find_machine = Mock(return_value=machine)
    manager.applications = {}

    tools = Mock()
    tools.operations_manager = manager

    def default_impl(machine, **kwargs):
        return {"success": True, "data": kwargs, "error": None}

    impl = tool_impl or default_impl

    # 用真函数而非 Mock，才能让 inspect.signature 看到 machine 参数
    def get_logs(machine, log_path, lines=100, grep_pattern=None):
        return impl(
            machine, log_path=log_path, lines=lines, grep_pattern=grep_pattern
        )

    tools.get_logs = get_logs
    provider.operations_tools = tools
    return provider


class _FakeSSH:
    """记录下发命令并按预设返回退出码的 _execute_ssh_command 替身"""

    def __init__(self, exit_code: int = 0, output: str = "", error: str = ""):
        self.exit_code = exit_code
        self.output = output
        self.error = error
        self.commands = []

    def __call__(self, ssh_config, command, timeout=None):
        self.commands.append(command)
        if self.exit_code == 0:
            return CommandResult(
                success=True, data=self.output, raw_output=self.output, exit_code=0
            )
        detail = self.error.strip() or "(命令无 stderr 输出)"
        return CommandResult(
            success=False,
            error=f"Command failed with exit code {self.exit_code}: {detail}",
            raw_output=self.error,
            exit_code=self.exit_code,
        )


# ==================== 1. 参数隔离（核心回归）====================


class TestToolInputNotMutated:
    """_execute_operations_tool 不得就地修改 Claude 的 tool_input"""

    def test_machine_name_not_popped_from_caller_dict(self):
        """
        核心回归：执行后调用方那份 tool_input 必须仍然带着 machine_name。

        这正是死循环的根因——被 pop 掉后，AI 自己的历史记录里就"没传过"
        machine_name，导致它永远学不会该传什么。
        """
        provider = _provider_with_tools(machine=_machine())

        tool_input = {
            "machine_name": "测试211服务器",
            "log_path": "/var/log/app.log",
            "lines": 5000,
        }
        snapshot = dict(tool_input)

        result = provider._execute_operations_tool("get_logs", tool_input)

        assert result["success"] is True
        assert tool_input == snapshot, "tool_input 被就地修改了，死循环 bug 会复发"
        assert "machine_name" in tool_input

    def test_api_messages_history_stays_intact(self):
        """
        还原真实链路：assistant 消息里存的是 block.input 的引用，
        执行工具后这段历史必须仍然完整（AI 下一轮看到的就是这份历史）。
        """
        provider = _provider_with_tools(machine=_machine())

        block_input = {"machine_name": "测试211服务器", "log_path": "/var/log/app.log"}
        api_messages = [{
            "role": "assistant",
            "content": [{
                "type": "tool_use", "id": "t1", "name": "get_logs", "input": block_input
            }],
        }]

        provider._execute_operations_tool("get_logs", block_input)

        recorded = api_messages[0]["content"][0]["input"]
        assert recorded["machine_name"] == "测试211服务器", \
            "AI 的历史被改成了'没传 machine_name'，会导致它反复重试"

    def test_repeated_calls_same_input_stay_stable(self):
        """同一份参数连续调用两次结果必须一致（第一次不能把第二次搞坏）"""
        provider = _provider_with_tools(machine=_machine())
        tool_input = {"machine_name": "测试211服务器", "log_path": "/var/log/app.log"}

        first = provider._execute_operations_tool("get_logs", tool_input)
        second = provider._execute_operations_tool("get_logs", tool_input)

        assert first["success"] is True
        assert second["success"] is True, "第一次调用污染了参数，第二次失败"

    def test_machine_name_not_forwarded_to_tool(self):
        """machine_name 是给调度层用的，不能透传进工具方法（否则 TypeError）"""
        received = {}

        def impl(machine, **kwargs):
            received.update(kwargs)
            return {"success": True, "data": None, "error": None}

        provider = _provider_with_tools(tool_impl=impl, machine=_machine())
        provider._execute_operations_tool(
            "get_logs", {"machine_name": "测试211服务器", "log_path": "/a.log"}
        )

        assert "machine_name" not in received
        assert received["log_path"] == "/a.log"


class TestMissingMachineNameError:
    """缺少 machine_name 时的报错要能指导 AI 下一步"""

    def test_error_lists_available_machines(self):
        """光说"缺少"不够，要告诉 AI 有哪些机器可选"""
        provider = _provider_with_tools(machine=_machine())

        result = provider._execute_operations_tool("get_logs", {"log_path": "/a.log"})

        assert result["success"] is False
        assert "machine_name" in result["error"]
        assert "测试211服务器" in result["error"], "未列出可选机器，AI 只能瞎猜"

    def test_machine_not_found_reports_name(self):
        """机器名找不到时如实报错，并回显 AI 传的名字"""
        provider = _provider_with_tools(machine=None)

        result = provider._execute_operations_tool(
            "get_logs", {"machine_name": "不存在的机器", "log_path": "/a.log"}
        )

        assert result["success"] is False
        assert "不存在的机器" in result["error"]


# ==================== 2. grep 语法与无匹配语义 ====================


class TestGetLogsGrepSyntax:
    """grep_pattern 必须按 ERE 解析"""

    def test_uses_grep_extended_regex(self):
        """
        核心回归：必须是 grep -E。
        裸 grep 是 BRE，AI 写的 'A|B' 会被当字面串，永远匹配不到。
        """
        cmd = GetLogsCommand()
        fake = _FakeSSH(exit_code=0, output="line1\nline2")
        cmd._execute_ssh_command = fake  # type: ignore[method-assign]

        cmd.execute(
            _machine(), log_path="/var/log/app.log", lines=5000,
            grep_pattern="SQLException|Deadlock",
        )

        assert len(fake.commands) == 1
        assert "grep -E" in fake.commands[0], "不是 -E，带 | 的模式必然匹配不到"

    def test_pattern_is_shell_quoted(self):
        """模式含 | () 等 shell 元字符，必须被引号包住，防止命令注入与语法错误"""
        cmd = GetLogsCommand()
        fake = _FakeSSH(exit_code=0, output="x")
        cmd._execute_ssh_command = fake  # type: ignore[method-assign]

        cmd.execute(
            _machine(), log_path="/var/log/app.log",
            grep_pattern="2026-09-29.*(ERROR|WARN)",
        )

        command = fake.commands[0]
        assert "'2026-09-29.*(ERROR|WARN)'" in command

    def test_no_grep_when_pattern_absent(self):
        """不传 grep_pattern 时不应拼接 grep"""
        cmd = GetLogsCommand()
        fake = _FakeSSH(exit_code=0, output="x")
        cmd._execute_ssh_command = fake  # type: ignore[method-assign]

        cmd.execute(_machine(), log_path="/var/log/app.log", lines=100)

        assert "grep" not in fake.commands[0]
        assert "tail -n 100" in fake.commands[0]


class TestGetLogsNoMatchSemantics:
    """grep 无匹配（exit 1）是结论，不是错误"""

    def test_exit_1_with_pattern_is_success(self):
        """
        核心回归：有 grep_pattern 且 exit 1 → success=True + matched=False。
        旧实现返回 success=False + 空错误信息，AI 只能反复换模式重试到超时。
        """
        cmd = GetLogsCommand()
        cmd._execute_ssh_command = _FakeSSH(exit_code=1)  # type: ignore[method-assign]

        result = cmd.execute(
            _machine(), log_path="/var/log/app.log", grep_pattern="SQLException|Deadlock"
        )

        assert result.success is True, "无匹配被当成失败，AI 会反复重试"
        assert result.data["matched"] is False
        assert result.data["lines"] == []
        assert result.data["lines_count"] == 0

    def test_no_match_note_tells_ai_not_to_retry(self):
        """结论里要明确"别用同一模式重试"，否则 AI 仍可能打转"""
        cmd = GetLogsCommand()
        cmd._execute_ssh_command = _FakeSSH(exit_code=1)  # type: ignore[method-assign]

        result = cmd.execute(
            _machine(), log_path="/var/log/app.log", grep_pattern="ERROR"
        )

        assert "重试" in result.data["note"]
        assert "ERROR" in result.data["note"]

    def test_exit_1_without_pattern_still_error(self):
        """没有 grep 时 exit 1 就是真失败（如文件不存在），不能被误判为无匹配"""
        cmd = GetLogsCommand()
        cmd._execute_ssh_command = _FakeSSH(  # type: ignore[method-assign]
            exit_code=1, error="tail: cannot open '/nope': No such file or directory"
        )

        result = cmd.execute(_machine(), log_path="/nope")

        assert result.success is False
        assert "No such file" in result.error

    def test_exit_2_is_real_error(self):
        """exit 2 是 grep 语法错/文件不可读等真错误，必须如实上报"""
        cmd = GetLogsCommand()
        cmd._execute_ssh_command = _FakeSSH(  # type: ignore[method-assign]
            exit_code=2, error="grep: Invalid regular expression"
        )

        result = cmd.execute(
            _machine(), log_path="/var/log/app.log", grep_pattern="[invalid"
        )

        assert result.success is False
        assert "Invalid regular expression" in result.error

    def test_successful_match_returns_lines(self):
        """正常匹配到内容时按行返回，且标记来自成功路径"""
        cmd = GetLogsCommand()
        cmd._execute_ssh_command = _FakeSSH(  # type: ignore[method-assign]
            exit_code=0, output="ERROR a\nERROR b"
        )

        result = cmd.execute(
            _machine(), log_path="/var/log/app.log", grep_pattern="ERROR"
        )

        assert result.success is True
        assert result.data["lines_count"] == 2
        assert result.data["lines"] == ["ERROR a", "ERROR b"]


class TestEmptyStderrMessage:
    """stderr 为空时错误信息不能退化成空串"""

    def test_blank_stderr_gets_placeholder(self):
        """
        旧实现产出 "Command failed with exit code 1: "（冒号后什么都没有），
        排查时完全看不出发生了什么。
        """
        cmd = GetLogsCommand()
        fake = _FakeSSH(exit_code=3, error="   ")
        cmd._execute_ssh_command = fake  # type: ignore[method-assign]

        result = cmd.execute(_machine(), log_path="/a.log")

        assert result.success is False
        assert not result.error.rstrip().endswith(":"), "错误信息以冒号结尾，等于没有信息"
        assert "无 stderr" in result.error

    def test_exit_code_exposed_on_result(self):
        """CommandResult 要带 exit_code，调用方才能区分无匹配与真错误"""
        cmd = GetLogsCommand()
        cmd._execute_ssh_command = _FakeSSH(exit_code=0, output="x")  # type: ignore[method-assign]

        result = cmd.execute(_machine(), log_path="/a.log")

        assert result.exit_code == 0
        assert "exit_code" in result.to_dict()


# ==================== 3. 重复失败兜底 ====================


class TestRepeatedFailureGuard:
    """完全相同(工具,参数)的重复失败要提示 AI 换策略"""

    @staticmethod
    def _run_guard(results):
        """
        复刻循环里的兜底逻辑，逐个喂入工具结果，返回每轮的 result_str。

        直接跑 _send_with_context_operations 需要 mock 整个 Anthropic 流式响应，
        这里只验证判定规则本身。
        """
        failed_calls = {}
        outputs = []
        for tool_name, tool_input, result in results:
            result_str = json.dumps(result, ensure_ascii=False)
            if isinstance(result, dict) and not result.get("success", True):
                call_key = (
                    tool_name,
                    json.dumps(tool_input, ensure_ascii=False, sort_keys=True),
                )
                failed_calls[call_key] = failed_calls.get(call_key, 0) + 1
                if failed_calls[call_key] >= 3:
                    result_str = json.dumps({
                        **result, "repeated_failure": True, "hint": "换策略"
                    }, ensure_ascii=False)
            outputs.append(result_str)
        return outputs

    def test_third_identical_failure_gets_hint(self):
        """同工具同参数第 3 次失败时注入提示"""
        fail = {"success": False, "error": "boom"}
        call = ("get_logs", {"machine_name": "m1", "log_path": "/a.log"}, fail)

        outputs = self._run_guard([call, call, call])

        assert "repeated_failure" not in outputs[0]
        assert "repeated_failure" not in outputs[1]
        assert "repeated_failure" in outputs[2]

    def test_different_params_not_counted_together(self):
        """换了参数就是新的尝试，不该被合并计数"""
        fail = {"success": False, "error": "boom"}
        outputs = self._run_guard([
            ("get_logs", {"machine_name": "m1", "log_path": "/a.log"}, fail),
            ("get_logs", {"machine_name": "m2", "log_path": "/a.log"}, fail),
            ("get_logs", {"machine_name": "m3", "log_path": "/a.log"}, fail),
        ])

        assert all("repeated_failure" not in o for o in outputs)

    def test_key_order_does_not_affect_dedup(self):
        """参数顺序不同但内容相同，应视为同一个调用（sort_keys 保证）"""
        fail = {"success": False, "error": "boom"}
        outputs = self._run_guard([
            ("get_logs", {"machine_name": "m1", "log_path": "/a.log"}, fail),
            ("get_logs", {"log_path": "/a.log", "machine_name": "m1"}, fail),
            ("get_logs", {"machine_name": "m1", "log_path": "/a.log"}, fail),
        ])

        assert "repeated_failure" in outputs[2]

    def test_success_never_triggers_guard(self):
        """成功的调用无论重复多少次都不该被拦（连续查多台机器是正常操作）"""
        ok = {"success": True, "data": {"lines": []}}
        call = ("get_logs", {"machine_name": "m1", "log_path": "/a.log"}, ok)

        outputs = self._run_guard([call] * 5)

        assert all("repeated_failure" not in o for o in outputs)

    def test_same_tool_different_machines_not_blocked(self):
        """
        回归 commit 9a4addc 的教训：不能因为"连续调同一个工具"就拦。
        巡检 5 台机器全部成功，是完全正常的用法。
        """
        ok = {"success": True, "data": {}}
        outputs = self._run_guard([
            ("get_logs", {"machine_name": f"m{i}", "log_path": "/a.log"}, ok)
            for i in range(5)
        ])

        assert all("repeated_failure" not in o for o in outputs)
