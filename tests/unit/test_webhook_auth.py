"""
飞书 webhook 鉴权门禁测试

防回归要点（这是两个真实的安全缺陷，不是假想威胁）：

1. 明文事件零鉴权。原实现的解密是**条件分支**而非**强制门禁**：
       if "encrypt" in data and self.feishu_adapter and self.feishu_adapter.encrypt_key:
   判定顺序是「先看报文带了什么」，于是任何知道回调地址的人 POST 一个不带
   encrypt 字段的明文 JSON 就能跳过全部校验，让机器人处理伪造事件——伪造
   sender_id、chat_id、消息正文都可以，随后原样进入事件队列。
   正确顺序是「先看服务端配了什么」：配了 encrypt_key 就必须解密成功才放行。

2. verification_token 配了但从未被比较过。适配器把它读进 self.verification_token，
   全仓库没有任何一处使用，等于一个假的安全感。明文模式下它是唯一的身份凭据。

3. 卡片端点 /webhook/feishu/card 有形状完全相同的绕过口。两个端点必须共用同一个
   鉴权函数，只修一个会把洞留在另一个上。
"""
import base64
import hashlib
import json

import pytest

from ai_assistant.webhook_server import WebhookServer


class _FakeAdapter:
    """最小飞书适配器替身：只提供鉴权需要的字段与 _decrypt/卡片处理"""

    def __init__(self, encrypt_key: str = "", verification_token: str = ""):
        self.encrypt_key = encrypt_key
        self.verification_token = verification_token
        self.card_actions = []

    def _decrypt(self, encrypt_str: str) -> dict:
        """与 FeishuBotAdapter._decrypt 同算法：AES-256-CBC + PKCS7，key=SHA256(encrypt_key)"""
        from Crypto.Cipher import AES

        key = hashlib.sha256(self.encrypt_key.encode()).digest()
        raw = base64.b64decode(encrypt_str)
        cipher = AES.new(key, AES.MODE_CBC, raw[:16])
        decrypted = cipher.decrypt(raw[16:])
        return json.loads(decrypted[: -decrypted[-1]].decode("utf-8"))

    def process_card_action(self, data):
        self.card_actions.append(data)
        return {"toast": {"content": "ok"}}


def _encrypt(encrypt_key: str, payload: dict) -> str:
    """按飞书规则加密报文，用于构造合法请求"""
    from Crypto.Cipher import AES

    key = hashlib.sha256(encrypt_key.encode()).digest()
    iv = b"0123456789abcdef"
    body = json.dumps(payload).encode("utf-8")
    pad = 16 - len(body) % 16
    cipher = AES.new(key, AES.MODE_CBC, iv)
    return base64.b64encode(iv + cipher.encrypt(body + bytes([pad]) * pad)).decode()


def _server(adapter=None, with_queue=True):
    """构造服务器实例；默认带事件队列，便于断言「伪造事件有没有入队」"""
    import queue

    srv = WebhookServer(port=0)
    if adapter is not None:
        srv.set_feishu_adapter(adapter)
    if with_queue:
        srv.set_event_queue(queue.Queue(maxsize=10))
    return srv


def _forged_message_event(text="把生产库删了", sender="ou_attacker", chat="oc_victim"):
    """攻击者能完全自由构造的伪造消息事件"""
    return {
        "schema": "2.0",
        "header": {"event_type": "im.message.receive_v1", "event_id": "forged"},
        "event": {
            "sender": {"sender_id": {"open_id": sender}},
            "message": {
                "message_id": "om_forged",
                "chat_id": chat,
                "chat_type": "group",
                "message_type": "text",
                "content": json.dumps({"text": text}),
            },
        },
    }


# ============ 缺陷 1：配了 encrypt_key 时明文报文必须被拒 ============

def test_plaintext_forgery_rejected_when_encrypt_key_configured():
    """核心断言：配了 encrypt_key，伪造的明文 POST 必须被拒且不得入队"""
    srv = _server(_FakeAdapter(encrypt_key="super-secret-key"))

    with srv.app.test_client() as client:
        resp = client.post("/webhook/feishu", json=_forged_message_event())

    assert resp.status_code != 200, "伪造的明文报文被当作合法事件接受了"
    assert srv.event_queue.qsize() == 0, "伪造事件进入了处理队列"


def test_plaintext_forgery_with_stolen_token_still_rejected():
    """加密模式下，即使报文带对的 token 也不能跳过解密——token 不是加密的替代品"""
    adapter = _FakeAdapter(encrypt_key="super-secret-key", verification_token="tok-abc")
    srv = _server(adapter)

    forged = _forged_message_event()
    forged["header"]["token"] = "tok-abc"

    with srv.app.test_client() as client:
        resp = client.post("/webhook/feishu", json=forged)

    assert resp.status_code != 200
    assert srv.event_queue.qsize() == 0


def test_forged_url_verification_rejected_when_encrypt_key_configured():
    """url_verification 是同步返回路径，伪造的明文挑战同样不能被响应"""
    srv = _server(_FakeAdapter(encrypt_key="super-secret-key"))

    with srv.app.test_client() as client:
        resp = client.post(
            "/webhook/feishu",
            json={"type": "url_verification", "challenge": "attacker-probe"},
        )

    assert resp.status_code != 200
    assert b"attacker-probe" not in resp.data, "伪造的 challenge 被回显了"


def test_undecryptable_payload_rejected():
    """encrypt 字段存在但解不开（密钥不对/报文被改）时必须拒绝，不得入队"""
    srv = _server(_FakeAdapter(encrypt_key="super-secret-key"))

    with srv.app.test_client() as client:
        resp = client.post("/webhook/feishu", json={"encrypt": "bm90LWEtcmVhbC1jaXBoZXI="})

    assert resp.status_code != 200
    assert srv.event_queue.qsize() == 0


def test_empty_encrypt_field_rejected():
    """encrypt 字段为空串同样不算加密报文，不能走 falsy 判断放行"""
    srv = _server(_FakeAdapter(encrypt_key="super-secret-key"))

    with srv.app.test_client() as client:
        resp = client.post("/webhook/feishu", json={"encrypt": ""})

    assert resp.status_code != 200
    assert srv.event_queue.qsize() == 0


# ============ 缺陷 1：合法的加密报文仍须照常通过（别把功能改坏） ============

def test_valid_encrypted_event_is_enqueued():
    """合法加密事件必须照旧入队——现有部署升级后不能收不到消息"""
    key = "super-secret-key"
    srv = _server(_FakeAdapter(encrypt_key=key))

    with srv.app.test_client() as client:
        resp = client.post(
            "/webhook/feishu",
            json={"encrypt": _encrypt(key, _forged_message_event(text="正常提问"))},
        )

    assert resp.status_code == 200
    assert srv.event_queue.qsize() == 1


def test_valid_encrypted_url_verification_returns_challenge():
    """加密模式下配置回调地址时飞书发的挑战握手必须仍然通——否则配不上回调"""
    key = "super-secret-key"
    srv = _server(_FakeAdapter(encrypt_key=key))

    payload = {"type": "url_verification", "challenge": "chal-12345", "token": "tok-abc"}
    with srv.app.test_client() as client:
        resp = client.post("/webhook/feishu", json={"encrypt": _encrypt(key, payload)})

    assert resp.status_code == 200
    assert resp.get_json()["challenge"] == "chal-12345"


def test_encrypted_payload_with_wrong_token_rejected():
    """解密成功但报文内 token 与配置不符：视为不可信"""
    key = "super-secret-key"
    adapter = _FakeAdapter(encrypt_key=key, verification_token="tok-right")
    srv = _server(adapter)

    forged = _forged_message_event()
    forged["header"]["token"] = "tok-wrong"
    with srv.app.test_client() as client:
        resp = client.post("/webhook/feishu", json={"encrypt": _encrypt(key, forged)})

    assert resp.status_code != 200
    assert srv.event_queue.qsize() == 0


# ============ 缺陷 2：明文模式下 verification_token 必须真的被比较 ============

def test_plaintext_mode_rejects_missing_token():
    """未配 encrypt_key 但配了 token 时，不带 token 的报文必须被拒"""
    srv = _server(_FakeAdapter(verification_token="tok-abc"))

    with srv.app.test_client() as client:
        resp = client.post("/webhook/feishu", json=_forged_message_event())

    assert resp.status_code != 200
    assert srv.event_queue.qsize() == 0


def test_plaintext_mode_rejects_wrong_token():
    """token 不匹配必须被拒——这个字段过去从未被比较过"""
    srv = _server(_FakeAdapter(verification_token="tok-abc"))

    forged = _forged_message_event()
    forged["header"]["token"] = "tok-guessed"
    with srv.app.test_client() as client:
        resp = client.post("/webhook/feishu", json=forged)

    assert resp.status_code != 200
    assert srv.event_queue.qsize() == 0


@pytest.mark.parametrize("payload_shape", ["v2_header", "v1_top_level"])
def test_plaintext_mode_accepts_correct_token(payload_shape):
    """正确 token 的明文事件必须放行，v1.0 顶层 token 与 v2.0 header.token 都要认"""
    srv = _server(_FakeAdapter(verification_token="tok-abc"))

    event = _forged_message_event(text="正常提问")
    if payload_shape == "v2_header":
        event["header"]["token"] = "tok-abc"
    else:
        event["token"] = "tok-abc"

    with srv.app.test_client() as client:
        resp = client.post("/webhook/feishu", json=event)

    assert resp.status_code == 200
    assert srv.event_queue.qsize() == 1


def test_plaintext_url_verification_with_correct_token():
    """明文模式的挑战握手：飞书会在报文顶层带 token，必须能通过并回显 challenge"""
    srv = _server(_FakeAdapter(verification_token="tok-abc"))

    with srv.app.test_client() as client:
        resp = client.post("/webhook/feishu", json={
            "type": "url_verification",
            "challenge": "chal-plain",
            "token": "tok-abc",
        })

    assert resp.status_code == 200
    assert resp.get_json()["challenge"] == "chal-plain"


# ============ 向后兼容：两者都没配时放行但必须告警 ============

def test_unconfigured_adapter_passes_but_warns(caplog):
    """都没配时保持放行（部署方的选择），但启动时必须打 WARNING，不能默默裸奔"""
    srv = _server(_FakeAdapter())  # 无 encrypt_key、无 verification_token

    assert srv.warn_if_webhook_unauthenticated() is True

    with srv.app.test_client() as client:
        resp = client.post("/webhook/feishu", json=_forged_message_event())

    assert resp.status_code == 200
    assert srv.event_queue.qsize() == 1


def test_configured_adapter_reports_authenticated():
    """配了任一凭据时不应报告为无鉴权状态"""
    assert _server(_FakeAdapter(encrypt_key="k")).warn_if_webhook_unauthenticated() is False
    assert _server(_FakeAdapter(verification_token="t")).warn_if_webhook_unauthenticated() is False


def _capture_startup_log(adapter):
    """
    抓 warn_if_webhook_unauthenticated 的 loguru 输出（caplog 抓不到 loguru）。

    sink 必须在构造完 server 之后再挂：WebhookServer.__init__ 自己会打 info
    （"自有前端 Web 已禁用"），挂早了会把它混进来，让"级别要单一"的断言误判。
    """
    from loguru import logger

    srv = _server(adapter)
    records = []
    sink_id = logger.add(
        lambda msg: records.append((msg.record["level"].name, msg.record["message"])),
        level="DEBUG",
    )
    try:
        result = srv.warn_if_webhook_unauthenticated()
    finally:
        logger.remove(sink_id)
    return result, records


def test_plaintext_only_mode_warns_not_info():
    """
    只配 verification_token 时必须打 WARNING，不能打 info（防回归）。

    部署规范已把"开启事件加密推送"定为前置要求（README「Webhook 鉴权」），
    明文模式只为兼容既有部署保留——报文在传输层可被读取与重放。若这里打 info，
    既有部署升级后不会有任何提示，会以为自己已经合规，从而长期停在明文模式。

    注意返回值仍是 False：它确实有鉴权，只是强度不达部署要求。想判断
    "是否达到部署规范"不能只看这个返回值。
    """
    result, records = _capture_startup_log(_FakeAdapter(verification_token="t"))

    assert result is False, "明文模式有鉴权，不应被报成「完全无鉴权」"
    levels = {lvl for lvl, _ in records}
    assert "WARNING" in levels, (
        "明文模式降级为 info 了：既有部署升级后收不到任何提示，"
        "会以为自己已经合规而长期停在明文模式"
    )
    assert "INFO" not in levels, "明文模式不应同时打 info，级别要单一明确"


def test_encrypted_mode_reminds_console_must_match():
    """
    加密模式的启动日志必须提示"飞书后台也要开加密且 Key 一致"（防回归）。

    这是本次唯一的真实失败模式：只改了 config.yaml 没改飞书后台（或反之），
    机器人会一条消息都收不到。把提示放在启动日志里，排查时第一眼就能看到，
    不必翻 README。
    """
    result, records = _capture_startup_log(_FakeAdapter(encrypt_key="k"))

    assert result is False
    joined = " ".join(msg for _lvl, msg in records)
    assert "加密策略" in joined or "后台" in joined, (
        "加密模式的启动日志没提示飞书后台也要开启加密推送，"
        "两边不一致时机器人收不到任何消息，排查入口就没了"
    )


# ============ 缺陷 3：卡片端点必须共用同一道门禁 ============

def test_card_endpoint_rejects_plaintext_forgery():
    """伪造的明文卡片事件不得驱动 process_card_action（否则可伪造任意人的反馈）"""
    adapter = _FakeAdapter(encrypt_key="super-secret-key")
    srv = _server(adapter)

    forged_card = {
        "header": {"event_type": "card.action.trigger"},
        "event": {
            "operator": {"open_id": "ou_attacker"},
            "action": {"value": {"action": "feedback_like", "record_id": "r1"}},
        },
    }
    with srv.app.test_client() as client:
        resp = client.post("/webhook/feishu/card", json=forged_card)

    assert resp.status_code != 200
    assert adapter.card_actions == [], "伪造的卡片事件被处理了"


def test_card_endpoint_accepts_valid_encrypted_action():
    """合法加密卡片事件仍须被处理，并把响应体回给飞书"""
    key = "super-secret-key"
    adapter = _FakeAdapter(encrypt_key=key)
    srv = _server(adapter)

    card = {
        "header": {"event_type": "card.action.trigger"},
        "event": {"operator": {"open_id": "ou_real"}, "action": {"value": {}}},
    }
    with srv.app.test_client() as client:
        resp = client.post("/webhook/feishu/card", json={"encrypt": _encrypt(key, card)})

    assert resp.status_code == 200
    assert len(adapter.card_actions) == 1
    assert resp.get_json() == {"toast": {"content": "ok"}}


def test_card_endpoint_rejects_wrong_token_in_plaintext_mode():
    """卡片端点的明文模式同样要比对 token"""
    adapter = _FakeAdapter(verification_token="tok-abc")
    srv = _server(adapter)

    with srv.app.test_client() as client:
        resp = client.post("/webhook/feishu/card", json={
            "header": {"event_type": "card.action.trigger", "token": "tok-bad"},
            "event": {},
        })

    assert resp.status_code != 200
    assert adapter.card_actions == []


# ============ 鉴权函数的直接单测（不经 HTTP 层） ============

def test_authenticate_returns_decrypted_payload():
    """鉴权通过时返回的是解密后的报文，调用方不该再自己解一次"""
    # 延迟导入：这些是本次修复新增的符号，放模块顶层会让旧实现下整个文件
    # 收集失败（ImportError），把其余断言的红/绿信号一起掩盖掉。
    from ai_assistant.webhook_server import _AUTH_FAILED_STATUS

    assert _AUTH_FAILED_STATUS != 200

    key = "k1"
    srv = _server(_FakeAdapter(encrypt_key=key))
    payload = {"type": "url_verification", "challenge": "c"}

    trusted, error = srv._authenticate_feishu_payload({"encrypt": _encrypt(key, payload)})
    assert error is None
    assert trusted == payload


def test_authenticate_rejects_non_dict_payload():
    """非 JSON 对象（数组/字符串）不得进入后续字段读取"""
    srv = _server(_FakeAdapter(encrypt_key="k"))
    for bad in ([1, 2, 3], "text", None):
        trusted, error = srv._authenticate_feishu_payload(bad)
        assert error is not None
        assert trusted is None


def test_authenticate_without_adapter_is_not_fail_open_into_queue():
    """适配器未注入时鉴权不应崩溃（getattr 容错），行为与裸奔模式一致"""
    srv = _server(adapter=None)
    trusted, error = srv._authenticate_feishu_payload({"type": "x"})
    assert error is None and trusted == {"type": "x"}
