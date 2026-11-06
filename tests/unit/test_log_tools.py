"""
日志附件只读工具（tools/log_tools.py）及其在 Agentic 模式中的接入

背景 bug：用户发 41.5MB debug.log 的 zip 问"民初23986号回传正常吗"，AI 1 轮结束、
没调任何工具、凭空作答。根因有三层：
1. AI 的 6 个工具全是 git 仓库工具，read_file 是 git show，读不了本地日志；
2. 未启用代码排查（troubleshoot.enabled=false，默认）时 _should_use_agentic_mode
   开头直接 return False，带日志的消息进不了工具模式；
3. _content_to_api_parts 只在"没有内联正文"时提示可读路径，大 .log 内联了开头，
   一句提示都没有；zip 只给清单，正文一个字不给。

本文件覆盖：越界攻击、输出上限、编码、zip 目录、模式路由、_execute_tool 分发、
服务端日志不泄漏关键词与正文。
"""
import os
import sys
import types
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from loguru import logger

# 运行环境未装 anthropic / paramiko，桩法与 test_anthropic_api_diagnostics.py 一致
# （异常属性必须是真异常类，否则 except 子句求值时 TypeError）。
# 只在未注册时注册，不覆盖别的文件已经挂好的桩；刻意不桩 jieba，原因见该文件注释。
# paramiko 必须桩：tools/__init__.py 会 import operations_commands → paramiko。
for _dep in ("anthropic", "paramiko"):
    if _dep in sys.modules:
        continue
    if _dep == "paramiko":
        _stub = types.ModuleType("paramiko")
        _stub.__getattr__ = lambda _attr: Mock()  # type: ignore[attr-defined]
        sys.modules["paramiko"] = _stub
        continue
    _anthropic = types.ModuleType("anthropic")

    class _APIError(Exception):
        pass

    class _APIStatusError(_APIError):
        def __init__(self, message="", status_code=500, response=None):
            super().__init__(message)
            self.message = message
            self.status_code = status_code
            self.response = response
            self.request_id = None

    _anthropic.APIError = _APIError
    _anthropic.APITimeoutError = type("APITimeoutError", (_APIError,), {})
    _anthropic.APIConnectionError = type("APIConnectionError", (_APIError,), {})
    _anthropic.APIStatusError = _APIStatusError
    _anthropic.Anthropic = lambda **kw: Mock(base_url=kw.get("base_url"), messages=Mock())
    sys.modules["anthropic"] = _anthropic

from ai_assistant.core.models import Content, Message
from ai_assistant.providers.anthropic_provider import AnthropicProvider
from ai_assistant.tools import log_tools as lt
from ai_assistant.tools.log_tools import (
    LOG_TOOL_NAMES,
    MAX_HITS,
    MAX_LINE_CHARS,
    MAX_READ_LINES,
    LogTools,
    collect_log_paths,
)

KEYWORD = "民初23986号"
ASCII_MARK = "TRACEZQX9"
BODY_MARK = "SECRETBODY77"


# ==================== 夹具 ====================

@pytest.fixture
def tree(tmp_path):
    """
    模拟 data/log_files：
      sessA/app.log                    本会话的单文件附件
      sessA/logs_unzipped/a.log        本会话 zip 解压目录
      sessA/logs_unzipped/sub/b.log.1  轮转文件（extract_zip 的清单里不会有它）
      sessA/logs_unzipped/data.bin     二进制
      sessA/logs_unzipped2/x.log       相邻同前缀目录（不在白名单）
      sessB/secret.log                 别的会话的日志
    """
    base = tmp_path / "log_files"
    a = base / "sessA"
    unz = a / "logs_unzipped"
    (unz / "sub").mkdir(parents=True)
    (a / "logs_unzipped2").mkdir()
    (base / "sessB").mkdir(parents=True)

    (a / "app.log").write_text("line1 hello\nline2 KW01 here\n", encoding="utf-8")
    (unz / "a.log").write_text("2026 INFO start\n2026 ERROR KW01 in a\n", encoding="utf-8")
    (unz / "sub" / "b.log.1").write_text("old KW01 in b\n", encoding="utf-8")
    (unz / "data.bin").write_bytes(b"\x00\x01KW01\x00binary")
    (a / "logs_unzipped2" / "x.log").write_text("sibling KW01 OTHER\n", encoding="utf-8")
    (base / "sessB" / "secret.log").write_text("other user KW01 SECRET_B\n", encoding="utf-8")
    return SimpleNamespace(
        base=base, a=a, unz=unz,
        app=a / "app.log",
        sibling=a / "logs_unzipped2" / "x.log",
        secret=base / "sessB" / "secret.log",
    )


@pytest.fixture
def tools(tree):
    return LogTools([str(tree.app), str(tree.unz)])


@pytest.fixture
def captured():
    lines = []
    sink_id = logger.add(lambda m: lines.append(str(m)), level="DEBUG", format="{message}")
    yield lines
    logger.remove(sink_id)


def _hit_files(result):
    return {r["file"] for r in result.get("results", [])}


def _all_hit_text(result):
    return "\n".join(h["text"] for r in result.get("results", []) for h in r["hits"])


# ==================== 1. 越界攻击 ====================

def test_log_tools_reject_other_session_file(tools, tree):
    """同 log_files 下另一个会话的文件：读、搜都拒绝"""
    r = tools.read_lines(str(tree.secret), 1)
    assert "error" in r and "content" not in r
    s = tools.search(["KW01"], path=str(tree.secret))
    assert "error" in s and "results" not in s


def test_log_tools_reject_absolute_path_outside(tools, tmp_path):
    """白名单外的绝对路径（存在的文件）拒绝，且与"不存在"返回同一句错误"""
    outside = tmp_path / "outside.log"
    outside.write_text("KW01 outside\n", encoding="utf-8")
    r1 = tools.read_lines(str(outside), 1)
    r2 = tools.read_lines(str(tmp_path / "nope.log"), 1)
    assert "error" in r1
    assert r1["error"] == r2["error"], "越界与不存在必须同一句错误，不给探针"


def test_log_tools_reject_dotdot_even_if_inside(tools, tree):
    """含 .. 一律拒绝，即使解析后仍在白名单内（不靠 resolve 兜底）"""
    p = os.path.join(str(tree.unz), "sub", "..", "a.log")
    r = tools.read_lines(p, 1)
    assert "error" in r and ".." in r["error"]


def test_log_tools_reject_dotdot_escape(tools, tree):
    """../ 穿越到别的会话"""
    p = os.path.join(str(tree.unz), "..", "..", "sessB", "secret.log")
    assert "error" in tools.read_lines(p, 1)
    assert "error" in tools.search(["KW01"], path=p)


def test_log_tools_reject_relative_path(tools, tree, monkeypatch):
    """相对路径一律拒绝：cwd 恰好在白名单目录时，相对路径会解析进白名单，不能靠 resolve 兜底"""
    monkeypatch.chdir(tree.a)
    r = tools.read_lines("app.log", 1)
    assert "error" in r and "content" not in r


def test_log_tools_reject_sibling_prefix_dir(tools, tree):
    """xxx_unzipped2 与白名单目录 xxx_unzipped 同前缀，不能被当成子路径"""
    r = tools.read_lines(str(tree.sibling), 1)
    assert "error" in r
    assert "OTHER" not in _all_hit_text(tools.search(["KW01"]))


def _try_symlink(src, dst):
    try:
        os.symlink(src, dst)
    except (OSError, NotImplementedError) as e:
        pytest.skip(f"当前环境无权限创建符号链接，跳过: {e}")


def test_log_tools_reject_symlink_file_outside(tools, tree):
    """zip 目录里指向白名单外的符号链接文件：搜索、列表、读取都看不到目标内容"""
    link = tree.unz / "evil.log"
    _try_symlink(tree.secret, link)
    assert "SECRET_B" not in _all_hit_text(tools.search(["KW01"]))
    assert "SECRET_B" not in _all_hit_text(tools.search(["KW01"], path=str(tree.unz)))
    assert str(tree.secret.resolve()) not in {f["path"] for f in tools.list_files()["files"]}
    assert "error" in tools.read_lines(str(link), 1)


def test_log_tools_reject_junction_outside(tools, tree):
    """
    Windows junction 指向白名单外目录。实测 Python 3.13 的 os.walk(followlinks=False)
    会走进 junction，所以逐个文件 resolve 复核是唯一防线。
    """
    if sys.platform != "win32":
        pytest.skip("junction 仅 Windows")
    import _winapi
    junc = tree.unz / "junc"
    try:
        _winapi.CreateJunction(str(tree.base / "sessB"), str(junc))
    except OSError as e:
        pytest.skip(f"无法创建 junction: {e}")
    assert "SECRET_B" not in _all_hit_text(tools.search(["KW01"]))
    assert "error" in tools.read_lines(str(junc / "secret.log"), 1)


def test_collect_log_paths_only_log_file_blocks(tree):
    """白名单只来自 log_file 块的 path；文本里提到的路径、超限说明块（path 为空）都不算"""
    msgs = [
        Message(role="user", content=[
            Content(type="log_file", data={"name": "app.log", "path": str(tree.app), "text": "", "note": ""}),
            Content(type="log_file", data={"name": "big.log", "path": "", "text": "", "note": "超限"}),
            Content(type="text", data=f"请读 {tree.secret}"),
        ], timestamp=None),
        Message(role="assistant", content=[Content(type="text", data="好")], timestamp=None),
        Message(role="user", content=[
            Content(type="log_file", data={"name": "z.zip", "path": str(tree.unz), "text": "", "note": "zip"}),
            Content(type="log_file", data={"name": "app.log", "path": str(tree.app), "text": "", "note": ""}),
        ], timestamp=None),
    ]
    assert collect_log_paths(msgs) == [str(tree.app), str(tree.unz)]


def test_collect_current_turn_log_paths_only_trailing_user(tree):
    """只收末尾连续 user 消息：历史轮的附件不算，当前轮多条 user 消息按原顺序收"""
    from ai_assistant.tools.log_tools import collect_current_turn_log_paths

    def _lf(p):
        return Content(type="log_file", data={"name": "x", "path": str(p), "text": "", "note": ""})

    msgs = [
        Message(role="user", content=[_lf(tree.secret)], timestamp=None),
        Message(role="assistant", content=[Content(type="text", data="答")], timestamp=None),
        Message(role="user", content=[_lf(tree.app)], timestamp=None),
        Message(role="user", content=[_lf(tree.unz), Content(type="text", data="问")], timestamp=None),
    ]
    assert collect_current_turn_log_paths(msgs) == [str(tree.app), str(tree.unz)]
    assert collect_current_turn_log_paths(msgs[:2]) == []


# ==================== 2. 输出上限 ====================

def test_search_log_caps_hits_and_reports_omitted(tmp_path):
    f = tmp_path / "many.log"
    f.write_text("".join(f"row {i} KW01\n" for i in range(120)), encoding="utf-8")
    r = LogTools([str(f)]).search(["KW01"])
    assert r["total_hits"] == 120
    assert r["shown"] == MAX_HITS
    assert sum(len(x["hits"]) for x in r["results"]) == MAX_HITS
    assert r["omitted"] == 120 - MAX_HITS
    assert str(120 - MAX_HITS) in r["hint"]


def test_search_log_truncates_long_line_around_hit(tmp_path):
    """超长行截断到 MAX_LINE_CHARS，且截取窗口包含命中点（不是只取行首）"""
    f = tmp_path / "long.log"
    f.write_text("x" * 3000 + "KEYHIT" + "y" * 3000 + "\n", encoding="utf-8")
    r = LogTools([str(f)]).search(["KEYHIT"])
    text = r["results"][0]["hits"][0]["text"]
    assert "KEYHIT" in text
    assert len(text) < MAX_LINE_CHARS + 30


def test_read_log_lines_caps_line_count(tmp_path):
    f = tmp_path / "rows.log"
    f.write_text("".join(f"row{i}\n" for i in range(1, 1001)), encoding="utf-8")
    r = LogTools([str(f)]).read_lines(str(f), 1, 1000)
    assert r["end_line"] == MAX_READ_LINES
    assert r["content"].count("\n") + 1 == MAX_READ_LINES
    assert r["next_start_line"] == MAX_READ_LINES + 1
    assert r["eof"] is False


def test_read_log_lines_range_and_eof(tmp_path):
    f = tmp_path / "rows.log"
    f.write_text("".join(f"row{i}\n" for i in range(1, 11)), encoding="utf-8")
    r = LogTools([str(f)]).read_lines(str(f), 8)
    assert r["content"] == "8| row8\n9| row9\n10| row10"
    assert r["eof"] is True and "next_start_line" not in r


def test_iter_chunks_streams_line_without_newline(tmp_path, monkeypatch):
    """没有换行的大文件按块读，单块不超过上限，块号都算同一行"""
    monkeypatch.setattr(lt, "READ_CHUNK_BYTES", 1000)
    f = tmp_path / "oneline.log"
    f.write_bytes(b"a" * 2500 + b"\nnext\n")
    chunks = list(lt._iter_chunks(f))
    assert max(len(c) for _, c in chunks) <= 1000
    assert [n for n, _ in chunks] == [1, 1, 1, 2]


def test_search_log_counts_split_long_line_once(tmp_path, monkeypatch):
    """同一超长行被切成多块且每块都命中时，只算一条命中"""
    monkeypatch.setattr(lt, "READ_CHUNK_BYTES", 1000)
    f = tmp_path / "oneline.log"
    f.write_bytes((b"K1X2" + b"z" * 996) * 3 + b"\nsecond K1X2\n")
    r = LogTools([str(f)]).search(["K1X2"])
    assert r["total_hits"] == 2
    assert [h["line"] for h in r["results"][0]["hits"]] == [1, 2]


def test_search_log_rejects_short_and_too_many_keywords(tools):
    assert "error" in tools.search(["a"])
    assert "error" in tools.search([f"kw{i}" for i in range(lt.MAX_KEYWORDS + 1)])
    assert "error" in tools.search([])


def test_search_log_ignore_case(tools):
    assert tools.search(["kw01"])["total_hits"] == 0
    assert tools.search(["kw01"], ignore_case=True)["total_hits"] > 0


# ==================== 3. 编码 ====================

def test_search_log_gbk_chinese_keyword(tmp_path):
    f = tmp_path / "gbk.log"
    f.write_bytes("2026-11-05 ERROR 民初23986号 回传失败\n".encode("gbk"))
    r = LogTools([str(f)]).search([KEYWORD])
    assert r["total_hits"] == 1
    assert "回传失败" in _all_hit_text(r)


def test_search_log_mixed_encoding_and_invalid_bytes(tmp_path):
    """前半 UTF-8、后半 GBK、夹杂非法字节：逐行解码，不抛异常，两种编码都搜得到"""
    f = tmp_path / "mixed.log"
    f.write_bytes(
        "ok 民初23986号 utf8\n".encode("utf-8")
        + b"\xff\xfe\xfa broken\n"
        + "ok 民初23986号 gbk\n".encode("gbk")
    )
    r = LogTools([str(f)]).search([KEYWORD])
    assert [h["line"] for h in r["results"][0]["hits"]] == [1, 3]
    assert LogTools([str(f)]).read_lines(str(f), 1)["content"].count("\n") == 2


# ==================== 4. zip 解压目录 ====================

def test_search_log_across_unzipped_dir(tools, tree):
    """目录白名单：多个文件（含子目录、轮转文件）一起搜，二进制文件跳过"""
    r = tools.search(["KW01"], path=str(tree.unz))
    files = _hit_files(r)
    assert str((tree.unz / "a.log").resolve()) in files
    assert str((tree.unz / "sub" / "b.log.1").resolve()) in files
    assert str((tree.unz / "data.bin").resolve()) not in files
    assert r["files_searched"] == 2


def test_list_log_files_includes_rotated_excludes_binary(tools, tree):
    paths = {f["path"] for f in tools.list_files()["files"]}
    assert str((tree.unz / "sub" / "b.log.1").resolve()) in paths
    assert str((tree.unz / "data.bin").resolve()) not in paths
    assert str(tree.app.resolve()) in paths


def test_search_log_default_scope_is_whole_whitelist(tools, tree):
    """不给 path 时搜全部白名单（文件根 + 目录根），但不越出白名单"""
    r = tools.search(["KW01"])
    assert str(tree.app.resolve()) in _hit_files(r)
    assert str((tree.unz / "a.log").resolve()) in _hit_files(r)
    assert "SECRET_B" not in _all_hit_text(r)


# ==================== 5. 模式路由与 tools_schema ====================

def _provider(git=False):
    p = AnthropicProvider(api_key="k", base_url="http://fake", model="m")
    if git:
        p.git_tools = Mock()
        p.git_tools_enabled = True
    return p


def _log_msg(path, text="民初23986号回传正常吗"):
    return Message(role="user", content=[
        Content(type="log_file", data={"name": "debug.log", "path": str(path), "text": "", "note": ""}),
        Content(type="text", data=text),
    ], timestamp=None)


def _resp(blocks, stop="tool_use"):
    return SimpleNamespace(
        stop_reason=stop, content=blocks,
        usage=SimpleNamespace(input_tokens=1, output_tokens=1),
    )


def _tool_use(tid, name, inp):
    return SimpleNamespace(type="tool_use", id=tid, name=name, input=inp)


def _end(text="结论"):
    return _resp([SimpleNamespace(type="text", text=text)], stop="end_turn")


def test_no_git_with_log_file_enters_agentic(tree):
    p = _provider(git=False)
    assert p._should_use_agentic_mode([_log_msg(tree.app)]) is True


def test_no_git_without_attachment_stays_rag():
    """无附件：与改前一致，显式指令/关键词在无 git 时也不进工具模式"""
    p = _provider(git=False)
    for text in ("你好", "/查代码 xx", "帮我排查一下"):
        msgs = [Message(role="user", content=[Content(type="text", data=text)], timestamp=None)]
        assert p._should_use_agentic_mode(msgs) is False


def test_no_git_unreadable_log_stays_rag(tree):
    """超限说明块（path 为空）或已被清理的文件，不进入只有空工具列表的工具模式"""
    p = _provider(git=False)
    oversize = Message(role="user", content=[
        Content(type="log_file", data={"name": "big.log", "path": "", "text": "", "note": "超限"}),
        Content(type="text", data="看下"),
    ], timestamp=None)
    assert p._should_use_agentic_mode([oversize]) is False
    assert p._should_use_agentic_mode([_log_msg(tree.base / "gone.log")]) is False


def test_no_git_history_log_does_not_trigger(tree):
    """只有历史轮带日志、当前轮没有：无 git 时不触发（只看当前轮）"""
    p = _provider(git=False)
    msgs = [
        _log_msg(tree.app),
        Message(role="assistant", content=[Content(type="text", data="答")], timestamp=None,
                metadata={"mode": "agentic"}),
        Message(role="user", content=[Content(type="text", data="追问")], timestamp=None),
    ]
    assert p._should_use_agentic_mode(msgs) is False


def test_no_git_tools_schema_only_log_tools(tree):
    """无 git + 带日志：走到 Agentic，发出去的 tools 只有日志工具"""
    p = _provider(git=False)
    p.client.messages.create = Mock(return_value=_end())
    p._send_with_context([_log_msg(tree.app)], "", "s1")
    kwargs = p.client.messages.create.call_args.kwargs
    assert {t["name"] for t in kwargs["tools"]} == set(LOG_TOOL_NAMES)
    # 无 git 时追加覆盖说明，且不注入分支映射
    assert "本轮工具范围" in kwargs["system"]
    assert "【日志附件工具】" in kwargs["system"]
    # 附件说明里点名日志工具（Agentic 调用点传了 log_tools_available=True）
    assert "search_log" in str(kwargs["messages"][0]["content"])


def test_no_git_system_prompt_skips_branch_hint(tree):
    p = _provider(git=False)
    p.branch_hint = "4.3.x → release/4.3"
    p.client.messages.create = Mock(return_value=_end())
    p._send_with_context_agentic([_log_msg(tree.app)], "", "s1")
    assert "版本号与分支映射规则" not in p.client.messages.create.call_args.kwargs["system"]


def test_git_with_log_offers_both_tool_sets(tree):
    p = _provider(git=True)
    p.client.messages.create = Mock(return_value=_end())
    p._send_with_context_agentic([_log_msg(tree.app)], "", "s1")
    kwargs = p.client.messages.create.call_args.kwargs
    names = {t["name"] for t in kwargs["tools"]}
    assert {"search_code", "read_file"} <= names
    assert set(LOG_TOOL_NAMES) <= names
    assert "本轮工具范围" not in kwargs["system"]


def test_git_without_log_has_no_log_tools():
    """日志工具只在有 log_file 时加入；纯代码排查的工具列表与改前一致"""
    from ai_assistant.tools.git_tools import GIT_TOOLS_SCHEMA
    p = _provider(git=True)
    p.client.messages.create = Mock(return_value=_end())
    msgs = [Message(role="user", content=[Content(type="text", data="/查代码 X")], timestamp=None)]
    p._send_with_context_agentic(msgs, "", "s1")
    kwargs = p.client.messages.create.call_args.kwargs
    assert [t["name"] for t in kwargs["tools"]] == [t["name"] for t in GIT_TOOLS_SCHEMA]
    assert "【日志附件工具】" not in kwargs["system"]


def test_agentic_without_any_tool_falls_back_to_standard():
    p = _provider(git=False)
    p._send_with_context_standard = Mock(return_value=("rag", {"mode": "rag"}))
    p.client.messages.create = Mock()
    msgs = [Message(role="user", content=[Content(type="text", data="x")], timestamp=None)]
    assert p._send_with_context_agentic(msgs, "", "s1")[0] == "rag"
    p.client.messages.create.assert_not_called()


def test_content_hint_names_log_tools_only_when_available(tree):
    """
    有日志工具时，即使有内联正文也要提示（大文件只内联开头）；没有日志工具的
    模式（RAG/运维）不能提工具名，否则是对 AI 说假话。
    """
    c = Content(type="log_file", data={
        "name": "debug.log", "path": str(tree.app), "text": "开头部分", "note": "",
    })
    with_tools = AnthropicProvider._content_to_api_parts(c, log_tools_available=True)[0]["text"]
    assert "search_log" in with_tools and "案号" in with_tools
    # 与 OpenAI 通道共用同一常量，措辞不漂移
    assert lt.LOG_TOOLS_ATTACHMENT_HINT in with_tools
    assert with_tools.index("search_log") < with_tools.index("开头部分")

    without = AnthropicProvider._content_to_api_parts(c)[0]["text"]
    assert "search_log" not in without and "代码排查工具" not in without

    no_text = Content(type="log_file", data={"name": "x.log", "path": str(tree.app), "text": "", "note": ""})
    assert "本轮无法读取全文" in AnthropicProvider._content_to_api_parts(no_text)[0]["text"]


# ==================== 6. _execute_tool 分发 ====================

def test_execute_tool_runs_log_tool_without_git(tools):
    p = _provider(git=False)
    assert p.git_tools is None
    r = p._execute_tool("search_log", {"keywords": ["KW01"]}, log_tools=tools)
    assert r.get("total_hits", 0) > 0


def test_execute_tool_log_tool_without_whitelist_errors(tree):
    """白名单不挂在 provider 上：前一次调用的附件不能被后一次（不带 log_tools）读到"""
    p = _provider(git=False)
    p.client.messages.create = Mock(return_value=_end())
    p._send_with_context_agentic([_log_msg(tree.app)], "", "s1")
    r = p._execute_tool("read_log_lines", {"path": str(tree.app), "start_line": 1})
    assert "error" in r and "content" not in r


# ==================== 7. 服务端日志不泄漏 ====================

@pytest.fixture
def leak_file(tmp_path):
    d = tmp_path / "oc_chatXYZ123"
    d.mkdir()
    f = d / "debug.log"
    f.write_text(
        "2026-11-05 10:00:00 INFO boot\n"
        f"2026-11-05 10:00:01 INFO 回传成功 {KEYWORD} {ASCII_MARK} {BODY_MARK}\n",
        encoding="utf-8",
    )
    return f


def _assert_no_leak(lines):
    text = "\n".join(lines)
    assert "日志工具调用: tool=" in text, "正向对照：日志确实被抓到了"
    for needle in (KEYWORD, "23986", ASCII_MARK, BODY_MARK, "回传成功", "oc_chatXYZ123"):
        assert needle not in text, f"服务端日志泄漏: {ascii(needle)}"
    # 关键词指纹也不能记：案号空间小，8 位 sha1 可以离线枚举还原
    from ai_assistant.utils.log_summary import fingerprint
    for kw in (KEYWORD, ASCII_MARK):
        assert fingerprint(kw) not in text, f"服务端日志含关键词指纹: {ascii(kw)}"
    # 被 repr 转义后的形态也不能出现
    assert ascii(KEYWORD)[1:-1] not in text


def test_server_log_exception_hides_message(leak_file, captured, monkeypatch):
    """工具内部异常：返回错误给 AI，服务端只记异常类型，不记可能带路径/关键词的异常文本"""
    t = LogTools([str(leak_file)])

    def _boom(*_a, **_k):
        raise ValueError(f"{leak_file} {KEYWORD}")

    monkeypatch.setattr(t, "search", _boom)
    r = t.execute("search_log", {"keywords": [KEYWORD]})
    assert "error" in r
    assert "ValueError" in "\n".join(captured)
    _assert_no_leak(captured)


def test_server_log_no_keyword_or_body_serial(leak_file, captured):
    """串行路径（每轮一个工具）：搜索 + 读行，两轮都不落关键词、正文、路径"""
    p = _provider(git=False)
    p.client.messages.create = Mock(side_effect=[
        _resp([_tool_use("t1", "search_log", {"keywords": [KEYWORD, ASCII_MARK]})]),
        _resp([_tool_use("t2", "read_log_lines", {"path": str(leak_file), "start_line": 2})]),
        _end(),
    ])
    p._send_with_context_agentic([_log_msg(leak_file)], "", "s1")
    joined = "\n".join(captured)
    assert "hits=1" in joined and "lines=1" in joined
    assert "并行执行" not in joined
    # 工具结果确实回给了 AI（不是因为没执行才不泄漏）
    sent = str(p.client.messages.create.call_args_list[-1].kwargs["messages"])
    assert BODY_MARK in sent
    _assert_no_leak(captured)


def test_server_log_no_keyword_or_body_parallel(leak_file, captured):
    """并行路径（一轮两个只读日志工具）"""
    p = _provider(git=False)
    p.client.messages.create = Mock(side_effect=[
        _resp([
            _tool_use("t1", "search_log", {"keywords": [KEYWORD]}),
            _tool_use("t2", "read_log_lines", {"path": str(leak_file), "start_line": 1}),
        ]),
        _end(),
    ])
    p._send_with_context_agentic([_log_msg(leak_file)], "", "s1")
    joined = "\n".join(captured)
    assert "并行执行 2 个只读工具" in joined
    sent = str(p.client.messages.create.call_args_list[-1].kwargs["messages"])
    assert BODY_MARK in sent
    _assert_no_leak(captured)
