"""测试延迟重试队列的消费逻辑（_check_and_retry_pending_messages）"""
import pytest
import queue
from unittest.mock import Mock

from ai_assistant.main import AIAssistant


def test_index_ready_clears_queue_and_enqueues_messages():
    """
    行为 1：索引就绪 → 队列清空、消息进了 event_queue

    验证：
    - pending_retry_queue 被清空（全部 pop 出来）
    - 每条消息的 event_data 进入 event_queue
    - 返回后 pending_retry_queue 为空
    """
    # 绕过 __init__，手动构造最小化实例
    app = AIAssistant.__new__(AIAssistant)
    app._last_indexing_check_time = 0
    app.pending_retry_queue = [
        (100.0, {"message_id": "m1", "text": "msg1"}),
        (200.0, {"message_id": "m2", "text": "msg2"}),
    ]
    app.event_queue = queue.Queue()

    # Mock doc_manager: 索引已就绪
    app.doc_manager = Mock()
    app.doc_manager.is_index_ready.return_value = True

    # 调用消费逻辑
    now = 10_000.0
    app._check_and_retry_pending_messages(now)

    # 断言 1：pending_retry_queue 被清空
    assert len(app.pending_retry_queue) == 0, \
        "索引就绪后 pending_retry_queue 应被清空"

    # 断言 2：event_queue 中有两条消息
    assert app.event_queue.qsize() == 2, \
        "两条消息应进入 event_queue"

    # 断言 3：消息内容正确（按 FIFO 顺序）
    msg1 = app.event_queue.get_nowait()
    assert msg1["message_id"] == "m1"
    msg2 = app.event_queue.get_nowait()
    assert msg2["message_id"] == "m2"

    # 断言 4：_last_indexing_check_time 已更新
    assert app._last_indexing_check_time == now


def test_index_not_ready_preserves_queue():
    """
    行为 2（核心）：索引未就绪 → 队列原样保留，event_queue 为空

    这是重试机制的关键：索引还在更新时不能丢消息，要等下次心跳再试。
    """
    app = AIAssistant.__new__(AIAssistant)
    app._last_indexing_check_time = 0
    original_queue = [
        (100.0, {"message_id": "m1"}),
        (200.0, {"message_id": "m2"}),
    ]
    app.pending_retry_queue = list(original_queue)  # 复制一份以便对比
    app.event_queue = queue.Queue()

    # Mock doc_manager: 索引未就绪
    app.doc_manager = Mock()
    app.doc_manager.is_index_ready.return_value = False

    # 调用消费逻辑
    now = 10_000.0
    app._check_and_retry_pending_messages(now)

    # 断言 1：pending_retry_queue 完全不变
    assert app.pending_retry_queue == original_queue, \
        "索引未就绪时 pending_retry_queue 必须原样保留"

    # 断言 2：event_queue 仍为空
    assert app.event_queue.qsize() == 0, \
        "索引未就绪时不应往 event_queue 放消息"

    # 断言 3：_last_indexing_check_time 已更新（即使未处理，也记录检查时间）
    assert app._last_indexing_check_time == now


def test_no_doc_manager_clears_queue():
    """
    行为 3：doc_manager 为 None → 队列被清空（会丢消息的分支）

    这是降级逻辑：没有文档管理器时无法重试，只能清空队列。
    必须有测试看着，防止意外进入这个分支导致消息丢失。
    """
    app = AIAssistant.__new__(AIAssistant)
    app._last_indexing_check_time = 0
    app.pending_retry_queue = [
        (100.0, {"message_id": "m1"}),
    ]
    app.event_queue = queue.Queue()
    app.doc_manager = None  # 无文档管理器

    # 调用消费逻辑
    now = 10_000.0
    app._check_and_retry_pending_messages(now)

    # 断言 1：pending_retry_queue 被清空
    assert len(app.pending_retry_queue) == 0, \
        "无 doc_manager 时应清空队列"

    # 断言 2：event_queue 为空（消息被丢弃，未入队）
    assert app.event_queue.qsize() == 0, \
        "无 doc_manager 时消息不会进入 event_queue"


def test_throttle_skips_when_under_30s():
    """
    行为 4：30 秒限流 - now 距上次不足 30s 时直接返回，队列不动

    防止频繁检查索引状态，主循环每次心跳都会调用，但实际只每 30s 执行一次。
    """
    app = AIAssistant.__new__(AIAssistant)
    app._last_indexing_check_time = 9980.0  # 上次检查时间
    app.pending_retry_queue = [
        (100.0, {"message_id": "m1"}),
    ]
    app.event_queue = queue.Queue()
    app.doc_manager = Mock()
    app.doc_manager.is_index_ready.return_value = True

    # 调用消费逻辑（距离上次只有 20 秒）
    now = 10_000.0
    app._check_and_retry_pending_messages(now)

    # 断言 1：pending_retry_queue 未变化
    assert len(app.pending_retry_queue) == 1, \
        "限流期内 pending_retry_queue 不应被处理"

    # 断言 2：event_queue 为空
    assert app.event_queue.qsize() == 0, \
        "限流期内不应往 event_queue 放消息"

    # 断言 3：_last_indexing_check_time 未更新
    assert app._last_indexing_check_time == 9980.0, \
        "限流期内不应更新检查时间"

    # 断言 4：is_index_ready 未被调用（提前返回）
    assert app.doc_manager.is_index_ready.call_count == 0, \
        "限流期内不应调用 is_index_ready"


def test_event_queue_full_puts_message_back():
    """
    行为 5：event_queue 满 → 消息放回队列头部，不丢

    保证消息不会因队列满而丢失，会在下次心跳重试。
    """
    app = AIAssistant.__new__(AIAssistant)
    app._last_indexing_check_time = 0
    app.pending_retry_queue = [
        (100.0, {"message_id": "m1"}),
        (200.0, {"message_id": "m2"}),
    ]

    # 创建一个 maxsize=1 的队列，并填满
    app.event_queue = queue.Queue(maxsize=1)
    app.event_queue.put({"dummy": "占位"})

    app.doc_manager = Mock()
    app.doc_manager.is_index_ready.return_value = True

    # 调用消费逻辑
    now = 10_000.0
    app._check_and_retry_pending_messages(now)

    # 断言 1：pending_retry_queue 不为空（m1 被放回）
    assert len(app.pending_retry_queue) == 2, \
        "队列满时消息应被放回 pending_retry_queue"

    # 断言 2：放回的消息在队列头部
    ts, data = app.pending_retry_queue[0]
    assert data["message_id"] == "m1", \
        "放回的消息应在队列头部"

    # 断言 3：event_queue 仍为满（只有占位的 dummy）
    assert app.event_queue.qsize() == 1
