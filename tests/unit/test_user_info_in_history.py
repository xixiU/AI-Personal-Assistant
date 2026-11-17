"""
测试用户信息在对话历史中的保存和传递

核心能力：
1. chat_history.save() 能接收并保存 user_id 和 user_name
2. ai_provider.call() 能接收用户信息并传递给 chat_history
3. 用户信息为空时不影响保存（兼容其他来源）
"""

import json
import tempfile
from pathlib import Path
from datetime import datetime

from ai_assistant.core.chat_history import ChatHistoryManager
from ai_assistant.core.models import Message, Content


def test_chat_history_saves_user_info():
    """chat_history.save() 能保存用户信息到 JSONL"""
    with tempfile.TemporaryDirectory() as tmpdir:
        manager = ChatHistoryManager(history_dir=tmpdir)

        record_id = manager.save(
            session_id="test_chat_123",
            query="测试问题",
            answer="测试回答",
            latency_ms=100,
            source="feishu",
            user_id="ou_test_user_123",
            user_name="张三",
        )

        # 验证 record_id 生成
        assert record_id is not None

        # 读取保存的 JSONL 文件
        today = datetime.now().strftime("%Y-%m-%d")
        file_path = Path(tmpdir) / f"{today}.jsonl"
        assert file_path.exists()

        with open(file_path, "r", encoding="utf-8") as f:
            lines = f.readlines()
            assert len(lines) == 1
            record = json.loads(lines[0])

            # 核心断言：用户信息被正确保存
            assert record["user_id"] == "ou_test_user_123"
            assert record["user_name"] == "张三"
            assert record["query"] == "测试问题"
            assert record["answer"] == "测试回答"
            assert record["source"] == "feishu"


def test_chat_history_without_user_info():
    """用户信息为空时不影响保存（兼容 web、wechat）"""
    with tempfile.TemporaryDirectory() as tmpdir:
        manager = ChatHistoryManager(history_dir=tmpdir)

        # 不传 user_id 和 user_name
        record_id = manager.save(
            session_id="web_session_456",
            query="Web 端问题",
            answer="Web 端回答",
            source="web",
        )

        assert record_id is not None

        # 读取保存的记录
        today = datetime.now().strftime("%Y-%m-%d")
        file_path = Path(tmpdir) / f"{today}.jsonl"

        with open(file_path, "r", encoding="utf-8") as f:
            record = json.loads(f.read())

            # 核心断言：没有用户信息字段（或为空）
            assert "user_id" not in record or record.get("user_id") is None
            assert "user_name" not in record or record.get("user_name") is None
            assert record["source"] == "web"


def test_ai_provider_accepts_user_info():
    """ai_provider.call() 能接收用户信息参数"""
    from unittest.mock import Mock, patch
    from ai_assistant.core.ai_provider import AIProvider
    from ai_assistant.core.models import Message, Content

    # 创建一个 mock provider
    class MockProvider(AIProvider):
        def _send_with_context(self, messages, doc_context, session_id):
            return "mock reply", {}

        def check_health(self):
            return True

    with tempfile.TemporaryDirectory() as tmpdir:
        manager = ChatHistoryManager(history_dir=tmpdir)
        AIProvider.set_chat_history(manager)

        provider = MockProvider()

        messages = [
            Message(
                role="user",
                content=[Content(type="text", data="测试问题")],
                timestamp=datetime.now()
            )
        ]

        # 调用时传入用户信息
        reply, record_id, metadata = provider.call(
            messages,
            session_id="feishu_chat_789",
            source="feishu",
            user_id="ou_feishu_user_456",
            user_name="李四",
        )

        # 验证回复和 record_id
        assert reply == "mock reply"
        assert record_id is not None

        # 验证用户信息被保存
        today = datetime.now().strftime("%Y-%m-%d")
        file_path = Path(tmpdir) / f"{today}.jsonl"

        with open(file_path, "r", encoding="utf-8") as f:
            record = json.loads(f.read())

            # 核心断言：用户信息通过 call() 传递到 save()
            assert record["user_id"] == "ou_feishu_user_456"
            assert record["user_name"] == "李四"
