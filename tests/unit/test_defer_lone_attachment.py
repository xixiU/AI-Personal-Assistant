"""
纯附件延迟等待机制测试（先发日志文件/截图 → 紧接着发问题）

背景（防回归）：
飞书客户端拖入文件即刻发送，文件无法与文字拼成一条消息，因此"先发文件、
紧接着发问题"是日志排查场景的主要用法。若收到纯文件消息就立即调 AI，
AI 手里没有任何问题描述只能反问。_defer_lone_attachment 把附件暂存一个
短窗口，等后续文字消息把它取走并入同一轮。

原实现 _defer_lone_image 只暂存图片，纯文件消息不进等待窗口，导致
"先发文件后发问题"时日志正文永远到不了 AI（本次修复）。
"""
import threading
import time

from ai_assistant.core.models import Content


class _StubApp:
    """只保留延迟机制所需最小状态的桩，避免拉起整个 AIAssistant 依赖链"""

    def __init__(self, wait: float = 0.6):
        import threading

        self._processing_lock = threading.Lock()
        self._pending_lone_images = {}
        self._lone_image_wait = wait

    # 直接复用被测实现，保证测的是真代码而不是副本
    from ai_assistant.main import AIAssistant

    _defer_lone_attachment = AIAssistant._defer_lone_attachment


def _log_content(name: str = "app.log") -> Content:
    return Content(type="log_file", data={
        "name": name,
        "path": f"/tmp/{name}",
        "text": "ERROR something broke",
        "note": "",
    })


def test_lone_log_file_taken_over_by_text():
    """等待期内后续文字消息取走暂存附件时，应返回 True（本轮交由后续消息处理）"""
    app = _StubApp(wait=3.0)
    logs = [_log_content()]
    captured = {}

    def _simulate_text_message():
        # 模拟主流程收到文字消息时的 pop 动作
        time.sleep(0.5)
        with app._processing_lock:
            captured["popped"] = app._pending_lone_images.pop("chat1", None)

    import threading
    threading.Thread(target=_simulate_text_message, daemon=True).start()

    result = app._defer_lone_attachment("chat1", None, logs, "om_1")

    assert result is True
    assert captured["popped"] is not None
    # 暂存结构必须带 log_contents，否则主流程取不到日志（本次修复的核心）
    assert captured["popped"]["log_contents"] == logs
    assert captured["popped"]["image"] is None


def test_lone_image_still_works():
    """图片场景必须保持原行为不回归"""
    app = _StubApp(wait=3.0)
    image = {"data": "deadbeef", "media_type": "image/png"}

    def _simulate_text_message():
        time.sleep(0.5)
        with app._processing_lock:
            app._pending_lone_images.pop("chat1", None)

    import threading
    threading.Thread(target=_simulate_text_message, daemon=True).start()

    result = app._defer_lone_attachment("chat1", image, None, "om_1")

    assert result is True


def test_image_and_logs_both_stashed():
    """图片与日志文件同时存在时，两者都要暂存"""
    app = _StubApp(wait=3.0)
    image = {"data": "deadbeef", "media_type": "image/png"}
    logs = [_log_content("a.log"), _log_content("b.log")]
    captured = {}

    def _simulate_text_message():
        time.sleep(0.5)
        with app._processing_lock:
            captured["popped"] = app._pending_lone_images.pop("chat1", None)

    import threading
    threading.Thread(target=_simulate_text_message, daemon=True).start()

    app._defer_lone_attachment("chat1", image, logs, "om_1")

    assert captured["popped"]["image"] == image
    assert len(captured["popped"]["log_contents"]) == 2


def test_timeout_returns_false_and_clears_stash():
    """等待超时应返回 False（调用方按纯附件处理），并清除暂存避免泄漏到下一轮"""
    app = _StubApp(wait=0.6)
    logs = [_log_content()]

    result = app._defer_lone_attachment("chat1", None, logs, "om_1")

    assert result is False
    assert "chat1" not in app._pending_lone_images


def test_new_attachment_overrides_and_takes_over():
    """等待期内收到新附件（ts 变化）也视为被接管，返回 True"""
    app = _StubApp(wait=3.0)

    def _simulate_second_attachment():
        time.sleep(0.5)
        with app._processing_lock:
            app._pending_lone_images["chat1"] = {
                "image": None,
                "log_contents": [_log_content("second.log")],
                "ts": time.time() + 999,  # 不同 ts
            }

    import threading
    threading.Thread(target=_simulate_second_attachment, daemon=True).start()

    result = app._defer_lone_attachment("chat1", None, [_log_content()], "om_1")

    assert result is True


def test_parse_stage_must_not_prefill_default_question():
    """
    哨兵：解析阶段不得给无文字的附件消息补默认提问。

    延迟等待的触发条件是 `not text`，解析阶段一旦补上默认提问，text 恒非空，
    _defer_lone_attachment 永远不会被调用 ——「先发文件、紧接着发问题」这条
    主要用法直接失效（第一条立即拿默认提问去问 AI，第二条问题另起一轮且
    手里没有日志）。这个回归曾真实发生过，故用哨兵锁住。

    只检查 _parse_feishu_event 函数体，避免「全文件搜索 + 位置比较」那种
    恒真断言：默认提问本来就只在 _process_event 里出现，而 _process_event
    在文件中排在 _parse_feishu_event 之前，全局位置比较无论对错都会通过。
    """
    import pathlib

    from ai_assistant.main import AIAssistant

    src = pathlib.Path(__file__).resolve().parents[2] / "src" / "ai_assistant" / "main.py"
    full = src.read_text(encoding="utf-8")

    question = AIAssistant._DEFAULT_ATTACHMENT_QUESTION

    # 切出 _parse_feishu_event 的函数体（到下一个同级 def 为止），
    # 并剥掉注释行——注释里提到默认提问是正常的（指路说明由谁来补）。
    start = full.index("    def _parse_feishu_event(")
    end = full.index("\n    def ", start + 10)
    parse_code = "\n".join(
        line for line in full[start:end].splitlines()
        if not line.lstrip().startswith("#")
    )

    assert question not in parse_code, (
        "_parse_feishu_event 的代码里又出现了默认提问字面量，"
        "解析阶段填 text 会让 _process_event 的 `not text` 恒为假、延迟等待失效"
    )
    assert "_DEFAULT_ATTACHMENT_QUESTION" not in parse_code, (
        "_parse_feishu_event 的代码里引用了 _DEFAULT_ATTACHMENT_QUESTION，"
        "同样会让延迟等待失效"
    )

    # 同时确认 _process_event 里补默认提问的位置在延迟判断之后
    p_start = full.index("    def _process_event(")
    p_end = full.index("\n    def ", p_start + 10)
    process_body = full[p_start:p_end]
    assert process_body.index("text = self._DEFAULT_ATTACHMENT_QUESTION") > \
        process_body.index("_defer_lone_attachment("), \
        "默认提问被放到了延迟判断之前，`not text` 判断将永远为假"


def test_consecutive_files_are_merged_not_overwritten():
    """
    连发多个日志文件时暂存必须合并（防回归）。

    飞书客户端拖入多个文件会发出多条独立的 file 消息。原实现对
    _pending_lone_images[session_id] 是覆盖赋值，导致：两条消息的等待线程
    都因 ts 变化而认为"已被接管"直接返回 True，谁都不去处理第一个文件，
    用户随后提问时只取到最后一个——先到的日志被永久静默丢弃。
    而"拖多个日志文件再提问"正是日志排查的典型用法。

    复现步骤：
    1. 私聊连续拖入 a.log、b.log（间隔小于 lone_image_wait）
    2. 紧接着发问题文字
    3. 原实现：AI 只看到 b.log；期望：两份都看到
    """
    app = _StubApp(wait=3.0)
    captured = {}

    def _first():
        app._defer_lone_attachment("chat1", None, [_log_content("a.log")], "om_1")

    def _second():
        time.sleep(0.4)
        app._defer_lone_attachment("chat1", None, [_log_content("b.log")], "om_2")

    def _text_message():
        time.sleep(1.2)
        with app._processing_lock:
            captured["popped"] = app._pending_lone_images.pop("chat1", None)

    threads = [
        threading.Thread(target=_first),
        threading.Thread(target=_second),
        threading.Thread(target=_text_message, daemon=True),
    ]
    for t in threads:
        t.start()
    for t in threads[:2]:
        t.join()

    names = [c.data["name"] for c in captured["popped"]["log_contents"]]
    assert names == ["a.log", "b.log"], f"先到的附件被覆盖丢弃: {names}"


def test_merge_keeps_existing_image_when_second_has_none():
    """先发图片再发日志文件：合并后图片不能被 None 覆盖掉"""
    app = _StubApp(wait=3.0)
    image = {"data": "deadbeef", "media_type": "image/png"}

    with app._processing_lock:
        app._pending_lone_images["chat1"] = {
            "image": image,
            "log_contents": [],
            "ts": time.time(),
        }

    captured = {}

    def _text_message():
        time.sleep(0.5)
        with app._processing_lock:
            captured["popped"] = app._pending_lone_images.pop("chat1", None)

    threading.Thread(target=_text_message, daemon=True).start()
    app._defer_lone_attachment("chat1", None, [_log_content("a.log")], "om_2")

    assert captured["popped"]["image"] == image, "合并时把已暂存的图片丢了"
    assert len(captured["popped"]["log_contents"]) == 1


def test_debug_log_does_not_dump_log_file_text():
    """
    哨兵：debug 日志不得打印日志文件正文。

    Content 是 dataclass，默认 repr 会把 data["text"] 整段输出。用户传一份
    几万字的 app.log，DEBUG 级别下整份原文落进日志文件，既违反项目规范
    「不得在日志中记录完整用户消息」，也会刷爆日志。
    models.Content.summary() 已提供 log_file(名字, text_len=N) 的脱敏输出。
    """
    import pathlib

    src = pathlib.Path(__file__).resolve().parents[2] / "src" / "ai_assistant" / "main.py"
    full = src.read_text(encoding="utf-8")

    start = full.index("    def _process_event(")
    end = full.index("\n    def ", start + 10)
    body = full[start:end]

    i_log = body.index('logger.debug("parsed message:{}"')
    head = body[:i_log]
    assert '_parsed_log["log_contents"]' in head, (
        "debug 打印前没有对 log_contents 脱敏，日志文件正文会整段落盘"
    )
    assert ".summary()" in head, "脱敏应复用 Content.summary()，不要另造格式"


def test_stash_is_per_session():
    """不同会话的暂存互不干扰，避免跨群串附件"""
    app = _StubApp(wait=0.6)

    with app._processing_lock:
        app._pending_lone_images["chat_other"] = {
            "image": None,
            "log_contents": [_log_content("other.log")],
            "ts": time.time(),
        }

    app._defer_lone_attachment("chat1", None, [_log_content()], "om_1")

    # 自己超时清理不应动别的会话
    assert "chat_other" in app._pending_lone_images
