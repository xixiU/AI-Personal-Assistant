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

    # 直接复用被测实现，保证测的是真代码而不是副本。
    # _take_pending_and_defer 是 _process_event 里「pop 暂存 + 进等待窗口」那段的
    # 收口，必须连它一起复用：只测 _defer_lone_attachment 会漏掉调用方先无条件
    # pop 这个关键时序，曾因此产生假绿灯（见
    # test_consecutive_files_are_merged_not_overwritten）。
    from ai_assistant.main import AIAssistant

    _defer_lone_attachment = AIAssistant._defer_lone_attachment
    _take_pending_and_defer = AIAssistant._take_pending_and_defer


def _log_content(name: str = "app.log") -> Content:
    return Content(type="log_file", data={
        "name": name,
        "path": f"/tmp/{name}",
        "text": "ERROR something broke",
        "note": "",
    })


def _simulate_attachment_event(app, session_id, image_data, own_logs, message_id):
    """
    复现 _process_event 收到一条纯附件消息时的真实时序。

    关键是**不要**跳过调用方的无条件 pop 直接调 _defer_lone_attachment：
    线上流程是「先 pop 清空槽位 → 再 defer」，跳过 pop 会让 defer 内部的合并
    分支看到残留的旧附件而误判为正常，这正是假绿灯的成因。

    Returns:
        {"deferred": bool, "image_in_hand": ..., "logs_in_hand": [...]}
        deferred 对应 _process_event 里"直接 return"的分支；
        in_hand 是超时后调用方手里实际还剩的附件（会被并入 content_parts）。
    """
    pending_image, pending_logs, deferred = app._take_pending_and_defer(
        session_id, image_data, own_logs or [], "", message_id
    )
    return {
        "deferred": deferred,
        "image_in_hand": image_data or pending_image,
        "logs_in_hand": list(pending_logs) + list(own_logs or []),
    }


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

    # 同时确认 _process_event 里补默认提问的位置在延迟判断之后。
    # 锚点是 _take_pending_and_defer —— 等待窗口的入口（pop 暂存 + 调
    # _defer_lone_attachment 收口在那个方法里），它内部的 `not text` 判断
    # 才是真正会被默认提问破坏的那个条件。
    p_start = full.index("    def _process_event(")
    p_end = full.index("\n    def ", p_start + 10)
    process_body = full[p_start:p_end]
    assert process_body.index("text = self._DEFAULT_ATTACHMENT_QUESTION") > \
        process_body.index("_take_pending_and_defer("), \
        "默认提问被放到了延迟判断之前，`not text` 判断将永远为假"


def test_consecutive_files_are_merged_not_overwritten():
    """
    连发多个日志文件时暂存必须合并（防回归）。

    飞书客户端拖入多个文件会发出多条独立的 file 消息。原实现对
    _pending_lone_images[session_id] 是覆盖赋值，导致：两条消息的等待线程
    都因 ts 变化而认为"已被接管"直接返回 True，谁都不去处理第一个文件，
    用户随后提问时只取到最后一个——先到的日志被永久静默丢弃。
    而"拖多个日志文件再提问"正是日志排查的典型用法。

    必须复现 _process_event 的真实时序：**先无条件 pop，再 defer**
    （见 _simulate_attachment_event）。这个测试早先直接调 _defer_lone_attachment，
    槽位里还留着 a.log，于是刚好走进 defer 内部的合并分支、测试通过——但线上
    真实流程里调用方总是先把槽位 pop 空了，合并分支其实是死代码，a.log 停在
    第二条事件的局部变量里随 return 一起丢掉。假绿灯由此产生，不要再退回去。

    复现步骤：
    1. 私聊连续拖入 a.log、b.log（间隔小于 lone_image_wait）
    2. 紧接着发问题文字
    3. 修复前：AI 只看到 b.log；期望：两份都看到
    """
    app = _StubApp(wait=3.0)
    captured = {}

    def _first():
        _simulate_attachment_event(app, "chat1", None, [_log_content("a.log")], "om_1")

    def _second():
        time.sleep(0.4)
        _simulate_attachment_event(app, "chat1", None, [_log_content("b.log")], "om_2")

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

    assert captured.get("popped") is not None, "文字消息没取到任何暂存附件"
    names = [c.data["name"] for c in captured["popped"]["log_contents"]]
    assert names == ["a.log", "b.log"], f"先到的附件被覆盖丢弃: {names}"


def test_timeout_after_pop_keeps_both_attachments():
    """
    连发两个文件后一直没有文字说明（等待超时），两份附件都要留在调用方手里。

    定位说明（别误读）：这个用例在"只传 own_logs 进 defer"那版 bug 代码下**也是
    绿的** —— 超时路径上 pop 出来的 pending 仍停在调用方的局部变量里，会照常
    并进 content_parts，不丢。真正的丢附件只发生在「被接管」路径（见上一个用例）。

    它守的是另一件事：禁止把修复改写成「pop 延后到 defer 判断之后」。那种写法
    下 defer 内部会把 a.log 与 b.log 合并后写回槽位，超时时 del 掉的是合并后的
    那份，而调用方手里只有 b.log —— a.log 又丢一次，而且这次连上一个用例都
    抓不到（它走的是接管路径）。所以这里专门把超时路径钉住。

    时序：
    1. 私聊连续拖入 a.log、b.log（间隔小于 lone_image_wait）
    2. 之后一直不发文字，等待窗口超时
    3. 第二条事件手里必须是 a.log + b.log 两份
    """
    app = _StubApp(wait=0.6)
    results = {}

    def _first():
        results["first"] = _simulate_attachment_event(
            app, "chat1", None, [_log_content("a.log")], "om_1"
        )

    def _second():
        # 必须落在第一条的等待窗口内，才能复现「pop 走别人暂存的附件」
        time.sleep(0.2)
        results["second"] = _simulate_attachment_event(
            app, "chat1", None, [_log_content("b.log")], "om_2"
        )

    threads = [threading.Thread(target=_first), threading.Thread(target=_second)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # 第一条被第二条接管（ts 变化），自己不再处理附件
    assert results["first"]["deferred"] is True, "第一条应被第二条接管"
    # 第二条没等到文字，超时后由它按纯附件处理，手里必须是两份
    assert results["second"]["deferred"] is False, "第二条无后续文字应超时返回"
    names = [c.data["name"] for c in results["second"]["logs_in_hand"]]
    assert names == ["a.log", "b.log"], f"超时路径上先到的附件丢了: {names}"
    assert "chat1" not in app._pending_lone_images, "超时后暂存未清理"


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
