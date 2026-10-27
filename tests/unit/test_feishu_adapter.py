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


# url_verification 挑战握手已从适配器搬到 HTTP 层（webhook_server.handle_feishu_webhook），
# 必须在鉴权门禁之后同步回显 challenge，适配器的 process_webhook_event 对这类报文返回 None。
# 该行为的防回归测试在 tests/unit/test_webhook_auth.py:174（加密模式）与 :246（明文模式），
# 原先这里的 test_feishu_bot_url_verification 已是重复覆盖，故删除。


def test_feishu_bot_check_trigger_no_event():
    """无事件时 check_trigger 应返回 False"""
    adapter = _make_adapter()
    assert adapter.check_trigger("【ai】") is False


def test_feishu_bot_fills_latest_message():
    """触发时必须把消息正文与会话类型填进 latest_message，供后续取用

    这是本文件里唯一守着 latest_message 填充的测试。原名 test_..._with_keyword
    是误导：check_trigger 在飞书 bot 模式下忽略 keyword（见下一条测试），
    传任何关键词都会返回 True，所以"关键词被识别"从来不是这里在测的东西。
    """
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


def test_feishu_bot_triggers_without_keyword():
    """不含关键词的消息同样触发：飞书 bot 模式按设计无条件触发，keyword 被忽略

    提交 7bcecb5「优化飞书webhook回调」在同一处删掉了 `if keyword in text` 分支，
    并把 docstring 改成「keyword 参数保留用于兼容接口，但不实际使用」——
    删逻辑与改文档成对出现，是有意的设计变更，不是重构漏改。理由见
    feishu_bot.py:819「用户主动发给机器人的消息本身就是意图明确的」。

    keyword 参数仍保留在签名里，因为微信 UI 自动化适配器真的在用它
    （wechat_adapter.py:202/:259/:418/:448 有实质比较）。

    这条断言钉住当前设计：谁把关键词门槛加回飞书侧，这里会红，迫使他
    同时面对 README 与 config.example.yaml 的描述。
    """
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
    assert result is True
    assert adapter.latest_message["text"] == "普通消息"
