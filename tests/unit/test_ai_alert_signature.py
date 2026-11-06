"""
告警抑制指纹与 summarize_text 摘要的配合

缺陷（修复前）：
  ai_alert._signature 取 `re.sub(r"\\d+", "N", detail)[:120]` 做抑制指纹，
  目的是让"剩余额度 1.74/需要 2.12"这类每次都变的数值不绕过抑制。
  但 Provider 写进 error_detail 的上游文案是 summarize_text 摘要，超过 80 字符
  （Anthropic 侧传了 limit=200，即超过 200 字符）就会附 `sha1=67b7b6d9`。
  数值一变 sha1 就变，其中 a~f 字母不受数字替换影响，且落在前 120 字符内，
  于是同一个额度故障每次提问都被当成新错误，告警群被刷爆。

复现步骤：
  1. 上游返回额度不足，文案里带每次都变的余额数字，且文案超过摘要整段显示门槛
  2. 连续提问两次
  3. 修复前：两次 error_detail 的 sha1 不同 → 签名不同 → 发出两条告警
     修复后：签名先抹掉 sha1 字段再替换数字 → 第二条被抑制，只发一条

防修过头：
  只抹 `sha1=` 前缀的字段。不带该前缀的十六进制串（网关 request id）能区分
  不同故障，必须保留。注意 error_type 不同的两条错误签名前缀本就不同，
  拿它们做反向用例没有鉴别力，反向用例必须是同一 error_type。
"""
import re

import pytest

from ai_assistant.utils import ai_alert
from ai_assistant.utils.log_summary import summarize_text


@pytest.fixture
def alerter(monkeypatch):
    """独立的告警器状态；不起发送线程（只验证抑制判定）"""
    a = ai_alert.get_ai_alerter()
    a.configure(enabled=True, webhook="http://fake-webhook")
    a._last_sent.clear()
    monkeypatch.setattr(a, "_send_safe", lambda *args: None)
    yield a
    a.configure(enabled=True, webhook=None)
    a._last_sent.clear()


def _quota_body(remain: str, need: str) -> str:
    """网关额度报错（两次之间只有余额数字不同，长度相同）"""
    return (
        '{"error":{"message":"当前分组额度不足，剩余额度 %s，本次需要 %s，'
        '请联系管理员充值后重试 quota exceeded for this group","type":"insufficient_quota"}}'
        % (remain, need)
    )


def _openai_detail(status: int, body: str) -> str:
    """OpenAI 通道 HTTPError 分支的 error_detail 形态（状态码 + 长度 + 摘要）"""
    return f"status={status}, len={len(body)}, response={summarize_text(body, label='upstream_msg')}"


def _anthropic_detail(status: int, body: str) -> str:
    """Anthropic 通道 APIStatusError 出口的 error_detail 形态（_ALERT_MSG_CHARS=200）"""
    return (
        f"status={status}, round=2, "
        f"message={summarize_text(body, limit=200, label='upstream_msg')}"
    )


def _sha1_in_window(detail: str) -> str:
    """返回落在前 120 字符签名窗口内的 sha1 字段值（没有则返回空串）"""
    m = re.search(r"sha1=([0-9a-f]+)", detail[:120])
    return m.group(1) if m else ""


@pytest.mark.parametrize(
    "make_detail, pad",
    [
        (_openai_detail, ""),
        # Anthropic 侧 limit=200：文案超过 200 字符才带 sha1
        (_anthropic_detail, " " + "x" * 120),
    ],
    ids=["openai", "anthropic"],
)
def test_signature_same_quota_with_changing_balance_suppressed(alerter, make_detail, pad):
    """只差余额数字的额度报错经摘要后，第二条必须被抑制"""
    d1 = make_detail(403, _quota_body("1.74", "2.12") + pad)
    d2 = make_detail(403, _quota_body("0.93", "2.40") + pad)

    # 前置：确认摘要确实把不同的 sha1 带进了签名窗口（否则本用例测不到缺陷）
    s1, s2 = _sha1_in_window(d1), _sha1_in_window(d2)
    assert s1 and s2 and s1 != s2, f"构造失效，sha1 未进窗口或相同: {s1!r} {s2!r}"

    assert ai_alert.report_ai_error("quota", d1) is True
    assert ai_alert.report_ai_error("quota", d2) is False, (
        "同一额度故障只因余额数字不同就绕过了抑制：额度耗尽时每次提问都会发一条告警"
    )


def test_signature_keeps_request_id_hex_distinct(alerter):
    """
    同为 api 类型、签名窗口内只差一个不带 sha1= 前缀的十六进制 request id：
    两条都不得被抑制（防止把替换泛化成"所有十六进制串"）
    """
    tail = " invalid request parameters, please check the model name and request format and retry"
    d1 = _openai_detail(400, "request id: req_3fa9c1e7" + tail)
    d2 = _openai_detail(400, "request id: req_b2d84f0a" + tail)

    # 前置：两条的 request id 都落在签名窗口内（否则窗口外的差异本来就不参与判定）
    assert "req_3fa9c1e7" in d1[:120] and "req_b2d84f0a" in d2[:120], d1[:120]

    assert ai_alert.report_ai_error("api", d1) is True
    assert ai_alert.report_ai_error("api", d2) is True, (
        "两条 request id 不同的故障被合并抑制：替换范围超出了 sha1= 字段"
    )


def test_signature_different_error_types_both_sent(alerter):
    """
    常规用例：额度不足 vs Key 失效都要发出。
    注意它不能证明替换没有修过头：签名带 error_type 前缀，两者本就不会被合并。
    """
    d_quota = _openai_detail(403, _quota_body("1.74", "2.12"))
    d_auth = _openai_detail(
        401,
        '{"error":{"message":"Incorrect API key provided: sk-****abcd. You can find your '
        'API key in your account settings.","type":"invalid_request_error"}}',
    )
    assert ai_alert.report_ai_error("quota", d_quota) is True
    assert ai_alert.report_ai_error("auth", d_auth) is True
