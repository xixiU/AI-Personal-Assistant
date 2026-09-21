"""
消息编辑事件（im.message.updated_v1）处理测试

背景：用户发出提问后发现写错了（漏版本号、写错机器名、少关键词），
直接编辑原消息比重发一条更自然，但早期 process_webhook_event 只认
im.message.receive_v1，编辑事件走到末尾 return None，日志里只留
「Adapter returned None, no AI reply needed」，机器人完全不响应。

复现步骤：
1. 在飞书开放平台订阅 im.message.updated_v1 事件
2. 群里 @机器人 提问，等机器人回答
3. 编辑刚才那条提问（改成正确内容）
4. 日志出现 Processing event_type: im.message.updated_v1 后
   紧跟 Adapter returned None，没有任何 AI 调用

核心规则（回归重点）：
1. 必须过滤机器人自身消息的更新事件，否则「改卡片→收到编辑事件→再回复→再改卡片」自循环
2. 必须按 (message_id, 内容指纹) 去重，连续编辑/重复投递不能重复回答；
   但内容真的又变了要放行（改两次都该答）
3. 归一化后必须保留 mentions / parent_id / root_id，否则 @所有人 判定和引用链失效
4. 编辑重答不能把该消息旧的一轮问答也注入上下文，否则 AI 同时看到改错前后两版
5. 开关关闭时不处理
"""
import json
import sys
import threading
import types
from unittest.mock import Mock

# 运行时依赖可能未安装（本项目按规范只做静态测试），注入最小桩模块。
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

from datetime import datetime

from ai_assistant.adapters.feishu_bot import FeishuBotAdapter
from ai_assistant.core.context_manager import ContextManager
from ai_assistant.core.models import Content, Message


def _msg(role: str, text: str) -> Message:
    return Message(
        role=role,
        content=[Content(type="text", data=text)],
        timestamp=datetime.now(),
    )


def _adapter(**overrides) -> FeishuBotAdapter:
    config = {
        "app_id": "cli_test",
        "app_secret": "secret",
        "base_url": "https://open.feishu.cn",
    }
    config.update(overrides)
    return FeishuBotAdapter(config)


def _updated_event(
    message_id: str = "om_user_msg_1",
    chat_id: str = "oc_chat_1",
    text: str = "184 上 ts-service 什么分支",
    message_type: str = "text",
    content: dict = None,
    mentions=None,
    parent_id: str = "",
    root_id: str = "",
    with_sender: bool = True,
) -> dict:
    message = {
        "message_id": message_id,
        "chat_id": chat_id,
        "chat_type": "group",
        "message_type": message_type,
        "content": json.dumps(content if content is not None else {"text": text},
                              ensure_ascii=False),
    }
    if mentions is not None:
        message["mentions"] = mentions
    if parent_id:
        message["parent_id"] = parent_id
    if root_id:
        message["root_id"] = root_id

    event = {"message": message}
    if with_sender:
        event["sender"] = {"sender_id": {"open_id": "ou_user_1"}}

    return {
        "schema": "2.0",
        "header": {"event_id": "evt_1", "event_type": "im.message.updated_v1"},
        "event": event,
    }


class TestUpdatedEventDispatch:
    """事件分发：原来直接 return None，现在要被识别"""

    def test_updated_event_is_processed(self):
        result = _adapter().process_webhook_event(_updated_event())
        assert result is not None
        assert result["event"]["_is_message_updated"] is True

    def test_normalized_shape_matches_receive_v1(self):
        """归一化后必须与 receive_v1 同构，下游解析零改动复用"""
        result = _adapter().process_webhook_event(_updated_event())
        message = result["event"]["message"]
        assert message["message_id"] == "om_user_msg_1"
        assert message["chat_id"] == "oc_chat_1"
        assert message["message_type"] == "text"
        assert json.loads(message["content"])["text"] == "184 上 ts-service 什么分支"
        assert result["event"]["sender"]["sender_id"]["open_id"] == "ou_user_1"

    def test_disabled_by_config(self):
        adapter = _adapter(handle_message_updated=False)
        assert adapter.process_webhook_event(_updated_event()) is None

    def test_missing_message_id_skipped(self):
        assert _adapter().process_webhook_event(_updated_event(message_id="")) is None

    def test_missing_chat_id_skipped(self):
        assert _adapter().process_webhook_event(_updated_event(chat_id="")) is None

    def test_empty_content_skipped(self):
        """撤回等无 content 的更新不处理"""
        event = _updated_event()
        event["event"]["message"]["content"] = ""
        assert _adapter().process_webhook_event(event) is None

    def test_sender_missing_gets_placeholder(self):
        """编辑事件可能不带 sender，补空 open_id 而不是崩溃"""
        result = _adapter().process_webhook_event(_updated_event(with_sender=False))
        assert result is not None
        assert result["event"]["sender"]["sender_id"]["open_id"] == ""


class TestSelfLoopProtection:
    """自循环防护（最关键的一道闸门）"""

    def test_own_message_update_skipped(self):
        """机器人回复卡片被重绘时不能再触发 AI，否则无限自循环"""
        adapter = _adapter()
        adapter.cache_message_record("om_bot_reply_1", "record-uuid-1")
        event = _updated_event(message_id="om_bot_reply_1")
        assert adapter.process_webhook_event(event) is None

    def test_user_message_not_affected_by_cache(self):
        adapter = _adapter()
        adapter.cache_message_record("om_bot_reply_1", "record-uuid-1")
        assert adapter.process_webhook_event(_updated_event(message_id="om_user_msg_1")) is not None


class TestFingerprintDedup:
    """内容指纹去重"""

    def test_same_content_twice_dedup(self):
        """飞书重复投递同一事件，只处理一次"""
        adapter = _adapter()
        event = _updated_event(text="查 184 的分支")
        assert adapter.process_webhook_event(event) is not None
        assert adapter.process_webhook_event(_updated_event(text="查 184 的分支")) is None

    def test_content_changed_again_is_processed(self):
        """改两次、两次内容都不同 → 都要答（只按 message_id 去重会漏这个）"""
        adapter = _adapter()
        assert adapter.process_webhook_event(_updated_event(text="查 184")) is not None
        assert adapter.process_webhook_event(_updated_event(text="查 184 的分支")) is not None

    def test_different_messages_independent(self):
        adapter = _adapter()
        assert adapter.process_webhook_event(
            _updated_event(message_id="om_a", text="同样的话")) is not None
        assert adapter.process_webhook_event(
            _updated_event(message_id="om_b", text="同样的话")) is not None

    def test_fingerprint_keys_bounded(self):
        """长跑不能无界增长"""
        adapter = _adapter()
        adapter._updated_fp_max_keys = 10
        for i in range(30):
            adapter.process_webhook_event(_updated_event(message_id=f"om_{i}", text=f"q{i}"))
        assert len(adapter._updated_msg_fingerprints) <= 11

    def test_dedup_thread_safe(self):
        """并发投递同一事件时只有一个通过"""
        adapter = _adapter()
        results = []

        def _worker():
            results.append(adapter.process_webhook_event(_updated_event(text="并发同一条")))

        threads = [threading.Thread(target=_worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert sum(1 for r in results if r is not None) == 1


class TestFieldsPreserved:
    """归一化必须保留下游依赖的字段"""

    def test_mentions_preserved_for_mention_all_check(self):
        """丢了 mentions 会让编辑后的 @所有人 消息绕过忽略逻辑"""
        result = _adapter().process_webhook_event(_updated_event(
            mentions=[{"key": "@_all", "id": {"open_id": "all"}}]
        ))
        assert result["event"]["message"]["mentions"][0]["key"] == "@_all"

    def test_chain_ids_preserved(self):
        """丢了 root_id 会让编辑后的引用追问退化成全新对话"""
        result = _adapter().process_webhook_event(_updated_event(
            parent_id="om_parent", root_id="om_root"
        ))
        message = result["event"]["message"]
        assert message["parent_id"] == "om_parent"
        assert message["root_id"] == "om_root"

    def test_post_type_preserved(self):
        """富文本编辑：content 结构原样透传给 _parse_post_content"""
        content = {"zh_cn": {"content": [[{"tag": "text", "text": "改过的富文本"}]]}}
        result = _adapter().process_webhook_event(
            _updated_event(message_type="post", content=content)
        )
        assert result["event"]["message"]["message_type"] == "post"
        assert json.loads(result["event"]["message"]["content"]) == content


class TestClearContextForRepeatAnswer:
    """编辑重答的上下文清理（防止新旧两版同时进 AI）"""

    def test_clear_context_removes_messages(self):
        mgr = ContextManager(max_messages=10)
        mgr.add_message("key1", _msg("user", "改错前的问题"))
        mgr.add_message("key1", _msg("assistant", "基于错问题的答案"))
        assert len(mgr.get_context("key1")) == 2

        mgr.clear_context("key1")
        assert mgr.get_context("key1") == []

    def test_clear_context_keeps_session_usable(self):
        """清空后仍能继续写入（Session 对象保留，避免并发下丢写）"""
        mgr = ContextManager(max_messages=10)
        mgr.add_message("key1", _msg("user", "旧"))
        mgr.clear_context("key1")
        mgr.add_message("key1", _msg("user", "新"))

        ctx = mgr.get_context("key1")
        assert len(ctx) == 1
        assert ctx[0].content[0].data == "新"

    def test_clear_context_unknown_key_no_crash(self):
        ContextManager().clear_context("never-seen")

    def test_clear_context_does_not_affect_other_keys(self):
        mgr = ContextManager(max_messages=10)
        mgr.add_message("a", _msg("user", "A"))
        mgr.add_message("b", _msg("user", "B"))
        mgr.clear_context("a")

        assert mgr.get_context("a") == []
        assert len(mgr.get_context("b")) == 1
