"""
测试飞书反馈容错功能：缓存未命中时通过 API 查找 record_id
"""

import json
import tempfile
from pathlib import Path
from datetime import datetime

import pytest

from ai_assistant.core.chat_history import ChatHistoryManager
from ai_assistant.adapters.feishu_bot import FeishuBotAdapter


class TestChatHistoryFindByQuery:
    """测试 ChatHistoryManager.find_record_by_query() 方法"""

    def test_find_record_by_query_success(self):
        """能找到匹配的 record_id"""
        with tempfile.TemporaryDirectory() as tmpdir:
            mgr = ChatHistoryManager(history_dir=tmpdir)

            # 保存几条记录
            record_id1 = mgr.save(
                session_id="chat_123",
                query="这是第一个问题",
                answer="这是第一个回答",
                source="feishu",
            )
            record_id2 = mgr.save(
                session_id="chat_123",
                query="这是第二个问题",
                answer="这是第二个回答",
                source="feishu",
            )
            mgr.save(
                session_id="chat_456",  # 不同 session
                query="这是第一个问题",  # query 相同但 session 不同
                answer="其他回答",
                source="feishu",
            )

            # 查找第一条记录
            found = mgr.find_record_by_query("chat_123", "这是第一个问题", source="feishu")
            assert found == record_id1

            # 查找第二条记录
            found = mgr.find_record_by_query("chat_123", "这是第二个问题", source="feishu")
            assert found == record_id2

    def test_find_record_by_query_not_found(self):
        """query 不匹配时返回 None"""
        with tempfile.TemporaryDirectory() as tmpdir:
            mgr = ChatHistoryManager(history_dir=tmpdir)

            mgr.save(
                session_id="chat_123",
                query="已有的问题",
                answer="已有的回答",
                source="feishu",
            )

            # 查找不存在的 query
            found = mgr.find_record_by_query("chat_123", "不存在的问题", source="feishu")
            assert found is None

            # session_id 不匹配
            found = mgr.find_record_by_query("chat_999", "已有的问题", source="feishu")
            assert found is None

            # source 不匹配
            found = mgr.find_record_by_query("chat_123", "已有的问题", source="wechat")
            assert found is None

    def test_find_record_by_query_strip_whitespace(self):
        """query 会 strip 后比较"""
        with tempfile.TemporaryDirectory() as tmpdir:
            mgr = ChatHistoryManager(history_dir=tmpdir)

            record_id = mgr.save(
                session_id="chat_123",
                query="  问题带空格  ",
                answer="回答",
                source="feishu",
            )

            # 不同的空格应该能匹配
            found = mgr.find_record_by_query("chat_123", "问题带空格", source="feishu")
            assert found == record_id

            found = mgr.find_record_by_query("chat_123", "  问题带空格", source="feishu")
            assert found == record_id

    def test_find_record_by_query_returns_latest(self):
        """重复提问时返回最新的 record_id"""
        with tempfile.TemporaryDirectory() as tmpdir:
            mgr = ChatHistoryManager(history_dir=tmpdir)

            # 同一个问题提问两次
            mgr.save(
                session_id="chat_123",
                query="重复的问题",
                answer="第一次回答",
                source="feishu",
            )
            record_id2 = mgr.save(
                session_id="chat_123",
                query="重复的问题",
                answer="第二次回答",
                source="feishu",
            )

            # 应该返回最新的（第二次）
            found = mgr.find_record_by_query("chat_123", "重复的问题", source="feishu")
            assert found == record_id2


class TestExtractQueryFromMessage:
    """测试 FeishuBotAdapter._extract_query_from_message() 方法"""

    def test_extract_text_message(self):
        """能从 text 类型消息提取 query"""
        adapter = FeishuBotAdapter({"app_id": "test", "app_secret": "test"})

        message_data = {
            "msg_type": "text",
            "body": {
                "content": json.dumps({"text": "这是一个文本问题"})
            }
        }

        query = adapter._extract_query_from_message(message_data)
        assert query == "这是一个文本问题"

    def test_extract_image_message(self):
        """image 类型返回默认附件提问"""
        adapter = FeishuBotAdapter({"app_id": "test", "app_secret": "test"})

        message_data = {
            "msg_type": "image",
            "body": {
                "content": json.dumps({"image_key": "img_xxx"})
            }
        }

        query = adapter._extract_query_from_message(message_data)
        assert query == "请分析这个日志文件中的问题。"

    def test_extract_file_message(self):
        """file 类型返回默认附件提问"""
        adapter = FeishuBotAdapter({"app_id": "test", "app_secret": "test"})

        message_data = {
            "msg_type": "file",
            "body": {
                "content": json.dumps({"file_key": "file_xxx"})
            }
        }

        query = adapter._extract_query_from_message(message_data)
        assert query == "请分析这个日志文件中的问题。"

    def test_extract_post_message(self):
        """能从 post 类型（富文本）提取文本内容"""
        adapter = FeishuBotAdapter({"app_id": "test", "app_secret": "test"})

        message_data = {
            "msg_type": "post",
            "body": {
                "content": json.dumps({
                    "zh_cn": {
                        "title": "",
                        "content": [
                            [
                                {"tag": "text", "text": "这是富文本"},
                                {"tag": "text", "text": "第二部分"}
                            ]
                        ]
                    }
                })
            }
        }

        query = adapter._extract_query_from_message(message_data)
        assert query == "这是富文本 第二部分"

    def test_extract_empty_message(self):
        """空消息返回 None"""
        adapter = FeishuBotAdapter({"app_id": "test", "app_secret": "test"})

        query = adapter._extract_query_from_message(None)
        assert query is None

        query = adapter._extract_query_from_message({})
        assert query is None
