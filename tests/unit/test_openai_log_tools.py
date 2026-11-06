"""
OpenAI 兼容通道的日志工具调用（providers/openai_provider.py）

背景 bug：
  ai_provider.flatten_content_to_text 在日志未内联时告诉 AI"（内容未内联，可用代码
  排查工具读取上述路径）"，而 OpenAI 通道只发普通 chat completions、没有任何工具，
  这是一句假话：AI 读不到日志正文，只能凭空作答。config.example.yaml 的默认
  provider 就是 openai。

复现步骤（修复前）：
  1. provider 配 openai，用户上传一个大日志（只内联开头或只给路径）并问"民初23986号回传正常吗"
  2. 请求体里没有 tools，prompt 却说"可用代码排查工具读取"
  3. AI 1 轮结束、看不到命中行，回答与日志内容无关

修复后：当前轮带可读日志时请求带 tools（search_log / read_log_lines / list_log_files），
工具结果以 role="tool" 回传；无日志时请求与改前逐字节一致；服务端不支持 tools 时
去掉 tools 重试一次并打 WARNING。

防二次出现要点：
  - 无日志附件时请求不得多带任何字段（Ollama 等对未知字段的处理不可控）
  - LogTools 不得挂到 self 上（provider 单例被并发调用，会串群读日志）
  - 服务端日志不得出现关键词原文、命中正文，也不得出现它们的指纹
  - 顶层不得 import log_tools（它经 tools/__init__ 拉起 paramiko）
"""
import copy
import importlib.util
import json
import sys
import types
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
import requests
from loguru import logger

from ai_assistant.core.ai_provider import flatten_content_to_text
from ai_assistant.core.models import Content, Message
from ai_assistant.providers import openai_provider
from ai_assistant.providers.openai_provider import OpenAIProvider
from ai_assistant.utils.log_summary import fingerprint


def to_openai_tools(schema):
    """新符号延迟取用：模块级 import 会让旧版 provider 上的对照跑成 collection error"""
    return openai_provider.to_openai_tools(schema)

KEYWORD = "ZZKW91357"
BODY_MARK = "ZZBODY24680"
OUTSIDE_MARK = "ZZOUTSIDE55"


# ============ 桩与夹具 ============

@pytest.fixture(autouse=True)
def _paramiko_stub(monkeypatch):
    """
    import log_tools 会先执行 tools/__init__.py → operations_commands → paramiko。
    运行环境没装 paramiko 时只挂这一个桩，且用 monkeypatch 在用例结束后撤掉。
    """
    try:
        import paramiko  # noqa: F401
    except ImportError:
        stub = types.ModuleType("paramiko")
        stub.__getattr__ = lambda _attr: Mock()  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "paramiko", stub)


@pytest.fixture
def lt():
    """log_tools 模块（在 paramiko 桩之后导入）"""
    import ai_assistant.tools.log_tools as mod
    return mod


@pytest.fixture
def captured():
    lines = []
    sink_id = logger.add(lambda m: lines.append(str(m)), level="DEBUG", format="{level} {message}")
    yield lines
    logger.remove(sink_id)


@pytest.fixture
def log_file(tmp_path):
    """会话子目录下的日志附件（模拟 data/log_files/<会话>/xxx.log）"""
    d = tmp_path / "oc_chat_a"
    d.mkdir()
    f = d / "debug.log"
    lines = [f"2026-11-05 10:20:{i:02d} INFO heartbeat ok" for i in range(40)]
    lines[25] = f"2026-11-05 10:20:25 INFO 民初23986号 回传成功 {KEYWORD} {BODY_MARK}"
    f.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return f


@pytest.fixture
def outside_file(tmp_path):
    """白名单外的文件（另一个群的日志）"""
    d = tmp_path / "oc_chat_b"
    d.mkdir()
    f = d / "other.log"
    f.write_text(f"别的群的日志 {OUTSIDE_MARK}\n", encoding="utf-8")
    return f


def _provider():
    return OpenAIProvider(base_url="http://fake", api_key="k", model="gpt-4", timeout=5)


def _msg(*contents, role="user"):
    return Message(role=role, content=list(contents), timestamp=datetime.now())


def _log_content(path, text="", note=""):
    return Content(type="log_file", data={"name": "debug.log", "path": str(path), "text": text, "note": note})


def _log_msg(path, question="民初23986号回传正常吗", text=""):
    return _msg(_log_content(path, text=text), Content(type="text", data=question))


class _Server:
    """
    按顺序返回预置响应，并深拷贝每次请求的 payload：工具循环会在同一个 messages
    列表上追加，只存引用的话第一轮的请求体会被后续轮次改写，断言失真。
    """

    def __init__(self, *responses):
        self.responses = list(responses)
        self.payloads = []
        self.headers = []
        self.urls = []
        self.timeouts = []

    def __call__(self, url, json=None, headers=None, timeout=None):
        self.urls.append(url)
        self.timeouts.append(timeout)
        self.payloads.append(copy.deepcopy(json))
        self.headers.append(copy.deepcopy(headers))
        return self.responses.pop(0)


def _ok(message, usage=None):
    r = Mock(status_code=200)
    r.json.return_value = {"choices": [{"message": message}], "usage": usage or {}}
    r.raise_for_status.return_value = None
    return r


def _text(content):
    return _ok({"role": "assistant", "content": content})


def _calls(*calls, content=None):
    return _ok({
        "role": "assistant",
        "content": content,
        "tool_calls": [
            {"id": cid, "type": "function", "function": {"name": name, "arguments": args}}
            for cid, name, args in calls
        ],
    })


def _http_err(status, text):
    resp = Mock(status_code=status, text=text)
    r = Mock(status_code=status, text=text)
    r.raise_for_status.side_effect = requests.exceptions.HTTPError(f"{status} Error", response=resp)
    return r


def _run(provider, messages, *responses, doc_context=""):
    server = _Server(*responses)
    with patch("ai_assistant.providers.openai_provider.requests.post", side_effect=server):
        result = provider._send_with_context(messages, doc_context)
    return result, server


def _user_content(payload):
    return [m for m in payload["messages"] if m["role"] == "user"][-1]["content"]


# ============ 带日志：工具调用主流程 ============

def test_openai_log_tools_search_then_final_reply(log_file, lt):
    """带日志时请求带 tools；search_log 结果以 role=tool 回传，第二轮给出最终回复"""
    p = _provider()
    (reply, meta), server = _run(
        p, [_log_msg(log_file)],
        _calls(("c1", "search_log", json.dumps({"keywords": ["23986"]}))),
        _text("结论：23986 回传成功"),
    )

    assert len(server.payloads) == 2
    first = server.payloads[0]
    assert first["tools"] == to_openai_tools(lt.LOG_TOOLS_SCHEMA)
    assert {t["function"]["name"] for t in first["tools"]} == set(lt.LOG_TOOL_NAMES)
    assert lt.LOG_TOOLS_ATTACHMENT_HINT in _user_content(first)

    second = server.payloads[1]["messages"]
    assistant, tool_msg = second[-2], second[-1]
    assert assistant["role"] == "assistant"
    assert assistant["tool_calls"][0]["id"] == "c1"
    assert assistant["content"] is None
    assert tool_msg["role"] == "tool"
    assert tool_msg["tool_call_id"] == "c1"
    assert "回传成功" in tool_msg["content"]

    assert reply == "结论：23986 回传成功"
    assert meta == {"mode": "agentic", "tool_rounds": 2}


def test_openai_log_tools_plain_text_reply_ends_loop(log_file):
    """模型不调工具、直接回文本：当作最终回复，只请求一次"""
    (reply, meta), server = _run(_provider(), [_log_msg(log_file)], _text("直接回答"))
    assert len(server.payloads) == 1
    assert reply == "直接回答"
    assert meta["tool_rounds"] == 1


def test_to_openai_tools_converts_from_single_schema(lt):
    """tools 由 LOG_TOOLS_SCHEMA 转换得到，parameters 与 input_schema 相等且不共享引用"""
    tools = to_openai_tools(lt.LOG_TOOLS_SCHEMA)
    assert [t["function"]["name"] for t in tools] == [s["name"] for s in lt.LOG_TOOLS_SCHEMA]
    for t, s in zip(tools, lt.LOG_TOOLS_SCHEMA):
        assert t["type"] == "function"
        assert t["function"]["parameters"] == s["input_schema"]
        assert t["function"]["parameters"] is not s["input_schema"]


# ============ 无日志：请求与改前一致 ============

def test_openai_no_log_payload_byte_identical():
    """
    无日志附件时 url / headers / payload / timeout 与接入日志工具前（HEAD 3b2d708 的
    openai_provider.py）的字面量完全一致：不多带任何字段，抽出的 _headers() 不得改变请求头
    """
    p = _provider()
    msgs = [_msg(Content(type="text", data="为啥报错"))]
    _, server = _run(p, msgs, _text("ok"), doc_context="DOC")

    assert len(server.payloads) == 1
    assert server.urls[0] == "http://fake/v1/chat/completions"
    assert server.timeouts[0] == 5
    assert server.payloads[0] == {
        "model": "gpt-4",
        "messages": [
            {
                "role": "system",
                "content": (
                    "DOC\n\n"
                    "请基于以上文档内容回答用户的问题。如果文档中没有相关信息，请如实告知。\n"
                    "回答完成后，在末尾附加参考文档链接（除非用户明确要求不附加），格式如下：\n"
                    "---\n"
                    "📎 参考文档：\n"
                    "- [文档标题](原文链接)"
                ),
            },
            {"role": "user", "content": "为啥报错"},
        ],
    }
    assert server.headers[0] == {"Content-Type": "application/json", "Authorization": "Bearer k"}


def test_openai_no_log_headers_without_api_key():
    """未配 api_key（本地 Ollama）时只有 Content-Type，不带空的 Authorization"""
    p = OpenAIProvider(base_url="http://fake/", api_key="", model="llama2", timeout=30)
    _, server = _run(p, [_msg(Content(type="text", data="q"))], _text("ok"))
    assert server.urls[0] == "http://fake/v1/chat/completions"
    assert server.timeouts[0] == 30
    assert server.headers[0] == {"Content-Type": "application/json"}


def test_openai_no_log_does_not_import_log_tools(monkeypatch, captured):
    """无日志附件时不尝试导入 log_tools（没装 paramiko 的部署普通提问也不能受影响）"""
    monkeypatch.setitem(sys.modules, "ai_assistant.tools.log_tools", None)
    reply, server = _run(_provider(), [_msg(Content(type="text", data="你好"))], _text("ok"))
    assert reply == "ok"
    assert "日志工具模块导入失败" not in "\n".join(captured)


def test_openai_log_path_missing_sends_no_tools(tmp_path):
    """日志文件已被清理：不带 tools，提示"本轮无法读取全文"，不点名工具"""
    gone = tmp_path / "gone.log"
    _, server = _run(_provider(), [_log_msg(gone)], _text("ok"))
    payload = server.payloads[0]
    assert "tools" not in payload
    assert "本轮无法读取全文" in _user_content(payload)
    assert "search_log" not in _user_content(payload)


def test_openai_log_tools_import_failure_degrades(monkeypatch, log_file, captured):
    """log_tools 导入失败（如缺 paramiko）：打 WARNING，按无工具处理，回复照常"""
    monkeypatch.setitem(sys.modules, "ai_assistant.tools.log_tools", None)
    reply, server = _run(_provider(), [_log_msg(log_file)], _text("凭提问作答"))

    assert reply == "凭提问作答"
    assert len(server.payloads) == 1
    assert "tools" not in server.payloads[0]
    assert "本轮无法读取全文" in _user_content(server.payloads[0])
    text = "\n".join(captured)
    assert "WARNING" in text and "日志工具模块导入失败" in text


# ============ 兼容兜底 ============

@pytest.mark.parametrize("status, body", [
    (400, '{"error":{"message":"registry.ollama.ai/library/llama2 does not support tools"}}'),
    (500, '{"error":"unknown field: tools"}'),
    # 首轮 400 且文案不含 tool/function：仍判为不支持（首轮只差 tools 字段）
    (400, '{"error":{"message":"invalid request parameters"}}'),
], ids=["400-keyword", "500-keyword", "400-round1-no-keyword"])
def test_openai_tools_rejected_retries_without_tools(log_file, captured, lt, status, body):
    """服务端拒绝 tools：去掉 tools 重试一次，prompt 改说"本轮无法读取全文"，打 WARNING"""
    reply, server = _run(_provider(), [_log_msg(log_file)], _http_err(status, body), _text("凭开头作答"))

    assert len(server.payloads) == 2
    assert "tools" in server.payloads[0]
    retry = server.payloads[1]
    assert "tools" not in retry
    assert "本轮无法读取全文" in _user_content(retry)
    assert lt.LOG_TOOLS_ATTACHMENT_HINT not in _user_content(retry)
    assert reply == "凭开头作答"
    text = "\n".join(captured)
    assert "WARNING" in text and "不支持工具调用" in text


def test_openai_tools_auth_error_not_retried(log_file):
    """认证失败即使文案含 function 也不重试：去掉 tools 照样失败，只会多烧一次调用"""
    (reply, meta), server = _run(
        _provider(), [_log_msg(log_file)],
        _http_err(401, '{"error":"invalid api key for function calling"}'),
    )
    assert len(server.payloads) == 1
    assert meta["error"] == "auth"


# ============ 循环上限 ============

def test_openai_log_tools_max_rounds(log_file):
    """每轮都要调工具时，达到 max_rounds 即停，返回超时文案"""
    p = _provider()
    p.max_rounds = 2
    args = json.dumps({"keywords": ["23986"]})
    (reply, meta), server = _run(
        p, [_log_msg(log_file)],
        _calls(("c1", "search_log", args)), _calls(("c2", "search_log", args)), _calls(("c3", "search_log", args)),
    )
    assert len(server.payloads) == 2
    assert meta["timeout"] is True
    assert meta["tool_rounds"] == 2
    assert "最大分析轮数" in reply


def test_openai_log_tools_max_time(monkeypatch, log_file):
    """总耗时超过 max_time 即停；超时后未执行的 tool_call 也回一条 error（否则下一轮 400）"""
    clock = iter(range(0, 10000, 100))  # 每次取时间前进 100s
    monkeypatch.setattr(openai_provider, "time", SimpleNamespace(monotonic=lambda: next(clock)))
    p = _provider()
    p.max_time = 150
    (reply, meta), server = _run(
        p, [_log_msg(log_file)],
        _calls(("c1", "search_log", json.dumps({"keywords": ["23986"]}))),
        _text("不应走到这里"),
    )
    assert len(server.payloads) == 1
    assert meta["timeout"] is True
    assert meta["tool_rounds"] == 1


# ============ 安全边界 ============

def test_openai_log_tools_reject_out_of_scope_path(log_file, outside_file):
    """读白名单外的文件（另一个群的日志）被拒，tool 消息里不含其内容"""
    _, server = _run(
        _provider(), [_log_msg(log_file)],
        _calls(("c1", "read_log_lines", json.dumps({"path": str(outside_file), "start_line": 1}))),
        _text("结论"),
    )
    tool_msg = server.payloads[1]["messages"][-1]
    assert tool_msg["role"] == "tool"
    assert "不在本轮日志附件范围" in tool_msg["content"]
    assert OUTSIDE_MARK not in tool_msg["content"]


def test_openai_log_tools_bad_arguments_reported_to_model(log_file):
    """arguments 不是合法 JSON：回一条 error 给模型，循环继续"""
    (reply, _), server = _run(
        _provider(), [_log_msg(log_file)],
        _calls(("c1", "search_log", "{not json")),
        _text("结论"),
    )
    tool_msg = server.payloads[1]["messages"][-1]
    assert tool_msg["tool_call_id"] == "c1"
    assert "不是合法的 JSON" in tool_msg["content"]
    assert reply == "结论"


def test_openai_log_tools_server_log_has_no_keyword_or_body(log_file, captured):
    """服务端日志不含关键词原文、命中正文，也不含它们的指纹"""
    _run(
        _provider(), [_log_msg(log_file, question=f"{KEYWORD} 回传正常吗")],
        _calls(("c1", "search_log", json.dumps({"keywords": [KEYWORD]}))),
        _text("结论"),
    )
    text = "\n".join(captured)
    assert "日志工具轮次 1" in text, "正向对照：工具循环日志确实被抓到了"
    for needle in (KEYWORD, BODY_MARK, "回传成功", fingerprint(KEYWORD), fingerprint(json.dumps({"keywords": [KEYWORD]}))):
        assert needle not in text, f"服务端日志泄漏: {ascii(needle)}"


def test_openai_log_tools_not_stored_on_provider(log_file):
    """LogTools 不得挂到 provider 上：调用前后实例属性不变"""
    p = _provider()
    before = dict(vars(p))
    _run(p, [_log_msg(log_file)], _calls(("c1", "list_log_files", "{}")), _text("结论"))
    assert vars(p) == before


# ============ HTTPError 出口：分类用原文，日志与告警只放摘要 ============

def _long_body(middle):
    """中段放标记、超出摘要头窗(80)和尾窗(40)：只在头尾放会被摘要原样带出而假绿"""
    return '{"error":{"message":"' + "a" * 120 + middle + "b" * 120 + '"}}'


def test_openai_http_error_body_not_logged(captured):
    """上游错误响应体原文不进日志、不进 error_detail；状态码保留"""
    secret = "ZZECHO-民初23986号-回显正文"
    reply, meta = _run(_provider(), [_msg(Content(type="text", data="q"))], _http_err(400, _long_body(secret)))[0]
    assert secret not in "\n".join(captured)
    assert secret not in meta["error_detail"]
    assert "status=400" in meta["error_detail"]
    assert meta["error"] == "api"


@pytest.mark.parametrize("status, body, reply, error", [
    # 返回文案与分类按 HEAD 版本的行为钉死（_http_error_result 由 RAG 与工具路径共用）
    (400, "invalid request body", "❌ AI 服务调用失败: HTTP 400", "api"),
    (401, "invalid api key", "❌ AI 服务认证失败（API Key 无效或权限不足），已通知维护人员处理", "auth"),
    (403, "当前分组额度不足", "💳 AI 服务额度/余额不足，已通知维护人员处理，请稍后重试", "quota"),
    (429, "too many requests", "⏱️ AI 服务请求过于频繁，请稍后重试", "rate_limit"),
    (503, "upstream unavailable", "🔧 AI 服务暂时不可用，请稍后重试", "server"),
])
def test_openai_rag_http_error_behavior_unchanged(status, body, reply, error):
    """原 RAG 路径的报错：返回文案、metadata.error、mode 与改前一致，且只请求一次"""
    (got_reply, meta), server = _run(
        _provider(), [_msg(Content(type="text", data="q"))], _http_err(status, body)
    )
    assert len(server.payloads) == 1
    assert got_reply == reply
    assert meta["error"] == error
    assert meta["mode"] == "rag"
    assert f"status={status}" in meta["error_detail"]


def test_openai_http_error_429_classified_rate_limit():
    """429 告警分类不受摘要影响"""
    (_, meta), _ = _run(
        _provider(), [_msg(Content(type="text", data="q"))],
        _http_err(429, _long_body("Rate limit reached for requests")),
    )
    assert meta["error"] == "rate_limit"


def test_openai_http_error_quota_classified_from_raw_text():
    """额度提示只在响应体中段（摘要看不到）时仍判为 quota：分类必须用原文"""
    (_, meta), _ = _run(
        _provider(), [_msg(Content(type="text", data="q"))],
        _http_err(403, _long_body("当前分组额度不足")),
    )
    assert meta["error"] == "quota"


# ============ 与 Anthropic 通道的附件文本一致 ============

@pytest.fixture
def anthropic_parts(monkeypatch):
    """
    隔离加载 anthropic_provider（不进 sys.modules，不留桩）。
    运行环境没装 anthropic 时只在本用例期间挂桩。
    """
    if "anthropic" not in sys.modules:
        try:
            import anthropic  # noqa: F401
        except ImportError:
            stub = types.ModuleType("anthropic")
            stub.__getattr__ = lambda _attr: Mock()  # type: ignore[attr-defined]
            monkeypatch.setitem(sys.modules, "anthropic", stub)
    path = Path(openai_provider.__file__).with_name("anthropic_provider.py")
    spec = importlib.util.spec_from_file_location("_anthropic_provider_isolated", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.AnthropicProvider._content_to_api_parts


@pytest.mark.parametrize("tools", [True, False])
@pytest.mark.parametrize("text", ["", "ERROR boom"])
@pytest.mark.parametrize("note", ["", "压缩包，已解压到目录"])
@pytest.mark.parametrize("path", ["/data/log_files/oc/debug.log", ""])
def test_flatten_log_file_matches_anthropic_parts(anthropic_parts, tools, text, note, path):
    """同一份附件在两个通道下文本完全一致（含提示措辞与先后顺序）"""
    c = Content(type="log_file", data={"name": "debug.log", "path": path, "text": text, "note": note})
    assert flatten_content_to_text(c, tools) == anthropic_parts(c, log_tools_available=tools)[0]["text"]
