"""
附件超过大小上限时，回复里必须确定性地出现提示（防回归）

## 背景（真实现场）

用户在飞书群发了一个日志包，引用它提问后 AI 什么都没读到。超限时 save_bytes 返回
None，调用方只给 AI 塞一条「超过大小上限或保存失败」的 note，用户前台完全不感知，
只有服务端一行 WARNING。AI 说不说、怎么说全凭它自己，常常照样"分析"一通。

## 修复

- feishu_bot._collect_log_files 区分超限与保存失败，超限块带 oversize / size_bytes /
  limit_bytes；
- _process_event 在 AI 回复后用 build_oversize_notice 扫描本轮 content_parts，
  由程序把提示追加到回复末尾（含文件名、实际大小、上限、精简建议）。

## 测试策略

驱动真实的 `_collect_log_files` 与 `_process_event`，从回复发送入口截获最终文本，
不只测 build_oversize_notice 本身——只测工具函数会出现"工具对了但调用点没接上"的假绿灯。
"""
import threading

import pytest


_AI_ANSWER = "AI 的正常回答"


def _make_adapter(tmp_path, data):
    """
    真实 FeishuBotAdapter + 真实 LogFileManager（上限 1MB），只桩掉网络下载。

    data 为 bytes 时所有 file_key 都返回它；为 dict 时按 file_key 取。
    """
    from ai_assistant.adapters.feishu_bot import FeishuBotAdapter
    from ai_assistant.utils.log_files import LogFileManager

    manager = LogFileManager(enabled=True, base_dir=str(tmp_path), max_size_mb=1)
    adapter = FeishuBotAdapter({"log_file_manager": manager})
    adapter.download_file = (
        lambda message_id, file_key: data[file_key] if isinstance(data, dict) else data
    )
    return adapter


def _oversize_contents(tmp_path, size_mb: float = 2.5, name: str = "debug.log"):
    adapter = _make_adapter(tmp_path, b"x" * int(size_mb * 1024 * 1024))
    return adapter._collect_log_files("om_file", [{"file_key": "fk", "file_name": name}], "oc_1")


# ============ 适配器：超限与保存失败要分开 ============

def test_collect_log_files_marks_oversize_with_size_and_limit(tmp_path):
    contents = _oversize_contents(tmp_path)

    assert len(contents) == 1
    data = contents[0].data
    assert data["oversize"] is True
    assert data["size_bytes"] == int(2.5 * 1024 * 1024)
    assert data["limit_bytes"] == 1024 * 1024
    assert data["path"] == ""
    # 给 AI 的说明要明确是"超限未读取"，不再与"保存失败"混在一句里
    assert "超过大小上限" in data["note"]
    assert "保存失败" not in data["note"]


# ============ 主流程：回复末尾必须追加提示 ============

class _StubContextManager:
    def __init__(self):
        self.messages = {}

    def add_message(self, key, message):
        self.messages.setdefault(key, []).append(message)

    def get_context(self, key):
        return list(self.messages.get(key, []))

    def clear_context(self, key):
        self.messages.pop(key, None)


class _StubAIProvider:
    def __init__(self):
        self.calls = []

    def call(self, messages, *args, **kwargs):
        self.calls.append(messages)
        return _AI_ANSWER, None, {}


class _StubQueue:
    def qsize(self):
        return 0


class _StubEventAdapter:
    """不提供 add_reaction / get_message：_process_event 用 hasattr 判断，缺失即跳过"""

    def process_webhook_event(self, raw_data):
        return {"event": {}}


def _make_app(parsed: dict, sent: list):
    """
    object.__new__ 绕过 __init__（真 __init__ 会读 config.yaml、建索引），
    被测的 _process_event 本身是真的。_send_feishu_reply 只截获回复文本。
    """
    from ai_assistant.main import AIAssistant

    app = object.__new__(AIAssistant)
    app.event_queue = _StubQueue()
    app.context_manager = _StubContextManager()
    app.ai_provider = _StubAIProvider()
    app.chat_history = None
    app._processing_lock = threading.Lock()
    app._processing_sessions = set()
    app._deferred_messages = {}
    app._pending_lone_images = {}
    app._lone_image_wait = 0
    app.pending_retry_queue = []
    app._parse_feishu_event = lambda processed_event, adapter: dict(parsed)
    app._send_feishu_reply = (
        lambda adapter, message_id, session_id, reply, *a, **kw: sent.append(reply)
    )
    return app


def _run(app):
    app._process_event({
        "trace_id": "test-trace",
        "adapter": _StubEventAdapter(),
        "raw_data": {"encrypt": "x"},
    })


def _parsed(log_contents):
    return {
        "chat_id": "oc_1",
        "text": "帮我看下这个日志",
        "message_id": "om_q",
        "sender_id": "ou_1",
        "root_id": "",
        "parent_id": "",
        "image_data": None,
        "log_contents": log_contents,
    }


def _assert_notice(reply: str):
    assert reply.startswith(_AI_ANSWER), "AI 照常回答，提示只能附加在后面"
    assert "debug.log" in reply
    assert "2.5MB" in reply
    assert "上限 1MB" in reply
    assert "精简" in reply


def test_process_event_appends_oversize_notice_to_reply(tmp_path):
    sent = []
    _run(_make_app(_parsed(_oversize_contents(tmp_path)), sent))

    assert len(sent) == 1
    _assert_notice(sent[0])


def test_process_event_no_notice_when_attachment_within_limit(tmp_path):
    adapter = _make_adapter(tmp_path, b"ERROR boom")
    contents = adapter._collect_log_files(
        "om_file", [{"file_key": "fk", "file_name": "small.log"}], "oc_1"
    )
    sent = []
    _run(_make_app(_parsed(contents), sent))

    assert sent == [_AI_ANSWER]


def test_two_attachments_only_oversize_one_is_reported(tmp_path):
    """一条消息两个附件、只有一个超限：提示只列超限的那个，正常那个照常交给 AI"""
    adapter = _make_adapter(tmp_path, {
        "fk_big": b"x" * int(2.5 * 1024 * 1024),
        "fk_small": b"ERROR small boom",
    })
    contents = adapter._collect_log_files("om_file", [
        {"file_key": "fk_big", "file_name": "debug.log"},
        {"file_key": "fk_small", "file_name": "small.log"},
    ], "oc_1")
    sent = []
    app = _make_app(_parsed(contents), sent)
    _run(app)

    assert len(sent) == 1
    _assert_notice(sent[0])
    notice = sent[0][len(_AI_ANSWER):]
    assert "small.log" not in notice, "正常附件不该出现在超限提示里"
    assert notice.count("debug.log") == 1

    # 正常附件的正文照常到达 AI
    small = [
        c for c in app.ai_provider.calls[0][-1].content
        if c.type == "log_file" and c.data["name"] == "small.log"
    ]
    assert len(small) == 1 and small[0].data["text"] == "ERROR small boom"


# ============ 引用回查：缓存命中时提示照样出现 ============

def test_quoted_single_oversize_attachment_notice_not_duplicated(tmp_path):
    """
    引用一条只带一个超限附件的文件消息提问（群聊主路径），连问两轮：
    - 两轮回复都带超限提示，且各只出现一次（第二轮命中引用缓存）；
    - 不会再叠加 _fetch_quoted_attachments 的"全军覆没 / 部分未能读取"说明，
      AI 收到的提问文字保持原样。
    """
    import json

    adapter = _make_adapter(tmp_path, b"x" * int(2.5 * 1024 * 1024))
    adapter.bot_config["thinking_reaction"] = False
    downloads = []
    real_download = adapter.download_file
    adapter.download_file = lambda mid, fk: downloads.append(mid) or real_download(mid, fk)
    adapter.process_webhook_event = lambda raw: {"event": {}}
    adapter.get_message = lambda mid: {
        "message_id": mid,
        "msg_type": "file",
        "body": {"content": json.dumps({"file_key": "fk_big", "file_name": "debug.log"})},
    }

    sent = []
    question = "帮我看下这个日志"
    app = _make_app({
        "chat_id": "oc_1", "text": question, "message_id": "om_q",
        "sender_id": "ou_1", "root_id": "om_file", "parent_id": "om_file",
        "image_data": None,
    }, sent)
    app._quoted_attachment_cache = {}
    app._quoted_cache_lock = threading.Lock()
    app._quoted_cache_max_keys = 200
    app.log_file_manager = adapter.log_file_manager

    for _ in range(2):
        app._process_event({"trace_id": "t", "adapter": adapter, "raw_data": {}})

    assert downloads == ["om_file"], "第二轮应命中引用缓存，不重新下载"
    assert len(sent) == 2
    for reply in sent:
        _assert_notice(reply)
        assert reply.count("超过大小上限") == 1, "超限提示重复出现"
    for messages in app.ai_provider.calls:
        texts = [c.data for c in messages[-1].content if c.type == "text"]
        assert texts == [question], "引用回查不应再往提问里叠加附件失败说明"

def test_quoted_oversize_notice_survives_cache_hit(tmp_path):
    from ai_assistant.main import AIAssistant
    from ai_assistant.utils.log_files import build_oversize_notice

    real_adapter = _make_adapter(tmp_path, b"x" * int(2.5 * 1024 * 1024))
    calls = []

    class _QuoteAdapter:
        def _collect_log_files(self, message_id, files, chat_id):
            calls.append(message_id)
            return real_adapter._collect_log_files(message_id, files, chat_id)

    class _App:
        _quoted_attachment_cache = {}
        _quoted_cache_lock = threading.Lock()
        _quoted_cache_max_keys = 200
        log_file_manager = real_adapter.log_file_manager
        _collect_quoted_log_files = AIAssistant._collect_quoted_log_files

    app = _App()
    files = [{"file_key": "fk_big", "file_name": "debug.log"}]

    first, _ = app._collect_quoted_log_files(_QuoteAdapter(), "om_file", files, "oc_1")
    second, _ = app._collect_quoted_log_files(_QuoteAdapter(), "om_file", files, "oc_1")

    assert calls == ["om_file"], "第二次应命中缓存，不重新下载"
    for contents in (first, second):
        _assert_notice(f"{_AI_ANSWER}\n\n{build_oversize_notice(contents)}")


# ============ Web 前端：上限常量与后端保持一致 ============

def test_web_frontend_limits_match_backend():
    """
    前端的单文件上限必须等于后端默认值，否则 50MB 到不了后端；
    所选文件总量上限经 base64 膨胀、加上图片后必须小于后端请求体上限，否则多个大文件
    会撞上 413，用户只能看到一句看不懂的报错。
    """
    import os
    import re

    from ai_assistant.utils.log_files import LogFileManager
    from ai_assistant.webhook_server import _MAX_CONTENT_LENGTH

    html_path = os.path.join(
        os.path.dirname(__file__), "..", "..", "src", "ai_assistant", "static", "index.html"
    )
    with open(html_path, encoding="utf-8") as f:
        html = f.read()

    size = re.search(r"const MAX_FILE_SIZE = (\d+) \* 1024 \* 1024;", html)
    assert size and int(size.group(1)) * 1024 * 1024 == LogFileManager().max_size_bytes

    total = re.search(r"const MAX_TOTAL_FILE_SIZE = (\d+) \* 1024 \* 1024;", html)
    image = re.search(r"file\.size > (\d+) \* 1024 \* 1024\) \{ showError\('图片不能超过", html)
    assert total and image, "前端缺少总量上限常量或图片上限"
    total_b64 = int(total.group(1)) * 1024 * 1024 * 4 / 3
    image_b64 = int(image.group(1)) * 1024 * 1024 * 4 / 3
    assert total_b64 + image_b64 < _MAX_CONTENT_LENGTH
    assert "totalTooLarge()" in html, "前端未在添加文件时做总量预检"
