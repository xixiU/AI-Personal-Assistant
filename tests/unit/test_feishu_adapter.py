import json
from unittest.mock import patch, MagicMock
from ai_assistant.adapters.feishu_bot import FeishuBotAdapter


def _make_adapter():
    return FeishuBotAdapter({
        "app_id": "test_app_id",
        "app_secret": "test_secret",
        "verification_token": "test_token",
    })


def test_feishu_bot_adapter_initialization():
    """测试飞书机器人适配器初始化"""
    adapter = _make_adapter()

    assert adapter.app_id == "test_app_id"
    assert adapter.app_secret == "test_secret"
    assert adapter.latest_event is None
    assert adapter.latest_message is None


def test_feishu_bot_url_verification():
    """测试 URL 验证响应"""
    adapter = _make_adapter()

    event_data = {"type": "url_verification", "challenge": "abc123"}
    response = adapter.handle_webhook_event(event_data)

    assert response == {"challenge": "abc123"}


def test_feishu_bot_check_trigger_no_event():
    """无事件时 check_trigger 应返回 False"""
    adapter = _make_adapter()
    assert adapter.check_trigger("【ai】") is False


def test_feishu_bot_check_trigger_with_keyword():
    """消息包含关键词时应触发"""
    adapter = _make_adapter()
    adapter.latest_event = {
        "header": {"event_type": "im.message.receive_v1", "event_id": "e1"},
        "event": {
            "message": {
                "message_id": "msg_001",
                "chat_id": "oc_xxx",
                "chat_type": "p2p",
                "message_type": "text",
                "content": json.dumps({"text": "【ai】你好"}),
            },
            "sender": {
                "sender_id": {"open_id": "ou_xxx"}
            }
        }
    }

    result = adapter.check_trigger("【ai】")
    assert result is True
    assert adapter.latest_message["text"] == "【ai】你好"
    assert adapter.latest_message["chat_type"] == "p2p"


def test_feishu_bot_check_trigger_no_keyword():
    """消息不含关键词时不触发"""
    adapter = _make_adapter()
    adapter.latest_event = {
        "header": {"event_type": "im.message.receive_v1", "event_id": "e2"},
        "event": {
            "message": {
                "message_id": "msg_002",
                "chat_id": "oc_xxx",
                "chat_type": "p2p",
                "message_type": "text",
                "content": json.dumps({"text": "普通消息"}),
            },
            "sender": {
                "sender_id": {"open_id": "ou_xxx"}
            }
        }
    }

    result = adapter.check_trigger("【ai】")
    assert result is False
