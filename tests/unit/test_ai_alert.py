"""
AI 调用失败告警测试

覆盖三件事：
1. 错误分类：网关把额度耗尽返回 403（而非 402）时仍能识别为 quota——
   这是本次线上问题的原始形态，回归重点。
2. 统一收口：Provider 在 metadata 里写了 error，AIProvider.call() 就必须触发告警；
   成功调用不得告警。
3. 抑制机制：同类错误在窗口内只发一条，避免持续性故障刷爆告警群。
"""
import time
from unittest.mock import Mock, patch

import pytest

from ai_assistant.core.ai_provider import (
    AIProvider,
    classify_api_error,
    friendly_api_error_message,
)
from ai_assistant.core.models import Content, Message
from ai_assistant.utils import ai_alert


# ============ 错误分类 ============

def test_classify_403_quota_from_gateway():
    """回归：new-api 网关把额度耗尽返回 403，必须归类为 quota 而非 api/auth"""
    message = (
        "Error code: 403 - {'error': {'type': 'new_api_error', 'message': "
        "'token quota is not enough, token remain quota: $1.749678, need quota: $2.121788'}}"
    )
    assert classify_api_error(403, message) == "quota"


def test_classify_402_quota():
    assert classify_api_error(402, "payment required") == "quota"


def test_classify_401_auth():
    assert classify_api_error(401, "invalid x-api-key") == "auth"


def test_classify_403_without_quota_hint_is_auth():
    """403 排除额度后按权限不足处理"""
    assert classify_api_error(403, "permission denied for this model") == "auth"


def test_classify_429_rate_limit():
    assert classify_api_error(429, "too many requests") == "rate_limit"


def test_classify_5xx_server():
    assert classify_api_error(503, "upstream unavailable") == "server"
    assert classify_api_error(500, "") == "server"


def test_classify_other_is_api():
    assert classify_api_error(400, "invalid request body") == "api"


def test_classify_chinese_quota_hint():
    """中文网关文案同样识别"""
    assert classify_api_error(400, "当前分组下该模型额度不足") == "quota"


def test_friendly_message_hides_upstream_detail_for_quota():
    """额度错误不向用户回显余额、request id 等运维信息"""
    msg = friendly_api_error_message("quota", "token remain quota: $1.749678")
    assert "1.749678" not in msg
    assert "额度" in msg or "余额" in msg


def test_friendly_message_keeps_detail_for_generic_api_error():
    msg = friendly_api_error_message("api", "invalid request body")
    assert "invalid request body" in msg


# ============ 告警器行为 ============

@pytest.fixture(autouse=True)
def _reset_alerter():
    """每个用例使用独立的告警器状态，避免抑制窗口互相干扰"""
    alerter = ai_alert.get_ai_alerter()
    alerter.configure(enabled=True, webhook="http://fake-webhook")
    alerter._last_sent.clear()
    yield
    alerter.configure(enabled=True, webhook=None)
    alerter._last_sent.clear()


def _sent_cards(mock_post):
    return [c.kwargs["json"] for c in mock_post.call_args_list]


@patch("requests.post")
def test_report_sends_card(mock_post):
    mock_post.return_value = Mock(status_code=200)
    assert ai_alert.report_ai_error("quota", "token quota is not enough", provider="AnthropicProvider") is True
    # 发送在守护线程中进行，等待其完成
    time.sleep(0.5)
    cards = _sent_cards(mock_post)
    assert len(cards) == 1
    text = str(cards[0])
    assert "AI 服务调用失败告警" in text
    assert "quota" in text


@patch("requests.post")
def test_no_webhook_no_send(mock_post):
    ai_alert.get_ai_alerter().configure(enabled=True, webhook=None)
    assert ai_alert.report_ai_error("quota", "x") is False
    assert mock_post.call_count == 0


@patch("requests.post")
def test_disabled_no_send(mock_post):
    ai_alert.get_ai_alerter().configure(enabled=False, webhook="http://fake-webhook")
    assert ai_alert.report_ai_error("quota", "x") is False
    assert mock_post.call_count == 0


@patch("requests.post")
def test_same_error_suppressed_within_window(mock_post):
    """同类错误只发一次；数值变化（剩余额度）不应绕过抑制"""
    mock_post.return_value = Mock(status_code=200)
    assert ai_alert.report_ai_error("quota", "token remain quota: $1.74, need $2.12") is True
    assert ai_alert.report_ai_error("quota", "token remain quota: $1.70, need $2.30") is False
    assert ai_alert.report_ai_error("quota", "token remain quota: $1.68, need $2.40") is False
    time.sleep(0.5)
    assert len(_sent_cards(mock_post)) == 1


@patch("requests.post")
def test_different_error_types_not_suppressed(mock_post):
    mock_post.return_value = Mock(status_code=200)
    assert ai_alert.report_ai_error("quota", "no quota") is True
    assert ai_alert.report_ai_error("timeout", "read timeout") is True
    time.sleep(0.5)
    assert len(_sent_cards(mock_post)) == 2


@patch("requests.post")
def test_suppressed_count_reported_after_window(mock_post):
    """窗口过后再发时，带上期间被抑制的次数"""
    mock_post.return_value = Mock(status_code=200)
    ai_alert.report_ai_error("quota", "no quota")
    ai_alert.report_ai_error("quota", "no quota")
    ai_alert.report_ai_error("quota", "no quota")
    # 手动把上次发送时间推到窗口之外，模拟时间流逝
    alerter = ai_alert.get_ai_alerter()
    sig, (_, suppressed) = next(iter(alerter._last_sent.items()))
    alerter._last_sent[sig] = (time.time() - ai_alert._SUPPRESS_WINDOW_SECONDS - 1, suppressed)
    assert ai_alert.report_ai_error("quota", "no quota") is True
    time.sleep(0.5)
    assert "抑制 2 次" in str(_sent_cards(mock_post)[-1])


@patch("requests.post")
def test_webhook_failure_does_not_raise(mock_post):
    """告警通道本身故障不得影响主流程"""
    mock_post.side_effect = RuntimeError("network down")
    assert ai_alert.report_ai_error("quota", "x") is True
    time.sleep(0.5)


@patch("requests.post")
def test_alert_content_is_redacted(mock_post):
    """告警内容经过脱敏，不把密钥原文带进告警群"""
    mock_post.return_value = Mock(status_code=200)
    ai_alert.report_ai_error("auth", "invalid api_key=sk-abcdef1234567890abcdef")
    time.sleep(0.5)
    assert "sk-abcdef1234567890abcdef" not in str(_sent_cards(mock_post))


# ============ call() 统一收口 ============

class _StubProvider(AIProvider):
    """返回预置结果的最小 Provider，用于验证 call() 的告警收口"""

    model = "stub-model"

    def __init__(self, result):
        super().__init__()
        self._result = result

    def _send_with_context(self, messages, doc_context, session_id=None):
        return self._result

    def check_health(self) -> bool:
        return True


def _msgs():
    return [Message(role="user", content=[Content(type="text", data="问题")], timestamp=None)]


@patch("ai_assistant.utils.ai_alert.AIErrorAlerter.report")
def test_call_reports_error_from_metadata(mock_report):
    """Provider 在 metadata 写了 error，call() 必须上报告警"""
    p = _StubProvider(("💳 AI 服务额度/余额不足", {
        "mode": "agentic", "error": "quota", "error_detail": "status=403, quota not enough",
    }))
    p.call(_msgs(), session_id="oc_test", source="feishu")
    assert mock_report.call_count == 1
    kwargs = mock_report.call_args.kwargs
    assert kwargs["error_type"] == "quota"
    assert kwargs["mode"] == "agentic"
    assert kwargs["source"] == "feishu"
    assert kwargs["session_id"] == "oc_test"
    assert "quota not enough" in kwargs["detail"]


@patch("ai_assistant.utils.ai_alert.AIErrorAlerter.report")
def test_call_no_alert_on_success(mock_report):
    p = _StubProvider(("正常回复", {"mode": "rag", "doc_count": 2}))
    p.call(_msgs(), session_id="oc_test", source="feishu")
    assert mock_report.call_count == 0


@patch("ai_assistant.utils.ai_alert.AIErrorAlerter.report")
def test_call_no_alert_on_plain_string_reply(mock_report):
    """兼容旧的纯字符串返回值，不误报"""
    p = _StubProvider("正常回复")
    p.call(_msgs(), session_id="oc_test", source="feishu")
    assert mock_report.call_count == 0


@patch("ai_assistant.utils.ai_alert.AIErrorAlerter.report")
def test_call_alert_failure_does_not_break_reply(mock_report):
    """告警上报抛异常时，用户仍能拿到回复"""
    mock_report.side_effect = RuntimeError("alert boom")
    p = _StubProvider(("❌ AI 服务调用失败", {"mode": "rag", "error": "api"}))
    reply, _record_id, metadata = p.call(_msgs(), session_id="oc_test", source="feishu")
    assert "AI 服务调用失败" in reply
    assert metadata["error"] == "api"
