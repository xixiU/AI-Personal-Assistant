"""
Trace Context - 请求链路追踪上下文

使用 contextvars 实现类似 Java MDC 的功能，在多线程环境中传递 trace_id
"""
import uuid
from contextvars import ContextVar
from typing import Optional

# 全局 ContextVar，存储当前请求的 trace_id
_trace_id_var: ContextVar[Optional[str]] = ContextVar('trace_id', default=None)


def generate_trace_id() -> str:
    """
    生成新的 trace_id（12 位十六进制）

    Returns:
        trace_id 字符串
    """
    return uuid.uuid4().hex[:12]


def set_trace_id(trace_id: str):
    """
    设置当前上下文的 trace_id

    Args:
        trace_id: trace_id 字符串
    """
    _trace_id_var.set(trace_id)


def get_trace_id() -> str:
    """
    获取当前上下文的 trace_id，如果没有则返回 "-"

    Returns:
        trace_id 字符串或 "-"
    """
    trace_id = _trace_id_var.get()
    return trace_id if trace_id else "-"


def clear_trace_id():
    """清除当前上下文的 trace_id"""
    _trace_id_var.set(None)


def with_new_trace_id():
    """
    生成并设置新的 trace_id

    Returns:
        新生成的 trace_id
    """
    trace_id = generate_trace_id()
    set_trace_id(trace_id)
    return trace_id
