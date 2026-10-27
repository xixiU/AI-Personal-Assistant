"""
引用追问时主动回查被引用消息附件的测试

背景（根因，改动前务必读懂）：
群聊里机器人的权限是 im:message.group_at_msg:readonly，只能收到 @机器人 的消息。
用户拖入文件时飞书客户端立即发送，那条纯文件消息**根本不会推 webhook 事件** ——
机器人对它毫无感知，_defer_lone_attachment 没有机会执行，pending_logs 恒为空。
用户随后引用该文件消息 @机器人提问时，机器人只收到这一个事件，事件里只有
parent_id / root_id 两个 ID，不内联被引用消息的内容。

所以「引用文件消息追问」要能工作，只能靠这两个 ID 主动回查（已实测：
GET /open-apis/im/v1/messages/{id} 对从未推送的消息也返回 code=0）。

另一条关键约束：回查与历史重建是两个**正交**的补全，不是互为 fallback。
chat_history 的 jsonl record 结构压根不存附件，所以「历史命中几轮」与「附件能不能
拿到」是两件独立的事；写成「历史没命中才回查」会让历史命中时附件永远拿不到。
"""
import json
import sys
import types
from unittest.mock import Mock

# anthropic SDK 可能未安装（本项目按规范只做静态测试，不要求装齐运行时依赖），
# 注入最小桩模块，保证"回查到的附件能触发 agentic 模式"这条断言可独立验证。
# 与 test_chain_mode_inherit.py 的桩写法保持一致。
if "anthropic" not in sys.modules:
    _stub = types.ModuleType("anthropic")

    class _APIError(Exception):
        pass

    class _APIStatusError(_APIError):
        def __init__(self, message, status_code=500, response=None):
            super().__init__(message)
            self.message = message
            self.status_code = status_code
            self.response = response

    _stub.APIError = _APIError
    _stub.APITimeoutError = _APIError
    _stub.APIConnectionError = _APIError
    _stub.APIStatusError = _APIStatusError
    _stub.Anthropic = lambda **kw: Mock(base_url=kw.get("base_url"), messages=Mock())
    sys.modules["anthropic"] = _stub


def _make_app():
    """
    构造只带回查所需最小状态的 AIAssistant 实例，避免拉起整个依赖链。

    新符号（_fetch_quoted_attachments 等）在函数内延迟导入：模块级导入会让
    「临时还原修复」的红灯验证退化成 ImportError，而 ImportError 不是断言失败、
    不算有效红灯（本项目踩过的坑）。
    """
    import threading

    from ai_assistant.main import AIAssistant

    class _StubApp:
        def __init__(self):
            self._quoted_attachment_cache = {}
            self._quoted_cache_lock = threading.Lock()
            self._quoted_cache_max_keys = 200
            self.log_file_manager = _StubLogManager()

        _fetch_quoted_attachments = AIAssistant._fetch_quoted_attachments
        _collect_quoted_log_files = AIAssistant._collect_quoted_log_files

    return _StubApp()


class _StubLogManager:
    """只实现扩展名白名单，回查路径用到的就是这一个方法"""

    enabled = True

    def is_allowed(self, file_name: str) -> bool:
        return str(file_name).lower().endswith((".log", ".txt", ".out", ".err", ".zip"))


class _StubAdapter:
    """
    飞书适配器桩：按 message_id 返回预置的消息详情，记录下载调用。

    _parse_message_content 直接复用真实实现，保证测的是生产解析逻辑。
    """

    def __init__(self, messages=None, files_result=None, image_result=None):
        from ai_assistant.adapters.feishu_bot import FeishuBotAdapter

        self._messages = messages or {}
        self._files_result = files_result if files_result is not None else []
        self._image_result = image_result
        self.get_message_calls = []
        self.collect_calls = []
        self.download_image_calls = []
        self._parse_message_content = FeishuBotAdapter._parse_message_content

    def get_message(self, message_id):
        self.get_message_calls.append(message_id)
        return self._messages.get(message_id)

    def _collect_log_files(self, message_id, files, chat_id):
        self.collect_calls.append((message_id, [f.get("file_key") for f in files], chat_id))
        if callable(self._files_result):
            return self._files_result(message_id, files, chat_id)
        return list(self._files_result)

    def download_image(self, message_id, image_key):
        self.download_image_calls.append((message_id, image_key))
        return self._image_result


def _file_message(file_key="file_v3_aaa", file_name="app.log", chat_id="oc_1"):
    """一条 msg_type=file 的消息详情（即 get_message 返回的 data.items[0]）"""
    return {
        "message_id": "om_file",
        "msg_type": "file",
        "chat_id": chat_id,
        "body": {"content": json.dumps({"file_key": file_key, "file_name": file_name})},
    }


def _text_message(message_id="om_text", text="上一轮的回答"):
    return {
        "message_id": message_id,
        "msg_type": "text",
        "chat_id": "oc_1",
        "body": {"content": json.dumps({"text": text})},
    }


def _log_content(name="app.log", path="/tmp/app.log", text="ERROR boom"):
    from ai_assistant.core.models import Content

    return Content(type="log_file", data={
        "name": name, "path": path, "text": text, "note": "",
    })


# ---------------------------------------------------------------- 回查主路径


def test_quoting_file_message_fetches_attachment():
    """
    首轮「引用文件消息 @机器人提问」必须回查到日志（本次修复的核心场景）。

    复现步骤：
    1. 群里拖入 app.log（这条消息不推 webhook，机器人完全没见过）
    2. 引用它 @机器人 问「这个日志有什么问题」
    3. 修复前：parent_id 被忽略，AI 只看到文字；期望：拿到 app.log
    """
    app = _make_app()
    expected = _log_content()
    adapter = _StubAdapter(
        messages={"om_file": _file_message()},
        files_result=[expected],
    )

    logs, image, notice = app._fetch_quoted_attachments(
        adapter, "om_file", "om_file", "oc_1"
    )

    assert logs == [expected], "引用的文件消息里的日志没被回查到"
    assert image is None
    assert notice == ""
    # parent_id == root_id 时只查一次，不浪费第二次 API 调用
    assert adapter.get_message_calls == ["om_file"]


def test_download_uses_parent_message_id_not_current():
    """
    下载附件必须用「附件所属那条消息」的 message_id（防回归）。

    resources 下载接口按"附件挂在哪条消息"鉴权。传当前提问消息的 ID 会 404，
    整条链路静默失败——而这个错误从日志上看就是一句"下载失败"，极难定位。
    """
    app = _make_app()
    adapter = _StubAdapter(
        messages={"om_file": _file_message(file_key="file_v3_xyz")},
        files_result=[_log_content()],
    )

    app._fetch_quoted_attachments(adapter, "om_file", "om_file", "oc_1")

    assert len(adapter.collect_calls) == 1
    called_message_id, called_keys, called_chat = adapter.collect_calls[0]
    assert called_message_id == "om_file", (
        f"下载用了错误的 message_id: {called_message_id}，"
        f"必须是被引用那条消息的 ID"
    )
    assert called_keys == ["file_v3_xyz"]
    assert called_chat == "oc_1"


def test_falls_back_to_root_when_parent_is_plain_text():
    """
    追问轮：parent 是机器人的纯文本回答，必须继续查 root 才能拿到日志。

    这是「引用机器人回答继续追问」的真实形态：
      parent_id = 机器人上一轮回答（text，无附件）
      root_id   = 最初那条文件消息（有附件）
    只查 parent 就会在第二轮起永久丢掉日志。
    """
    app = _make_app()
    expected = _log_content()
    adapter = _StubAdapter(
        messages={
            "om_bot_reply": _text_message("om_bot_reply"),
            "om_file": _file_message(),
        },
        files_result=[expected],
    )

    logs, image, notice = app._fetch_quoted_attachments(
        adapter, "om_bot_reply", "om_file", "oc_1"
    )

    assert logs == [expected], "parent 无附件时没有继续查 root"
    assert adapter.get_message_calls == ["om_bot_reply", "om_file"]
    assert notice == ""


def test_at_most_two_lookups():
    """
    回查次数硬性封顶两次，不做无界向上爬。

    链中间节点都是「机器人回答 / 用户追问」这类纯文本，附件只可能出现在直接
    引用点或链根，继续向上爬收益为零；而每跳一次都是一次网络往返，长链路下
    延迟不可控。
    """
    app = _make_app()
    adapter = _StubAdapter(
        messages={
            "om_p": _text_message("om_p"),
            "om_r": _text_message("om_r"),
        },
    )

    logs, image, notice = app._fetch_quoted_attachments(adapter, "om_p", "om_r", "oc_1")

    assert logs == []
    assert image is None
    # 链上都是纯文本不是故障，不该生成告知文本
    assert notice == ""
    assert len(adapter.get_message_calls) == 2, (
        f"回查次数超过 2 次: {adapter.get_message_calls}"
    )


def test_plain_text_parent_without_root_is_quiet():
    """被引用的是纯文本且无 root 时安静返回，不产生 notice"""
    app = _make_app()
    adapter = _StubAdapter(messages={"om_text": _text_message("om_text")})

    logs, image, notice = app._fetch_quoted_attachments(adapter, "om_text", "", "oc_1")

    assert (logs, image, notice) == ([], None, "")
    assert adapter.get_message_calls == ["om_text"]


def test_quoted_image_message_is_fetched():
    """被引用的是图片消息时也要回查（被引用消息不只可能是 file）"""
    app = _make_app()
    image_payload = {"data": "ZGVhZGJlZWY=", "media_type": "image/png"}
    adapter = _StubAdapter(
        messages={"om_img": {
            "message_id": "om_img",
            "msg_type": "image",
            "chat_id": "oc_1",
            "body": {"content": json.dumps({"image_key": "img_v2_abc"})},
        }},
        image_result=image_payload,
    )

    logs, image, notice = app._fetch_quoted_attachments(adapter, "om_img", "", "oc_1")

    assert image == image_payload
    assert logs == []
    assert notice == ""
    assert adapter.download_image_calls == [("om_img", "img_v2_abc")]


def test_quoted_post_message_attachment_is_fetched():
    """被引用的是富文本（post）时，内嵌的 file/media 附件也要取到"""
    app = _make_app()
    expected = _log_content(name="trace.log")
    content = {"content": [[
        {"tag": "text", "text": "看下这个"},
        {"tag": "file", "file_key": "file_v3_post", "file_name": "trace.log"},
    ]]}
    adapter = _StubAdapter(
        messages={"om_post": {
            "message_id": "om_post",
            "msg_type": "post",
            "chat_id": "oc_1",
            "body": {"content": json.dumps(content)},
        }},
        files_result=[expected],
    )

    logs, _image, notice = app._fetch_quoted_attachments(adapter, "om_post", "", "oc_1")

    assert logs == [expected]
    assert notice == ""


# ---------------------------------------------------------- 失败不静默


def test_download_failure_produces_notice():
    """
    有附件标识但读取全失败时必须出声（项目原则：不静默丢弃附件）。

    静默的后果是 AI 以为"用户只发了文字"，于是凭空猜一个答案，用户还以为
    机器人看过日志了。
    """
    app = _make_app()
    adapter = _StubAdapter(
        messages={"om_file": _file_message()},
        files_result=[],  # 下载失败
    )

    logs, image, notice = app._fetch_quoted_attachments(adapter, "om_file", "", "oc_1")

    assert logs == []
    assert image is None
    assert notice, "附件读取失败却没有生成任何告知文本，AI 会凭空作答"
    assert "附件" in notice


def test_unsupported_extension_produces_notice():
    """类型不在白名单时也要说明，而不是当作"没有附件"继续往上爬"""
    app = _make_app()
    adapter = _StubAdapter(
        messages={"om_file": _file_message(file_name="screenshot.docx")},
    )

    logs, _image, notice = app._fetch_quoted_attachments(adapter, "om_file", "", "oc_1")

    assert logs == []
    assert notice, "非日志类附件被静默跳过"
    # 白名单拦下的文件不该发起下载
    assert adapter.collect_calls == []


def test_partial_failure_lists_failed_names():
    """多个附件只读到一部分时，说明里要列出读不到的那些"""
    app = _make_app()
    content = {"content": [[
        {"tag": "file", "file_key": "k1", "file_name": "a.log"},
        {"tag": "file", "file_key": "k2", "file_name": "b.docx"},
    ]]}
    adapter = _StubAdapter(
        messages={"om_post": {
            "message_id": "om_post",
            "msg_type": "post",
            "chat_id": "oc_1",
            "body": {"content": json.dumps(content)},
        }},
        files_result=[_log_content(name="a.log")],
    )

    logs, _image, notice = app._fetch_quoted_attachments(adapter, "om_post", "", "oc_1")

    assert len(logs) == 1
    assert "b.docx" in notice, f"失败文件名没出现在说明里: {notice}"


def test_get_message_returning_none_is_tolerated():
    """get_message 查不到（无权限/已撤回）时不抛异常，整条主流程不能被带崩"""
    app = _make_app()
    adapter = _StubAdapter(messages={})

    logs, image, notice = app._fetch_quoted_attachments(adapter, "om_x", "om_y", "oc_1")

    assert (logs, image, notice) == ([], None, "")


def test_malformed_content_json_is_tolerated():
    """body.content 不是合法 JSON 时跳过该节点，不抛异常"""
    app = _make_app()
    adapter = _StubAdapter(messages={"om_bad": {
        "message_id": "om_bad",
        "msg_type": "file",
        "chat_id": "oc_1",
        "body": {"content": "{not json"},
    }})

    logs, image, notice = app._fetch_quoted_attachments(adapter, "om_bad", "", "oc_1")

    assert (logs, image, notice) == ([], None, "")


def test_adapter_without_get_message_is_skipped():
    """非飞书机器人适配器（UI 自动化版）没有回查能力，安静跳过"""
    app = _make_app()

    class _NoGetMessage:
        pass

    logs, image, notice = app._fetch_quoted_attachments(
        _NoGetMessage(), "om_x", "om_y", "oc_1"
    )

    assert (logs, image, notice) == ([], None, "")


# -------------------------------------------------------------------- 缓存


def test_same_file_key_is_not_downloaded_twice(tmp_path):
    """
    同一条引用链被反复追问时，同一个 file_key 不能重复下载。

    用户引用机器人回答继续问，每轮回查到的 file_key 都一样；不缓存就是每轮
    重下一遍整包日志（实测那份 thread dump 有 525KB）。
    """
    app = _make_app()
    real_file = tmp_path / "app.log"
    real_file.write_text("ERROR boom", encoding="utf-8")
    adapter = _StubAdapter(
        messages={"om_file": _file_message()},
        files_result=[_log_content(path=str(real_file))],
    )

    first_logs, _i1, _n1 = app._fetch_quoted_attachments(adapter, "om_file", "", "oc_1")
    second_logs, _i2, _n2 = app._fetch_quoted_attachments(adapter, "om_file", "", "oc_1")

    assert first_logs == second_logs
    assert len(adapter.collect_calls) == 1, (
        f"同一个 file_key 被下载了 {len(adapter.collect_calls)} 次，缓存没生效"
    )


def test_cache_entry_with_vanished_path_is_refetched(tmp_path):
    """
    缓存的落地文件被定时清理后必须重新下载（防回归）。

    LogFileManager 有 retention_hours 定时清理。缓存住一个已被清掉的路径，
    AI 会拿着不存在的路径去用代码排查工具读文件，读不到还以为是日志本身
    有问题，排查方向直接跑偏。
    """
    app = _make_app()
    real_file = tmp_path / "app.log"
    real_file.write_text("ERROR boom", encoding="utf-8")
    adapter = _StubAdapter(
        messages={"om_file": _file_message()},
        files_result=[_log_content(path=str(real_file))],
    )

    app._fetch_quoted_attachments(adapter, "om_file", "", "oc_1")
    assert len(adapter.collect_calls) == 1

    # 模拟定时清理把落地文件删掉
    real_file.unlink()

    app._fetch_quoted_attachments(adapter, "om_file", "", "oc_1")
    assert len(adapter.collect_calls) == 2, (
        "缓存的落地文件已被清理，却没有重新下载"
    )


def test_refetched_entry_replaces_stale_cache(tmp_path):
    """
    失效条目重下后必须更新缓存，第三轮要重新命中（防回归）。

    只做"校验失败就跳过缓存去下载"是不够的：若失效条目没删掉、或重下结果没
    覆盖写回，每轮追问都要走一遍"命中→校验失败→重下"，缓存等于完全失效，
    而日志包动辄几百 KB，引用链被追问十轮就是十次重下。
    """
    app = _make_app()
    first_file = tmp_path / "app.log"
    first_file.write_text("ERROR boom", encoding="utf-8")
    second_file = tmp_path / "app-again.log"
    second_file.write_text("ERROR boom again", encoding="utf-8")

    # 每次下载返回当前应落地的路径：第一次 first_file，之后 second_file
    state = {"n": 0}

    def _files_result(message_id, files, chat_id):
        state["n"] += 1
        path = first_file if state["n"] == 1 else second_file
        return [_log_content(path=str(path))]

    adapter = _StubAdapter(
        messages={"om_file": _file_message()},
        files_result=_files_result,
    )

    app._fetch_quoted_attachments(adapter, "om_file", "", "oc_1")
    assert len(adapter.collect_calls) == 1

    # 定时清理删掉第一份落地文件 → 第二轮应重下
    first_file.unlink()
    logs_second, _i, _n = app._fetch_quoted_attachments(adapter, "om_file", "", "oc_1")
    assert len(adapter.collect_calls) == 2
    assert logs_second[0].data["path"] == str(second_file), "重下结果不对"

    # 第三轮：新路径仍存在，必须命中缓存而不是再下一次
    app._fetch_quoted_attachments(adapter, "om_file", "", "oc_1")
    assert len(adapter.collect_calls) == 2, (
        "重下后缓存没更新，每轮追问都在重复下载同一个文件"
    )

    # 失效条目不得残留（同一个 file_key 只应有一条，且指向新路径）
    assert len(app._quoted_attachment_cache) == 1
    cached = app._quoted_attachment_cache["file_v3_aaa"]
    assert cached.data["path"] == str(second_file), "缓存里留的还是失效的旧路径"


def test_concurrent_branch_requires_text_for_deferred_replay():
    """
    哨兵：并发分支不丢附件依赖「回查要求有文字」这个前提（防回归）。

    _process_event 的"session 处理中"分支不执行回查。它不丢附件的唯一依据是：
    回查触发条件要求 text 非空 → 落到那条分支的消息必然带文字 → 必然命中
    `if text:` 登记进 _deferred_messages → finally 里必然补跑一次。

    而无文字的分支只入上下文、直接 return、**不补跑**。所以一旦有人放宽回查
    条件允许无文字触发，这条路径就会静默丢附件。这个耦合不可见、很脆，
    用哨兵把两端一起钉住。
    """
    import pathlib

    src = pathlib.Path(__file__).resolve().parents[2] / "src" / "ai_assistant" / "main.py"
    full = src.read_text(encoding="utf-8")

    start = full.index("    def _process_event(")
    end = full.index("\n    def ", start + 10)
    body = full[start:end]

    # 前提一：回查条件必须要求 text 非空
    i_call = body.index("self._fetch_quoted_attachments(")
    i_if = body.rindex("\n            if (", 0, i_call)
    condition = body[i_if:i_call]
    assert "and text" in condition, (
        "回查条件不再要求 text 非空，但并发分支的无文字消息不会补跑，"
        "该路径会静默丢附件 —— 放宽条件前必须先让无文字分支也能补跑"
    )

    # 前提二：登记补跑仍挂在 `if text:` 上（两端必须同时成立）
    i_defer_reg = body.index("self._deferred_messages[session_id] = event_data")
    preceding = body[:i_defer_reg]
    assert preceding.rstrip().endswith("if text:"), (
        "补跑登记不再由 `if text:` 守卫，并发分支的行为前提已变，"
        "请重新核对回查在该分支上是否还安全"
    )


def test_cache_is_bounded():
    """缓存必须有上限，长跑进程不能无界增长"""
    app = _make_app()
    app._quoted_cache_max_keys = 3

    for i in range(10):
        key = f"file_v3_{i}"
        adapter = _StubAdapter(
            messages={"om_file": _file_message(file_key=key, file_name=f"{i}.log")},
            files_result=[_log_content(name=f"{i}.log", path="")],
        )
        app._fetch_quoted_attachments(adapter, "om_file", "", "oc_1")

    assert len(app._quoted_attachment_cache) <= 4, (
        f"缓存超出上限: {len(app._quoted_attachment_cache)}"
    )


# -------------------------------------------- 与下游模式判定 / 主流程的衔接


def test_fetched_logs_trigger_agentic_mode():
    """
    回查到的附件必须能让本轮进入 agentic 排查模式 —— 这是整个需求的目的。

    _current_turn_has_attachment 看的是当前轮 user 消息里有没有 log_file 内容块，
    所以回查结果必须是 log_file 类型的 Content 并并入当前轮。
    """
    from ai_assistant.core.models import Content, Message
    from ai_assistant.providers.anthropic_provider import AnthropicProvider

    app = _make_app()
    adapter = _StubAdapter(
        messages={"om_file": _file_message()},
        files_result=[_log_content()],
    )

    logs, _image, _notice = app._fetch_quoted_attachments(adapter, "om_file", "", "oc_1")
    assert logs, "前置条件：必须先回查到日志"

    # 还原主流程的拼装顺序：附件在前，文字在后
    messages = [Message(
        role="user",
        content=list(logs) + [Content(type="text", data="这个日志什么问题")],
        timestamp=None,
    )]

    assert AnthropicProvider._current_turn_has_attachment(messages) is True, (
        "回查到的附件没能让本轮进入 agentic 排查模式"
    )


def test_attachments_are_ordered_before_text_in_process_event():
    """
    哨兵：quoted_logs 必须排在 text 的 Content 之前。

    provider 侧按内容块顺序拼 prompt，附件排在问题之后会让"先给材料再提问"
    的结构倒置。与 pending/own 既有顺序保持一致。
    """
    import pathlib

    src = pathlib.Path(__file__).resolve().parents[2] / "src" / "ai_assistant" / "main.py"
    full = src.read_text(encoding="utf-8")

    start = full.index("    def _process_event(")
    end = full.index("\n    def ", start + 10)
    body = full[start:end]

    # 只看主流程那段 content_parts 构建。_process_event 里有两处构建：
    # 前一处是"session 处理中"分支（只入上下文、不调 AI），不参与本轮 prompt；
    # 从全函数体 index 会命中那一处的 text append，位置比较无论对错都会过。
    i_main = body.index("content_parts = []", body.index("self._processing_sessions.add"))
    main_block = body[i_main:]

    i_text = main_block.index('content_parts.append(Content(type="text", data=text))')
    for name in ("pending_logs", "own_logs", "quoted_logs"):
        i_logs = main_block.index(f"for _lc in {name}:")
        assert i_logs < i_text, (
            f"{name} 被排到了 text 之后，材料与问题顺序倒置"
        )


def test_refetch_is_not_gated_on_history_being_empty():
    """
    哨兵：回查不得写成「本地历史缺失才回查」的 fallback（防回归）。

    chat_history 的 jsonl record 结构压根不存附件，所以「历史命中几轮」与
    「附件能不能拿到」是两件独立的事。若把回查做成历史的 fallback，历史命中时
    附件就永远拿不到了 —— 而"引用同一条链反复追问"恰好总是历史命中。

    检查方式：回查的调用点不能依赖 context_messages / records 这类历史产物。
    """
    import pathlib

    src = pathlib.Path(__file__).resolve().parents[2] / "src" / "ai_assistant" / "main.py"
    full = src.read_text(encoding="utf-8")

    start = full.index("    def _process_event(")
    end = full.index("\n    def ", start + 10)
    body = full[start:end]

    i_call = body.index("self._fetch_quoted_attachments(")
    # 取调用点所在的 if 条件块（往前找最近的 if，含多行条件）
    i_if = body.rindex("\n            if (", 0, i_call)
    condition = body[i_if:i_call]

    for forbidden in ("context_messages", "_rebuild_context_from_chain", "chat_history"):
        assert forbidden not in condition, (
            f"回查的触发条件里出现了 {forbidden}，说明被写成了历史的 fallback；"
            f"jsonl 不存附件，这会让历史命中时附件永远拿不到"
        )

    # 历史重建本身也不该被回查改写
    assert "quoted" not in full[
        full.index("    def _rebuild_context_from_chain("):
        full.index("\n    def ", full.index("    def _rebuild_context_from_chain(") + 10)
    ], "_rebuild_context_from_chain 被混入了附件回查逻辑，两者必须保持正交"


def test_no_refetch_when_current_turn_has_own_attachment():
    """
    哨兵：本轮自带附件时不回查。

    白费一次 API 调用，且用户当前发的附件才是他想问的东西，引用链上的旧附件
    此时不该再抢位置。
    """
    import pathlib

    src = pathlib.Path(__file__).resolve().parents[2] / "src" / "ai_assistant" / "main.py"
    full = src.read_text(encoding="utf-8")

    start = full.index("    def _process_event(")
    end = full.index("\n    def ", start + 10)
    body = full[start:end]

    i_call = body.index("self._fetch_quoted_attachments(")
    i_if = body.rindex("\n            if (", 0, i_call)
    condition = body[i_if:i_call]

    for required in ("not image_data", "not own_logs", "not pending_logs", "parent_id"):
        assert required in condition, (
            f"回查的触发条件里缺少 {required}，本轮已有附件时会白跑一次回查"
        )


def test_refetch_logging_does_not_dump_file_content():
    """
    哨兵：回查链路的日志不得打印日志文件正文。

    Content 是 dataclass，默认 repr 会把 data["text"] 整段输出。用户引用一份
    几万字的 app.log，整份原文就落进日志文件，违反项目规范「不得在日志中记录
    完整用户消息」。models.Content.summary() 已提供脱敏输出。
    """
    import pathlib

    src = pathlib.Path(__file__).resolve().parents[2] / "src" / "ai_assistant" / "main.py"
    full = src.read_text(encoding="utf-8")

    start = full.index("    def _process_event(")
    end = full.index("\n    def ", start + 10)
    body = full[start:end]

    i_log = body.index("引用回查附件已并入本轮")
    # 该日志语句附近必须用 summary()，不能直接把 Content 丢进 format
    snippet = body[i_log - 200:i_log + 400]
    assert ".summary()" in snippet, (
        "引用回查的日志没有用 Content.summary() 脱敏，日志文件正文会整段落盘"
    )
