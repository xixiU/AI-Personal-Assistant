"""
纯文本通道（OpenAI 兼容 API）的附件拍平防回归测试

背景（必须保留的 bug 复现条件）：
    openai_provider 把 Message.content 拍平成纯文本时，
    原实现只认 `content.type == "text"`，没有 else 分支也没有 warning。
    于是 log_file / image 内容块被**静默丢弃**。
    config.example.yaml 的默认 provider 是 "openai"，所以按默认配置部署时，
    用户上传的日志文件根本到不了模型，AI 只收到问题文本然后一本正经地瞎答。

复现步骤：
    1. provider 配 openai（默认），用户在飞书发「日志文件 + 一句提问」
    2. 观察实际发给 API 的 payload：只有提问文本，没有任何文件内容
    3. 日志里没有任何提示

本文件的断言就是守这三件事：
    - log_file 正文/路径/说明必须出现在发出去的 payload 里
    - image 必须有占位说明 + warning（本通道不做视觉输入）
    - 未知类型必须 warning（防下次新增 Content 类型再漏一个）
"""
from datetime import datetime
from unittest.mock import Mock, patch

import pytest
from loguru import logger

from ai_assistant.core.models import Content, Message


# 新符号延迟导入：模块级 import 一个还不存在的名字会变成 collection error，
# 而 collection error 不是断言失败，起不到红灯验证的作用（本项目踩过这个坑）。
def _flatten_content():
    from ai_assistant.core.ai_provider import flatten_content_to_text
    return flatten_content_to_text


def _flatten_message():
    from ai_assistant.core.ai_provider import flatten_message_text
    return flatten_message_text


def _openai_provider():
    from ai_assistant.providers.openai_provider import OpenAIProvider
    return OpenAIProvider(base_url="http://fake", api_key="k", model="gpt-4", timeout=5)


def _msg(*contents, role="user"):
    return Message(role=role, content=list(contents), timestamp=datetime.now())


def _log_file_content(name="app.log", path="/tmp/app.log", text="ERROR boom", note=""):
    return Content(type="log_file", data={
        "name": name, "path": path, "text": text, "note": note,
    })


def _image_content():
    return Content(type="image", data={"media_type": "image/png", "data": "QUJD"})


@pytest.fixture
def captured_logs():
    """收集 loguru 输出，用于断言 warning 真的记了（而不是静默丢弃）"""
    records = []
    sink_id = logger.add(lambda m: records.append(m), level="DEBUG")
    yield records
    logger.remove(sink_id)


def _text_of(records):
    return "\n".join(str(r) for r in records)


# ============ 拍平函数本身 ============

def test_flatten_log_file_carries_name_path_text():
    """log_file 必须带上文件名/路径/正文——这是整个修复的核心"""
    out = _flatten_content()(_log_file_content())
    assert "app.log" in out
    assert "/tmp/app.log" in out
    assert "ERROR boom" in out


def test_flatten_log_file_matches_anthropic_format():
    """与 anthropic_provider._content_to_api_parts 的文本组织保持一致"""
    out = _flatten_content()(_log_file_content())
    assert out.startswith("[用户上传的日志文件] 文件名: app.log")
    assert "本地路径: /tmp/app.log" in out
    assert "文件内容:\nERROR boom" in out


def test_flatten_log_file_without_text_hints_path_readable():
    """大文件只给路径时，要提示 AI 可以自己去读，而不是当成空文件"""
    out = _flatten_content()(_log_file_content(text=""))
    assert "/tmp/app.log" in out
    assert "内容未内联" in out


def test_flatten_log_file_note_preserved():
    """压缩包/保存失败的 note 必须传达给 AI"""
    out = _flatten_content()(
        _log_file_content(text="", note="文件超过大小上限或保存失败，未能读取内容")
    )
    assert "未能读取内容" in out


def test_flatten_image_emits_placeholder_and_warning(captured_logs):
    """图片不支持视觉输入时，必须留占位说明 + 记 warning，不能一声不响"""
    out = _flatten_content()(_image_content())
    assert out, "图片内容块必须留下占位说明，否则模型会把残缺信息当完整提问"
    assert "不支持图像输入" in out
    log_text = _text_of(captured_logs)
    assert "WARNING" in log_text
    assert "图片内容已丢弃" in log_text


def test_flatten_unknown_type_warns_with_type_name(captured_logs):
    """未知类型落 else 分支必须 warning 并带类型名（防下次新增类型再漏）"""
    out = _flatten_content()(Content(type="video", data="xxx"))
    assert out == ""
    log_text = _text_of(captured_logs)
    assert "WARNING" in log_text
    assert "video" in log_text


def test_flatten_never_logs_full_file_body(captured_logs):
    """项目硬规则：日志不得出现文件正文，只允许文件名 + 长度"""
    secret = "SUPER-SECRET-LOG-BODY-0xDEADBEEF"
    _flatten_content()(_log_file_content(text=secret))
    assert secret not in _text_of(captured_logs)


def test_flatten_message_joins_blocks_with_newline():
    """多内容块之间要有边界，不能像原实现那样无分隔符直接拼接"""
    out = _flatten_message()(_msg(_log_file_content(), Content(type="text", data="为啥报错")))
    assert "为啥报错" in out
    assert "app.log" in out
    assert "\n为啥报错" in out


# ============ OpenAI 通道端到端 ============

@patch("ai_assistant.providers.openai_provider.requests.post")
def test_openai_payload_contains_log_file(mock_post):
    """回归点：OpenAI 兼容通道发出的 payload 必须含日志文件内容"""
    mock_post.return_value = Mock(
        status_code=200,
        json=lambda: {"choices": [{"message": {"content": "ok"}}], "usage": {}},
        raise_for_status=lambda: None,
    )
    p = _openai_provider()
    p._send_with_context(
        [_msg(_log_file_content(), Content(type="text", data="帮我看下这个日志"))],
        doc_context="",
    )
    sent = mock_post.call_args.kwargs["json"]["messages"][-1]["content"]
    assert "app.log" in sent
    assert "ERROR boom" in sent
    assert "帮我看下这个日志" in sent


@patch("ai_assistant.providers.openai_provider.requests.post")
def test_openai_payload_contains_image_placeholder(mock_post):
    """图片虽然不发给模型，但 payload 里要有「我看不到图」的说明"""
    mock_post.return_value = Mock(
        status_code=200,
        json=lambda: {"choices": [{"message": {"content": "ok"}}], "usage": {}},
        raise_for_status=lambda: None,
    )
    p = _openai_provider()
    p._send_with_context([_msg(_image_content(), Content(type="text", data="这截图啥意思"))], "")
    sent = mock_post.call_args.kwargs["json"]["messages"][-1]["content"]
    assert "不支持图像输入" in sent
    assert "这截图啥意思" in sent


@patch("ai_assistant.providers.openai_provider.requests.post")
def test_openai_never_logs_payload_body(mock_post, captured_logs):
    """
    OpenAI 通道的日志不得出现日志文件正文。

    这条守的是本次修复**新引入**的风险：拍平之后 payload 的 messages 里
    开始携带日志文件正文，此时任何 `logger.debug(f"...{payload}")` 形状的
    日志行都等于把用户上传的整份日志写进我们自己的日志（CLAUDE.md 禁止）。
    当前 openai_provider 里没有这种行（只打长度/状态码/异常），这条断言
    保证以后也不会被加回来。
    """
    mock_post.return_value = Mock(
        status_code=200,
        json=lambda: {"choices": [{"message": {"content": "ok"}}], "usage": {}},
        raise_for_status=lambda: None,
    )
    secret = "SUPER-SECRET-LOG-BODY-0xDEADBEEF"
    p = _openai_provider()
    p._send_with_context(
        [_msg(_log_file_content(text=secret), Content(type="text", data="分析"))], ""
    )
    assert secret not in _text_of(captured_logs)


@patch("ai_assistant.providers.openai_provider.requests.post")
def test_openai_still_handles_plain_text(mock_post):
    """纯文本消息行为不变（修复不应改变既有路径）"""
    mock_post.return_value = Mock(
        status_code=200,
        json=lambda: {"choices": [{"message": {"content": "ok"}}], "usage": {}},
        raise_for_status=lambda: None,
    )
    p = _openai_provider()
    p._send_with_context([_msg(Content(type="text", data="你好"))], "")
    assert mock_post.call_args.kwargs["json"]["messages"][-1]["content"] == "你好"
