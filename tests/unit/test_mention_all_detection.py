"""
@所有人 判定测试

背景 bug：配置了 trigger.ignore_mention_all: true，但「别人在群里 @所有人」
机器人照常回复，而「自己 @所有人」能正确被忽略。
根因：_parse_feishu_event 只在 message_type == "text" 分支里查 content 文本中的
字面量 "@_all"，而飞书富文本(post)消息的 @所有人 是独立的 at 节点
（{"tag":"at","user_id":"all"}），文本里没有任何 @_all 痕迹，
于是 post 类型的 @所有人 完全绕过了判定（图片/文件分支同样没查）。

复现步骤：
1. config.yaml 设 trigger.ignore_mention_all: true
2. 在群里用「富文本」消息（带图片或多段格式的消息）@所有人
3. 机器人仍然回复；日志里看不到"忽略包含 @_all 的消息"，反而走进
   "解析富文本消息: text_len=..., images=1" 并调用 AI
4. 换成纯文本消息 @所有人 → 正常被忽略（对比差异所在）

核心规则（回归重点）：
1. 三种表达都要认：mentions 数组 key="@_all"、text 字面量 "@_all"、post 的 at 节点
2. 判定必须在类型分发之前统一收口，post/image/file 分支都受保护
3. at 节点可能嵌在任意层级，需递归查找
4. @具体某人（user_id 是真实 open_id）不能被误判为 @所有人
"""
import json
import sys
import types
from unittest.mock import Mock

# 运行时依赖可能未安装（本项目按规范只做静态测试），注入最小桩模块，
# 保证被测的 @所有人 判定逻辑可独立测试。
for _name in ("anthropic", "paramiko", "pywinauto", "jieba"):
    if _name not in sys.modules:
        _stub = types.ModuleType(_name)
        _stub.__getattr__ = lambda _attr: Mock()  # type: ignore[attr-defined]
        sys.modules[_name] = _stub

if "cryptography" not in sys.modules:
    _crypto = types.ModuleType("cryptography")
    _fernet = types.ModuleType("fernet")
    _fernet.Fernet = Mock
    _crypto.fernet = _fernet
    sys.modules["cryptography"] = _crypto
    sys.modules["cryptography.fernet"] = _fernet

from ai_assistant.main import AIAssistant


def _text_msg(text: str, mentions=None) -> dict:
    """构造飞书 text 类型消息"""
    msg = {
        "message_type": "text",
        "content": json.dumps({"text": text}, ensure_ascii=False),
    }
    if mentions is not None:
        msg["mentions"] = mentions
    return msg


def _post_msg(content_data: dict, mentions=None) -> dict:
    """构造飞书 post(富文本) 类型消息"""
    msg = {
        "message_type": "post",
        "content": json.dumps(content_data, ensure_ascii=False),
    }
    if mentions is not None:
        msg["mentions"] = mentions
    return msg


class TestMentionAllText:
    """text 类型：字面量占位符"""

    def test_text_with_at_all_detected(self):
        assert AIAssistant._is_mention_all(_text_msg("@_all 大家看下需求")) is True

    def test_text_without_at_all_not_detected(self):
        assert AIAssistant._is_mention_all(_text_msg("@_user_1 帮我看下")) is False

    def test_plain_text_not_detected(self):
        assert AIAssistant._is_mention_all(_text_msg("4.4.0 加了两块需求")) is False


class TestMentionAllPost:
    """post 类型：at 节点（原始 bug 所在）"""

    def test_post_at_node_user_id_all(self):
        """核心回归：富文本 @所有人，文本里没有 @_all，靠 at 节点识别"""
        msg = _post_msg({
            "zh_cn": {
                "content": [[
                    {"tag": "at", "user_id": "all", "user_name": "所有人"},
                    {"tag": "text", "text": "4.4.0加了两块需求，大家看下"},
                    {"tag": "img", "image_key": "img_v3_xxx"},
                ]]
            }
        })
        assert AIAssistant._is_mention_all(msg) is True

    def test_post_at_node_by_user_name(self):
        """部分部署 user_id 可能缺失，靠 user_name 兜底"""
        msg = _post_msg({
            "zh_cn": {"content": [[{"tag": "at", "user_name": "所有人"}]]}
        })
        assert AIAssistant._is_mention_all(msg) is True

    def test_post_at_specific_user_not_detected(self):
        """@具体某人 不能误判为 @所有人"""
        msg = _post_msg({
            "zh_cn": {
                "content": [[
                    {"tag": "at", "user_id": "ou_e0a7f4cb59c1a75e", "user_name": "rjyuan2"},
                    {"tag": "text", "text": "帮我看下这个日志"},
                ]]
            }
        })
        assert AIAssistant._is_mention_all(msg) is False

    def test_post_at_node_deeply_nested(self):
        """at 节点嵌在更深层级时也要能找到"""
        msg = _post_msg({
            "zh_cn": {
                "content": [[
                    {"tag": "text", "text": "前言"},
                    {"tag": "div", "content": [[{"tag": "at", "user_id": "all"}]]},
                ]]
            }
        })
        assert AIAssistant._is_mention_all(msg) is True

    def test_post_without_lang_wrapper(self):
        """私有化部署可能无语言包裹"""
        msg = _post_msg({"content": [[{"tag": "at", "user_id": "all"}]]})
        assert AIAssistant._is_mention_all(msg) is True

    def test_post_no_at_node_not_detected(self):
        msg = _post_msg({
            "zh_cn": {
                "content": [[
                    {"tag": "text", "text": "看下这个截图"},
                    {"tag": "img", "image_key": "img_v3_xxx"},
                ]]
            }
        })
        assert AIAssistant._is_mention_all(msg) is False

    def test_post_malformed_content_no_crash(self):
        """content 不是合法 JSON 时不应抛异常"""
        msg = {"message_type": "post", "content": "not-a-json"}
        assert AIAssistant._is_mention_all(msg) is False


class TestMentionAllMentionsArray:
    """mentions 数组：飞书最可靠的来源"""

    def test_mentions_key_at_all(self):
        msg = _post_msg(
            {"zh_cn": {"content": [[{"tag": "text", "text": "大家看下"}]]}},
            mentions=[{"key": "@_all", "id": {"open_id": "all"}, "name": "所有人"}],
        )
        assert AIAssistant._is_mention_all(msg) is True

    def test_mentions_open_id_all(self):
        msg = _post_msg(
            {"zh_cn": {"content": [[{"tag": "text", "text": "大家看下"}]]}},
            mentions=[{"key": "@_user_1", "id": {"open_id": "all"}}],
        )
        assert AIAssistant._is_mention_all(msg) is True

    def test_mentions_specific_user_not_detected(self):
        msg = _post_msg(
            {"zh_cn": {"content": [[{"tag": "text", "text": "帮看下"}]]}},
            mentions=[{"key": "@_user_1", "id": {"open_id": "ou_abc123"}, "name": "rjyuan2"}],
        )
        assert AIAssistant._is_mention_all(msg) is False

    def test_mentions_malformed_entries_no_crash(self):
        msg = _text_msg("你好", mentions=["bad", None, 123, {}])
        assert AIAssistant._is_mention_all(msg) is False

    def test_mentions_not_a_list_no_crash(self):
        msg = _text_msg("你好")
        msg["mentions"] = "unexpected"
        assert AIAssistant._is_mention_all(msg) is False


class TestMentionAllOtherTypes:
    """image/file 类型：原来完全没做判定"""

    def test_image_message_with_mentions_at_all(self):
        msg = {
            "message_type": "image",
            "content": json.dumps({"image_key": "img_v3_xxx"}),
            "mentions": [{"key": "@_all", "id": {"open_id": "all"}}],
        }
        assert AIAssistant._is_mention_all(msg) is True

    def test_file_message_without_mention_all(self):
        msg = {
            "message_type": "file",
            "content": json.dumps({"file_key": "file_v3_xxx", "file_name": "app.log"}),
        }
        assert AIAssistant._is_mention_all(msg) is False

    def test_empty_message_no_crash(self):
        assert AIAssistant._is_mention_all({}) is False
