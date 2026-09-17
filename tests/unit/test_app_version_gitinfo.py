"""
Java 版本/分支查询默认走企业规范位置的测试

背景（企业规范）：Java 应用的版本/分支/commit 由构建流水线写入 jar 内
BOOT-INF/classes/git.info。MANIFEST.MF 只有 Implementation-Version 之类的
粗粒度信息，拿不到分支和 commit。

原缺陷复现步骤：
1. 运维模式问"184 上 ts-service 什么分支"
2. Claude 从进程 cmdline 拿到绝对 jar_path，调 get_app_version
3. 若该 jar_path 未命中 config.yaml 里带 file_path 的 version_detection 配置
   （新应用、路径变更、或 AI 拿到的路径与配置 path 前缀不一致），
   jar_file_path 为空 → 工具硬编码读 META-INF/MANIFEST.MF → 拿不到分支

回归重点：
1. 不传 jar_file_path 时，第一个尝试的必须是 BOOT-INF/classes/git.info
2. git.info 读不到才回退 META-INF/MANIFEST.MF（老 jar 兼容）
3. 显式传了 jar_file_path 时不做候选回退，尊重调用方原意
4. version_detection 未配 file_path 时不再硬写 MANIFEST.MF
"""
import sys
import types
from unittest.mock import Mock, patch

import pytest

# paramiko 可能未安装（本项目按规范只做静态测试，不要求装齐运行时依赖）。
# 注入最小桩模块：本测试全程 mock _execute_ssh_command，不会真建 SSH 连接。
if "paramiko" not in sys.modules:
    _stub = types.ModuleType("paramiko")
    _stub.SSHClient = Mock
    _stub.AutoAddPolicy = Mock
    _stub.RSAKey = Mock
    _stub.SSHException = type("SSHException", (Exception,), {})
    _stub.AuthenticationException = type("AuthenticationException", (Exception,), {})
    sys.modules["paramiko"] = _stub

# cryptography.fernet.Fernet 用于密码解密，本测试不涉及真实连接，直接桩掉
if "cryptography" not in sys.modules:
    _crypto = types.ModuleType("cryptography")
    _fernet = types.ModuleType("fernet")
    _fernet.Fernet = Mock
    _crypto.fernet = _fernet
    sys.modules["cryptography"] = _crypto
    sys.modules["cryptography.fernet"] = _fernet

from ai_assistant.core.operations_models import Machine, SSHConfig
from ai_assistant.tools.operations_commands import CommandResult, GetAppVersionCommand


@pytest.fixture
def machine():
    return Machine(
        name="研发主服务器",
        display_name="184",
        ssh_config=SSHConfig(host="10.0.0.184", username="deploy", password=None),
    )


@pytest.fixture
def command():
    return GetAppVersionCommand(cipher=Mock())


def _ssh_stub(available: dict):
    """
    模拟 SSH：命令里出现哪个 jar 内文件路径，就返回对应内容。
    available 未covered 的路径返回 marker（模拟 unzip 读不到）。
    """
    def _run(ssh_config, cmd, timeout=None):
        for path, content in available.items():
            if path in cmd:
                return CommandResult(success=True, data=content, raw_output=content)
        return CommandResult(
            success=True,
            data="___GITINFO_NOT_FOUND___",
            raw_output="___GITINFO_NOT_FOUND___",
        )
    return _run


# ============ 核心：默认优先 git.info ============

def test_default_reads_gitinfo_first(command, machine):
    """回归：不传 jar_file_path 时必须优先读 BOOT-INF/classes/git.info"""
    git_info = '{"branch":"release-4.3.5","commitId":"a1b2c3d"}'
    with patch.object(
        command, "_execute_ssh_command",
        side_effect=_ssh_stub({"BOOT-INF/classes/git.info": git_info}),
    ):
        result = command.execute(machine, source="jar", jar_path="/app/ts-service-5.0.jar")

    assert result.success
    assert result.data["file_path"] == "BOOT-INF/classes/git.info"
    assert "release-4.3.5" in result.data["content"]


def test_gitinfo_tried_before_manifest(command, machine):
    """两个文件都存在时，仍必须取 git.info（含分支），而不是 MANIFEST"""
    with patch.object(
        command, "_execute_ssh_command",
        side_effect=_ssh_stub({
            "BOOT-INF/classes/git.info": '{"branch":"master"}',
            "META-INF/MANIFEST.MF": "Implementation-Version: 5.0.1",
        }),
    ) as mocked:
        result = command.execute(machine, source="jar", jar_path="/app/a.jar")

    assert result.data["file_path"] == "BOOT-INF/classes/git.info"
    # 命中即返回，不应再去读 MANIFEST
    assert mocked.call_count == 1


def test_falls_back_to_manifest_when_no_gitinfo(command, machine):
    """老 jar 没有 git.info → 回退 MANIFEST.MF，不能直接失败"""
    with patch.object(
        command, "_execute_ssh_command",
        side_effect=_ssh_stub({"META-INF/MANIFEST.MF": "Implementation-Version: 4.2.0"}),
    ) as mocked:
        result = command.execute(machine, source="jar", jar_path="/app/legacy.jar")

    assert result.success
    assert result.data["file_path"] == "META-INF/MANIFEST.MF"
    assert mocked.call_count == 2  # 先试 git.info，失败后试 MANIFEST


def test_both_missing_returns_failure(command, machine):
    """两个候选都读不到时返回失败，且错误信息可定位"""
    with patch.object(command, "_execute_ssh_command", side_effect=_ssh_stub({})):
        result = command.execute(machine, source="jar", jar_path="/app/empty.jar")

    assert not result.success
    assert result.error


# ============ 显式指定时不回退 ============

def test_explicit_file_path_no_fallback(command, machine):
    """显式传 jar_file_path → 只读该文件，不做候选回退"""
    with patch.object(
        command, "_execute_ssh_command",
        side_effect=_ssh_stub({"custom/version.txt": "v9.9.9"}),
    ) as mocked:
        result = command.execute(
            machine, source="jar",
            jar_path="/app/a.jar", jar_file_path="custom/version.txt",
        )

    assert result.success
    assert result.data["file_path"] == "custom/version.txt"
    assert mocked.call_count == 1


def test_explicit_file_path_missing_does_not_try_gitinfo(command, machine):
    """显式指定的文件不存在时直接失败，不偷偷改读 git.info（避免答非所问）"""
    with patch.object(
        command, "_execute_ssh_command",
        side_effect=_ssh_stub({"BOOT-INF/classes/git.info": '{"branch":"x"}'}),
    ) as mocked:
        result = command.execute(
            machine, source="jar",
            jar_path="/app/a.jar", jar_file_path="not/exist.txt",
        )

    assert not result.success
    assert mocked.call_count == 1


# ============ version_detection 自动模式 ============

def test_auto_detect_without_file_path_uses_gitinfo():
    """version_detection 里没写 file_path 时，不再硬写 MANIFEST.MF"""
    from ai_assistant.tools.operations_tools import OperationsTools

    tools = OperationsTools(operations_manager=Mock())
    fake_cmd = Mock()
    fake_cmd.execute.return_value = CommandResult(
        success=True, data={"file_path": "BOOT-INF/classes/git.info", "content": "branch=main"}
    )

    tools._auto_detect_version(
        Mock(),
        [{"type": "jar_manifest", "jar_path": "app.jar"}],
        fake_cmd,
    )

    # jar_file_path 应为 None，把候选决策权交给 command 层
    assert fake_cmd.execute.call_args.kwargs["jar_file_path"] is None


def test_auto_detect_respects_configured_file_path():
    """配置里显式写了 file_path 时按配置走"""
    from ai_assistant.tools.operations_tools import OperationsTools

    tools = OperationsTools(operations_manager=Mock())
    fake_cmd = Mock()
    fake_cmd.execute.return_value = CommandResult(success=True, data={"content": "x"})

    tools._auto_detect_version(
        Mock(),
        [{"type": "jar_manifest", "jar_path": "app.jar", "file_path": "BOOT-INF/classes/git.info"}],
        fake_cmd,
    )

    assert fake_cmd.execute.call_args.kwargs["jar_file_path"] == "BOOT-INF/classes/git.info"
