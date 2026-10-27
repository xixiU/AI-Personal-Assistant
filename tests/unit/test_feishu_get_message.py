"""
FeishuBotAdapter.get_message（消息详情回查）测试

为什么需要这个接口：
群聊里机器人的权限是 im:message.group_at_msg:readonly，只能收到 @机器人 的消息。
用户拖入文件时飞书客户端立即发送，那条纯文件消息根本不会推 webhook —— 机器人对它
毫无感知。用户随后引用它 @机器人提问时，事件里只有 parent_id / root_id 两个 ID，
不内联被引用消息的内容，所以附件只能靠回查这个接口拿到。

已实测确认：对一条从未推送给机器人的消息，本接口返回 HTTP 200 / code=0，
能拿到 msg_type=file、chat_id、sender 与 body.content（内含 file_key）。

错误码必须分两类（关键）：参数类错误说明 message_id 本身有问题，改权限没用；
权限类错误才需要去飞书后台申请发布。把前者误读成后者会把排查方向直接带偏。
"""
import json
from unittest.mock import Mock, patch

import pytest


@pytest.fixture
def adapter():
    """最小配置的适配器，token 直接打桩避免真实网络请求"""
    from ai_assistant.adapters.feishu_bot import FeishuBotAdapter

    a = FeishuBotAdapter({
        "app_id": "cli_test",
        "app_secret": "secret_test",
        "base_url": "https://open.feishu.cn",
    })
    a.get_tenant_access_token = lambda: "t-dummy"
    return a


def _response(payload, status=200):
    resp = Mock()
    resp.status_code = status
    resp.json.return_value = payload
    resp.raise_for_status = Mock()
    return resp


def test_returns_first_item_on_success(adapter):
    """code=0 时返回 data.items[0]"""
    item = {
        "message_id": "om_file",
        "msg_type": "file",
        "chat_id": "oc_1",
        "body": {"content": json.dumps({"file_key": "file_v3_x", "file_name": "app.log"})},
    }
    payload = {"code": 0, "msg": "success", "data": {"items": [item]}}

    with patch("ai_assistant.adapters.feishu_bot.requests.get",
               return_value=_response(payload)) as mock_get:
        result = adapter.get_message("om_file")

    assert result == item
    url = mock_get.call_args[0][0]
    assert url == "https://open.feishu.cn/open-apis/im/v1/messages/om_file"
    headers = mock_get.call_args[1]["headers"]
    assert headers["Authorization"] == "Bearer t-dummy"


def test_file_key_is_extractable_from_returned_body(adapter):
    """
    回查结果必须能直接喂给 _parse_message_content 抠出 file_key。

    body.content 是**字符串化的 JSON**（不是嵌套对象），这一层容易看漏，
    漏了就会把整串当字典取、拿到 None，附件静默丢失。
    """
    from ai_assistant.adapters.feishu_bot import FeishuBotAdapter

    item = {
        "msg_type": "file",
        "body": {"content": json.dumps({"file_key": "file_v3_abc", "file_name": "app.log"})},
    }
    payload = {"code": 0, "data": {"items": [item]}}

    with patch("ai_assistant.adapters.feishu_bot.requests.get",
               return_value=_response(payload)):
        result = adapter.get_message("om_file")

    content_data = json.loads(result["body"]["content"])
    _text, _images, files = FeishuBotAdapter._parse_message_content(
        result["msg_type"], content_data
    )
    assert files == [{"file_key": "file_v3_abc", "file_name": "app.log"}]


def test_empty_message_id_short_circuits(adapter):
    """message_id 为空直接返回 None，不浪费一次网络请求"""
    with patch("ai_assistant.adapters.feishu_bot.requests.get") as mock_get:
        assert adapter.get_message("") is None
    assert mock_get.call_count == 0


@pytest.mark.parametrize("code", [99992354, 99991672, 99991671, 230001, 230002, 232001])
def test_known_error_codes_return_none(adapter, code):
    """各类业务错误码都返回 None，不抛异常把主流程带崩"""
    payload = {"code": code, "msg": "boom", "data": {}}

    with patch("ai_assistant.adapters.feishu_bot.requests.get",
               return_value=_response(payload)):
        assert adapter.get_message("om_x") is None


def test_param_and_permission_codes_are_distinguished():
    """
    参数类与权限类错误码必须分表维护（防回归）。

    99992354 是 message_id 不合法/不存在，属于参数问题；把它归进权限类会让
    日志提示"去申请权限"，而真正的原因可能只是 ID 抄错一位，排查直接跑偏。
    """
    from ai_assistant.adapters.feishu_bot import FeishuBotAdapter

    param_codes = FeishuBotAdapter._GET_MESSAGE_PARAM_CODES
    perm_codes = FeishuBotAdapter._GET_MESSAGE_PERMISSION_CODES

    assert 99992354 in param_codes, "参数类错误码缺失"
    assert 99992354 not in perm_codes, "参数类错误码被错误归入权限类"
    assert 99991672 in perm_codes, "权限类错误码缺失"
    # 两张表不得有交集，否则提示语取哪条取决于代码顺序
    assert not (set(param_codes) & set(perm_codes))


def test_empty_items_returns_none(adapter):
    """code=0 但 items 为空时返回 None（不是故障，但也没东西可用）"""
    payload = {"code": 0, "data": {"items": []}}

    with patch("ai_assistant.adapters.feishu_bot.requests.get",
               return_value=_response(payload)):
        assert adapter.get_message("om_x") is None


def test_network_exception_returns_none(adapter):
    """网络异常/网关返回 HTML 时返回 None，异常不外溢"""
    with patch("ai_assistant.adapters.feishu_bot.requests.get",
               side_effect=RuntimeError("gateway 403")):
        assert adapter.get_message("om_x") is None


def test_does_not_log_app_secret(adapter, caplog):
    """
    日志里不得出现 app_secret（项目硬规则，config.yaml 是 gitignore 的）。
    """
    payload = {"code": 99991672, "msg": "no permission", "data": {}}

    with patch("ai_assistant.adapters.feishu_bot.requests.get",
               return_value=_response(payload)):
        adapter.get_message("om_x")

    assert "secret_test" not in caplog.text
