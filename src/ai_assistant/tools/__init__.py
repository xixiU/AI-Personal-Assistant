"""
工具模块

提供 AI 可调用的工具，支持 Agentic 模式下的自主排查
"""

from ai_assistant.tools.operations_commands import (
    OperationCommand,
    CommandResult,
    CommandRegistry,
    get_command_registry,
    GetAppStatusCommand,
    GetAppVersionCommand,
    GetJarInfoCommand,
    GetProcessInfoCommand,
    GetSystemMetricsCommand,
    GetLogsCommand,
    RestartAppCommand,
    StopAppCommand,
    StartAppCommand,
)

from ai_assistant.tools.operations_tools import OperationsTools

__all__ = [
    # Commands
    "OperationCommand",
    "CommandResult",
    "CommandRegistry",
    "get_command_registry",
    "GetAppStatusCommand",
    "GetAppVersionCommand",
    "GetJarInfoCommand",
    "GetProcessInfoCommand",
    "GetSystemMetricsCommand",
    "GetLogsCommand",
    "RestartAppCommand",
    "StopAppCommand",
    "StartAppCommand",
    # Tools
    "OperationsTools",
]
