"""
日志不得记录完整用户消息 / 文件正文（防回归）

## 背景（真实现场）

用户私聊发了一份 3442 字符的 Java 异常堆栈（NullPointerException +
Connection refused），同一份原文在**一次请求**的日志里被完整打印至少 4 遍，
终端被刷满：
  1. `main.py` 的 `logger.debug("parsed message:{}", _parsed_log)` —— text 字段全文
  2. `main.py` 的 `logger.info(f"... text:{text} ...")` —— **INFO 级直接打全文**
  3. `feishu_doc_manager.get_documents_by_query` 的「关键词增强检索」—— 全文
  4. `feishu_doc_manager._filter_docs_by_ai` 的「AI 标题过滤输入」—— 全文

这不只是体验问题，是违反 CLAUDE.md 两条硬规范的缺陷：
  - 「数据隐私：日志中不记录完整消息内容（仅记录长度和摘要）」
  - 「禁止事项：不得在日志中记录完整的用户消息」

## 复现步骤（修复前）

1. 启动服务（日志级别默认 INFO 即可复现第 2 处，DEBUG 下 4 处全现）
2. 飞书私聊机器人，粘贴一份 3000+ 字符的多行异常堆栈并 @ 机器人
3. 观察终端 / `logs/*.log`：堆栈原文被整段打出多遍，真正的流程日志被淹没

## 本文件的测试策略

断言分两层，缺一不可：
  - **真实调用点**（test_process_event_*）：用桩驱动真的
    `AIAssistant._process_event`，复现线上时序，确认那两条日志里确实没有全文。
    只测工具函数会产生假绿灯 —— 工具对了但调用点没用上。
  - **工具自身语义**（test_summarize_*）：长度/行数/指纹/换行转义等不变量。

loguru 的输出 pytest 的 `caplog` 抓不到，必须自己 `logger.add(sink)`。
"""
import io
import threading

import pytest
from loguru import logger


# ============ 测试素材：复现现场那份 3442 字符的多行异常堆栈 ============

def _long_stacktrace(lines: int = 90) -> str:
    """
    构造一份多行长堆栈。行数与长度都要足够大，才能体现"刷屏"。

    **首行刻意做短（30 字符以内）**：这样任何"裸切片"式的假修复
    （`text[:50]` / `[:60]` / `[:80]`）都必然把首行的换行符一起带进日志，
    多行断言才真的有判别力。首行很长时裸切片切出来恰好是单行，测试会
    误判为通过 —— 这正是红灯验证时发现的一个弱探针。
    """
    head = "java.lang.NullPointerException\n"
    body = "\n".join(
        f"\tat com.example.order.OrderServiceImpl.handle(OrderServiceImpl.java:{100 + i})"
        for i in range(lines)
    )
    tail = "\nCaused by: java.net.ConnectException: Connection refused (Connection refused)"
    return head + body + tail


@pytest.fixture
def log_sink():
    """
    挂一个 loguru sink 收集日志文本。

    必须在被测代码执行**之前**挂、执行之后再读：构造函数自己也会打日志，
    sink 挂晚了会漏掉；而测试结束必须 remove，否则会污染后续用例。
    """
    buf = io.StringIO()
    sink_id = logger.add(buf, level="DEBUG", format="{message}")
    try:
        yield buf
    finally:
        logger.remove(sink_id)


# ============ 驱动真实 _process_event 的最小桩 ============

class _StubContextManager:
    def __init__(self):
        self.messages = {}

    def add_message(self, key, message):
        self.messages.setdefault(key, []).append(message)

    def get_context(self, key):
        return list(self.messages.get(key, []))

    def clear_context(self, key):
        self.messages.pop(key, None)


class _StopHere(Exception):
    """桩用的哨兵异常：让流程在调 AI 之前停下，不必拉起真的 provider"""


class _StubAIProvider:
    def call(self, *args, **kwargs):
        raise _StopHere("stub: stop before real AI call")


class _StubQueue:
    def qsize(self):
        return 0


class _StubAdapter:
    """
    刻意不提供 add_reaction / get_message：
    `_process_event` 用 hasattr 判断，缺失即跳过，正好让流程最短。
    """

    def process_webhook_event(self, raw_data):
        return {"event": {}}


def _make_app(parsed: dict):
    """
    造一个只具备 `_process_event` 所需状态的 AIAssistant 实例。

    用 `object.__new__` 绕过 `__init__`（真 __init__ 会读 config.yaml、连 SSH、
    建索引），但**被测方法本身是真的**，调用顺序与线上一致 —— 这是避免
    "stub 没复现真实调用点顺序导致假绿灯"的关键。
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
    app._lone_image_wait = 0  # 不进等待窗口，直接走主流程
    app.pending_retry_queue = []
    # 解析结果由测试直接给定：本测试关心的是"解析之后怎么打日志"，
    # 不关心飞书报文怎么解析，避免把适配器细节绑进来。
    app._parse_feishu_event = lambda processed_event, adapter: dict(parsed)
    app._send_feishu_reply = lambda *a, **kw: None
    return app


def _run_process_event(app):
    event = {
        "trace_id": "test-trace",
        "adapter": _StubAdapter(),
        "raw_data": {"encrypt": "x"},
    }
    # _process_event 内部已 try/except 兜住一切异常（包含哨兵），不会抛出来
    app._process_event(event)


# ============ 真实调用点断言 ============

def test_process_event_does_not_log_full_user_text(log_sink):
    """
    核心防回归：`_process_event` 的两条日志（INFO「Processing message」与
    DEBUG「parsed message」）都不得出现用户提问全文。

    红灯依据：恢复成 f"text:{text}" / 直接打 _parsed_log 时，
    全文会出现在 sink 里，下面三条 assert 会以 AssertionError 失败。
    """
    stack = _long_stacktrace()
    app = _make_app({
        "chat_id": "oc_test_chat",
        "text": stack,
        "message_id": "om_test_1",
        "sender_id": "ou_user_1",
        "root_id": "",
        "parent_id": "",
    })

    _run_process_event(app)
    out = log_sink.getvalue()

    # 1) 全文绝不能出现
    assert stack not in out, "日志中出现了用户提问全文，违反「不得记录完整用户消息」"

    # 2) 连堆栈正文的中段都不该出现（抽一行作为探针，防止"只截掉首尾"的假修复）
    probe = "at com.example.order.OrderServiceImpl.handle(OrderServiceImpl.java:160)"
    assert probe in stack
    assert probe not in out, "日志中出现了堆栈中段原文，截断不彻底"

    # 3) 必须仍留有排查信息：长度 + 本次这条消息的会话标识
    assert f"len={len(stack)}" in out, "摘要里没有长度，运维无法判断是否超长输入导致"
    assert "oc_test_chat" in out, "会话标识被一起截掉了，日志失去定位价值"


def test_process_event_log_volume_is_bounded(log_sink):
    """
    刷屏本身就是 bug：3442 字符的输入不该在日志里产生数千字符的输出。

    用「日志总量 / 输入长度」卡上限，比逐条匹配字符串更贴近"终端被刷满"
    这个真实症状，也不会因为日志措辞微调而误报。
    """
    stack = _long_stacktrace()
    app = _make_app({
        "chat_id": "oc_test_chat",
        "text": stack,
        "message_id": "om_test_2",
        "sender_id": "ou_user_1",
        "root_id": "",
        "parent_id": "",
    })

    _run_process_event(app)
    out = log_sink.getvalue()

    # 修复前：text 至少被打 2 遍（parsed + Processing），输出 > 2 倍输入。
    # 修复后：两处各一条摘要（约 150 字符），远低于输入长度。
    assert len(out) < len(stack), (
        f"日志输出({len(out)}字符)超过了用户输入({len(stack)}字符)，说明原文仍在被整段打印"
    )


def test_process_event_multiline_text_not_exploded_into_log_lines(log_sink):
    """
    多行文本（堆栈是典型）即使截断了，裸切片仍会把换行带进日志，
    一条日志炸成十几行，还会破坏下游按行采集。摘要必须把换行转义。

    红灯依据：把摘要换回 text[:80] 这类裸切片，日志行数会随堆栈行数暴涨。
    """
    stack = _long_stacktrace()
    app = _make_app({
        "chat_id": "oc_test_chat",
        "text": stack,
        "message_id": "om_test_3",
        "sender_id": "ou_user_1",
        "root_id": "",
        "parent_id": "",
    })

    _run_process_event(app)
    out = log_sink.getvalue()

    # 输入有 90+ 行；日志总行数应远小于它（每条日志自带一个结尾换行）
    assert out.count("\n") < 20, (
        f"日志被撑成 {out.count(chr(10))} 行，多行文本的换行没有被转义"
    )
    # 承载用户内容的那两条日志各自必须是**单行**：裸切片会把换行带进来，
    # 把一条日志劈成多行（行数断言在片段恰好不含换行时不敏感，故再卡这一条）
    lines = [l for l in out.split("\n") if l]
    for marker in ("Processing message for session", "parsed message:"):
        hit = [l for l in lines if marker in l]
        assert hit, f"没有找到日志行: {marker}"
        # 该 marker 只应出现在一行里；若换行泄漏，后续片段会落到不含 marker 的行上
        assert len(hit) == 1
        # 这一行之后紧跟的必须是下一条日志（以时间戳或已知 marker 开头），
        # 不能是堆栈续行
        idx = lines.index(hit[0])
        if idx + 1 < len(lines):
            assert not lines[idx + 1].lstrip().startswith("at com.example"), (
                f"「{marker}」之后出现了堆栈续行，说明换行未转义、日志被劈成多行"
            )


def test_process_event_does_not_log_attachment_file_body(log_sink):
    """
    日志文件附件正文同样不得落进日志（既有脱敏，一并锁住防回归）。

    用户上传一份 app.log，`parsed["log_contents"]` 里是 Content 对象，
    默认 repr 会把 data["text"] 整段打出来。
    """
    from ai_assistant.core.models import Content

    body = _long_stacktrace()
    log_content = Content(type="log_file", data={
        "name": "app.log",
        "path": "/tmp/app.log",
        "text": body,
        "note": "",
    })
    app = _make_app({
        "chat_id": "oc_test_chat",
        "text": "帮我看下这个日志",
        "message_id": "om_test_4",
        "sender_id": "ou_user_1",
        "root_id": "",
        "parent_id": "",
        "log_contents": [log_content],
    })

    _run_process_event(app)
    out = log_sink.getvalue()

    # 探针必须是**不含换行的单行片段**：Content 是 dataclass，它的 repr 会把
    # 内部字符串的换行渲染成 `\n` 字面量，于是带真换行的 `body` 不再是 out 的
    # 子串 —— 只断言 `body not in out` 会在正文确实泄漏时照样通过（红灯验证时
    # 实测到的假绿灯）。用一行堆栈帧作探针才真的能判别。
    probe = "at com.example.order.OrderServiceImpl.handle(OrderServiceImpl.java:160)"
    assert probe in body
    assert probe not in out, "日志文件正文被整段打进日志"
    assert body not in out
    # 文件名必须留着：排查时要知道是哪个附件
    assert "app.log" in out, "文件名被截掉了，无法定位是哪个附件"
    # 正文长度必须留着：判断是不是超大附件导致的问题
    assert f"text_len={len(body)}" in out or f"len={len(body)}" in out, (
        "附件正文长度丢失，无法判断是否超大附件"
    )


def test_process_event_does_not_log_image_base64(log_sink):
    """图片 base64 本体不得落进日志（既有脱敏，一并锁住防回归）"""
    b64 = "QUJDREVG" * 2000  # 16000 字符，模拟一张截图
    app = _make_app({
        "chat_id": "oc_test_chat",
        "text": "这张图什么问题",
        "message_id": "om_test_5",
        "sender_id": "ou_user_1",
        "root_id": "",
        "parent_id": "",
        "image_data": {"data": b64, "media_type": "image/png"},
    })

    _run_process_event(app)
    out = log_sink.getvalue()

    assert b64 not in out, "图片 base64 本体被打进日志"
    assert "QUJDREVGQUJDREVG" not in out, "图片 base64 片段被打进日志"


# ============ 检索链路的调用点断言 ============

def _import_doc_manager():
    """
    导入 FeishuDocManager。

    它在模块级 import hybrid_search，后者又在模块级 `import jieba`，而本仓库
    的测试环境没装 jieba（基线里就有因此而来的 collection error）。这里给
    sys.modules 塞一个 jieba 占位模块再导入：被测的两条日志语句完全不碰分词，
    这样做能拿到**真实调用点**的覆盖，而不是 skip 掉事了 —— skip 等于没防住。
    占位只在缺真包时注入，装了 jieba 的环境走真包。
    """
    import sys
    import types

    if "jieba" not in sys.modules:
        try:
            import jieba  # noqa: F401
        except ImportError:
            stub = types.ModuleType("jieba")
            stub.cut = lambda *a, **kw: iter(())
            stub.lcut = lambda *a, **kw: []
            sys.modules["jieba"] = stub

    from ai_assistant.core.feishu_doc_manager import FeishuDocManager
    return FeishuDocManager


def test_doc_manager_ai_title_filter_does_not_log_full_query(log_sink):
    """
    `feishu_doc_manager._filter_docs_by_ai` 的「AI 标题过滤输入」不得打全文。
    这是同一份原文在一次请求里的第 4 遍打印。

    候选文档标题必须保留：它是判断"AI 过滤有没有把对的文档筛掉"的唯一依据，
    不能连它一起截掉。
    """
    FeishuDocManager = _import_doc_manager()

    query = _long_stacktrace()
    mgr = object.__new__(FeishuDocManager)
    mgr._ai_provider = type("P", (), {
        "filter_docs_by_relevance": staticmethod(lambda *a, **kw: [])
    })()
    # 候选项打印的是 path（实现里 path 优先于 title），这里按实现给素材
    mgr._filter_docs_by_ai(query, [{"title": "订单服务部署手册", "path": "运维/订单服务部署手册"}])

    out = log_sink.getvalue()
    assert query not in out, "「AI 标题过滤输入」仍在打用户提问全文"
    probe = "at com.example.order.OrderServiceImpl.handle(OrderServiceImpl.java:160)"
    assert probe not in out, "堆栈中段原文仍被打出，截断不彻底"
    assert "运维/订单服务部署手册" in out, "候选文档路径被截掉了，无法判断 AI 过滤是否筛错"


def test_doc_manager_keyword_extraction_does_not_log_full_query(log_sink):
    """
    `_extract_keywords` 降级分支的「规则关键词提取」不得打全文。

    原实现是 `query_text[:50]` —— 长度上已截断，但裸切片会把堆栈换行一起
    带进日志。这里断言该条日志自身是单行。
    """
    FeishuDocManager = _import_doc_manager()

    query = _long_stacktrace()
    mgr = object.__new__(FeishuDocManager)
    mgr._ai_provider = None
    mgr._keyword_extractor = None
    mgr._extract_keywords(query)

    out = log_sink.getvalue()
    assert query not in out
    hit = [l for l in out.split("\n") if "规则关键词提取" in l]
    assert hit, "没有找到「规则关键词提取」日志行"
    assert len(hit) == 1
    idx = out.split("\n").index(hit[0])
    rest = out.split("\n")[idx + 1:]
    assert not any(l.lstrip().startswith("at com.example") for l in rest), (
        "「规则关键词提取」把堆栈换行带进了日志，被劈成多行"
    )


def test_hybrid_search_does_not_log_full_query(log_sink):
    """
    `hybrid_search.search` 的「混合检索」日志不得打用户提问全文。

    原实现是 `query[:50]`（长度已截断，是清单里的"正确范例"），但同样是裸切片，
    多行堆栈的换行会被带进日志。这里连检索结果一起断言：Top5 标题与分数必须
    保留，它们是判断"检索有没有找错文档"的依据，不能跟着一起截掉。
    """
    _import_doc_manager()  # 复用 jieba 占位注入
    from ai_assistant.core.hybrid_search import HybridSearchEngine

    query = _long_stacktrace()
    eng = object.__new__(HybridSearchEngine)
    eng._bm25_docs = [{"title": "订单服务部署手册"}]
    doc = {"title": "订单服务部署手册", "path": "运维/订单", "score": 1.0}
    eng._vector_search = lambda q, top_k: [doc]
    eng._bm25_search = lambda q, top_k: [doc]
    eng._rrf_fusion = lambda v, b, k=60, bm25_weight=1.0: [doc]

    eng.search(query, top_k=3)

    out = log_sink.getvalue()
    assert query not in out
    probe = "at com.example.order.OrderServiceImpl.handle(OrderServiceImpl.java:160)"
    assert probe not in out, "「混合检索」仍在打堆栈原文"
    assert "订单服务部署手册" in out, "检索命中的文档标题被截掉了，无法判断是否检索错"
    # query 片段必须被压在首行里（该日志本身有 Top5 等多行结构，首行之后应当
    # 直接是「向量Top5」，而不是堆栈续行）。注意首行里出现转义后的
    # `\n\tat com.example...` 字面量是**正确**结果——那说明换行被转义住了；
    # 判别点是真换行有没有把这一行劈开。
    lines = out.split("\n")
    assert "混合检索" in lines[0]
    assert lines[1].lstrip().startswith("向量Top5"), (
        f"「混合检索」首行之后不是 向量Top5 而是 {lines[1]!r}，说明 query 的真换行把日志劈开了"
    )


def test_log_subcall_degraded_truncates_even_if_caller_passes_full_query(log_sink):
    """
    `log_subcall_degraded` 不能依赖调用方自觉截断。

    现状各 Provider 都传 `query_text[:50]`，但签名不强制；`ai_provider.py`
    的告警上报处就曾直接传完整 `last_user_text`。本函数必须自己兜一层。
    """
    from ai_assistant.core.ai_provider import log_subcall_degraded

    query = _long_stacktrace()
    log_subcall_degraded(
        "keyword_extraction", "JSON 解析失败", raw="not a json", query_preview=query
    )

    out = log_sink.getvalue()
    assert query not in out, "调用方传了完整提问时，log_subcall_degraded 把它原样打出来了"
    assert "keyword_extraction" in out, "子调用标识丢失，无法判断是哪个环节降级"


# ============ 摘要工具自身的语义不变量 ============

def test_summarize_text_keeps_length_and_fingerprint():
    """摘要必须带长度、行数、指纹 —— 这是「只打长度不够用」的折中结论"""
    from ai_assistant.utils.log_summary import summarize_text, fingerprint

    stack = _long_stacktrace()
    s = summarize_text(stack)

    assert stack not in s
    assert f"len={len(stack)}" in s
    assert f"lines={stack.count(chr(10)) + 1}" in s
    # 指纹让同一份内容在多处日志之间可对账（这是"打 4 遍"的收口手段）
    assert f"sha1={fingerprint(stack)}" in s
    # 同一内容两次摘要必须一致，不同内容必须不同
    assert summarize_text(stack) == s
    assert fingerprint(stack) != fingerprint(stack + "x")


def test_summarize_text_single_lines_the_preview():
    """
    片段必须单行化：换行/制表符转义成可见字面量，且**只转义一次**。

    双重转义（手写 replace 之后又套 `!r`）同样能通过"无真换行"的断言，
    但日志里会出现 `\\\\n` 噪声 —— 实测踩到过，所以这里精确断言转义结果。
    """
    from ai_assistant.utils.log_summary import summarize_text

    s = summarize_text("line1\nline2\tend")
    assert "\n" not in s and "\t" not in s
    assert "\\n" in s and "\\t" in s
    assert "\\\\n" not in s, "换行被双重转义，日志里会出现 \\\\n 噪声"

    # 长内容（走首尾片段分支）同样不许双重转义
    long_s = summarize_text("head\n" + "x" * 200 + "\ntail")
    assert "\n" not in long_s
    assert "\\\\n" not in long_s, "长内容分支的换行被双重转义"


def test_summarize_text_short_content_is_readable():
    """短内容要能直接读，否则排查体验反而变差"""
    from ai_assistant.utils.log_summary import summarize_text

    s = summarize_text("订单服务起不来")
    assert "订单服务起不来" in s
    assert "len=7" in s
    # 短内容不补尾部片段，也不需要指纹
    assert "sha1=" not in s


def test_summarize_text_long_content_keeps_head_and_tail():
    """超长内容保留首尾：尾部的 Caused by 往往才是根因"""
    from ai_assistant.utils.log_summary import summarize_text

    stack = _long_stacktrace()
    s = summarize_text(stack)
    assert "NullPointerException" in s, "首部片段丢失，无法人眼辨识是哪条消息"
    assert "Connection refused" in s, "尾部片段丢失，Caused by 这类根因看不到"
    assert len(s) < 300, f"单条摘要 {len(s)} 字符，仍然偏长"


def test_summarize_text_never_raises_on_weird_input():
    """日志摘要失败不能影响主流程（webhook 必须 3 秒内返回）"""
    from ai_assistant.utils.log_summary import summarize_text

    class _Boom:
        def __str__(self):
            raise RuntimeError("boom")

    assert summarize_text(None) == "text(empty)"
    assert summarize_text("") == "text(empty)"
    assert "summary-failed" in summarize_text(_Boom())
    assert "len=" in summarize_text(12345)  # 非字符串容错


def test_summarize_obj_summarizes_nested_long_strings():
    """结构化对象里的长字符串必须被压掉，短标识字段保持可读"""
    from ai_assistant.utils.log_summary import summarize_obj

    stack = _long_stacktrace()
    out = summarize_obj({
        "chat_id": "oc_abc",
        "text": stack,
        "nested": {"body": stack},
        "flag": True,
        "count": 3,
    })
    rendered = repr(out)

    assert stack not in rendered
    assert "oc_abc" in rendered, "短标识字段被套了摘要前缀，结构日志变得没法读"
    assert out["flag"] is True and out["count"] == 3, "标量被改写了"
    assert f"len={len(stack)}" in rendered
    assert f"len={len(stack)}" in repr(out["nested"]), "嵌套层的长文本没被摘要"


def test_summarize_obj_uses_model_summary_and_hides_bytes():
    """带 summary() 的模型对象复用自己的摘要；bytes 只打长度不解码"""
    from ai_assistant.core.models import Content
    from ai_assistant.utils.log_summary import summarize_obj

    body = _long_stacktrace()
    out = summarize_obj({
        "log_contents": [Content(type="log_file", data={"name": "app.log", "text": body})],
        "blob": b"\x00\x01binary-secret",
    })
    rendered = repr(out)

    assert body not in rendered
    assert "app.log" in rendered, "没有复用 Content.summary()，文件名丢失"
    assert "binary-secret" not in rendered, "bytes 被解码打印"
    assert "bytes(len=" in rendered


def test_summarize_obj_folds_huge_collections():
    """超长列表/超多键必须折叠，否则一行日志仍会被撑爆"""
    from ai_assistant.utils.log_summary import summarize_obj

    out = summarize_obj(list(range(500)))
    assert len(out) <= 13, f"500 项列表没被折叠，实际 {len(out)} 项"
    assert "more" in repr(out[-1])

    deep = {"a": {"b": {"c": {"d": {"e": {"f": "too deep"}}}}}}
    assert "depth-limit" in repr(summarize_obj(deep))


def test_summarize_obj_rejects_limit_so_it_cannot_be_widened():
    """
    summarize_obj 必须**不接** limit 参数。

    这不是风格问题，是一个从调用点看不出来的泄漏口。summarize_text 的 limit
    同时抬高了"整段显示"的门槛（传 limit=600 时，600 字符以内的内容头部片段
    就是全文），而 summarize_obj 面对的是结构化 payload —— 哪个字段承载用户
    内容事先并不知道。一旦它收下 limit 并沿递归向下继承，
    `summarize_obj(payload, limit=600)` 会把整棵树里每一个 600 字符以内的
    字符串原样打出去。所以这个旋钮在结构化入口上直接不提供。

    红灯验证（给 summarize_obj 加回 `limit` 并向下透传）时，下面第一条
    `TypeError` 断言会失败。
    """
    import inspect
    import pytest as _pytest
    from ai_assistant.utils.log_summary import summarize_obj, PREVIEW_HEAD_CHARS

    assert "limit" not in inspect.signature(summarize_obj).parameters, (
        "summarize_obj 又接了 limit 参数，排查旋钮会变成泄漏开关"
    )
    with _pytest.raises(TypeError):
        summarize_obj({"text": "x"}, limit=600)

    # 300 字符单行用户提问：超过透传窗口(80)，但低于别处常见的 limit(600)。
    # 无论调用点怎么写，它都必须被摘要。
    single_line_question = "订单服务连不上数据库，" * 30
    assert len(single_line_question) > PREVIEW_HEAD_CHARS
    assert len(single_line_question) < 600

    rendered = repr(summarize_obj({"text": single_line_question}))
    assert single_line_question not in rendered, "300 字符的用户提问被原样打出"
    # 仍然要是可排查的摘要，不是光秃秃的长度
    assert "len=330" in rendered
    assert "sha1=" in rendered


def test_summarize_obj_set_fingerprint_is_order_stable():
    """
    set 必须先排序再展开。

    set 的迭代顺序受 str hash 随机化影响，同一份内容跨进程会得到不同顺序，
    摘要字符串随之不同 —— 于是"同一份内容"算出不同指纹，而内容指纹的全部价值
    就在于能跨日志条目比对。这里在**单进程内**用多个等值 set 复现顺序差异
    （不同插入顺序构造的 set，迭代顺序可以不同）。
    """
    from ai_assistant.utils.log_summary import summarize_obj

    # 必须用**多个子进程 + 不同 PYTHONHASHSEED** 来复现，不能在单进程内
    # 换插入顺序：同一批短字符串在同一进程里的 set 迭代顺序由 hash 决定，
    # 与插入顺序无关，所以"换顺序构造再比对"根本测不出东西（实测是假绿灯，
    # 把排序改回去照样通过）。真正的不稳定来自 str hash 的**进程级**随机化。
    import os
    import subprocess
    import sys

    repo_src = os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
        "src",
    )
    snippet = (
        "from ai_assistant.utils.log_summary import summarize_obj\n"
        "members = [f'doc-{i}' for i in range(12)]\n"
        "print(repr(summarize_obj({'docs': set(members)})))\n"
    )

    renders = set()
    raw_orders = set()
    for seed in ("1", "7", "12345", "99991"):
        env = dict(os.environ)
        env["PYTHONHASHSEED"] = seed
        env["PYTHONPATH"] = repo_src
        out = subprocess.run(
            [sys.executable, "-c", snippet],
            capture_output=True,
            text=True,
            env=env,
            timeout=60,
        )
        assert out.returncode == 0, f"子进程失败: {out.stderr}"
        renders.add(out.stdout.strip())

        # 同时确认这些种子**确实**让裸 set 的迭代顺序发生了变化，
        # 否则本测试只是在比对一堆恒等输出（又一种假绿灯）。
        probe = subprocess.run(
            [sys.executable, "-c", "print(list({f'doc-{i}' for i in range(12)}))"],
            capture_output=True,
            text=True,
            env=env,
            timeout=60,
        )
        raw_orders.add(probe.stdout.strip())

    assert len(raw_orders) > 1, (
        "这些 PYTHONHASHSEED 没能让 set 顺序发生变化，本测试失去判别力"
    )
    assert len(renders) == 1, (
        f"等值 set 的摘要跨进程出现 {len(renders)} 种结果，指纹不可比对: {renders}"
    )

    # 不可比类型混在一个 set 里也不能炸（排序 key 用 repr 而非值本身）
    mixed = summarize_obj({"mixed": {1, "a", None}})
    assert "summary-failed" not in repr(mixed)


def test_head_len_rule_has_single_implementation():
    """
    `limit → 头部片段长度` 的归一规则必须只有一处实现。

    之前 summarize_text 和 summarize_obj 各写了一遍
    `PREVIEW_HEAD_CHARS if limit is None else max(1, int(limit))`，
    同一规则落在两处，改一处另一处就漂 —— 而会漂掉的那一处正好是防泄漏逻辑。
    这里断言两个入口对同一 limit 的行为一致，任何一处私自改规则都会红灯。
    """
    from ai_assistant.utils.log_summary import (
        _head_len,
        PREVIEW_HEAD_CHARS,
        summarize_text,
        summarize_obj,
    )

    assert _head_len(None) == PREVIEW_HEAD_CHARS
    assert _head_len(600) == 600
    # limit=0 / 负数会让切片变成空串或反向取值，摘要失去辨识度，下限锁 1
    assert _head_len(0) == 1
    assert _head_len(-5) == 1

    # 上面只验了 helper 本身。还必须验 summarize_text **确实在用它** ——
    # 否则 summarize_text 里私自写回 `max(0, int(limit))` 这类变体，
    # helper 的断言照样全绿（实测过的假绿灯）。做法是从摘要里把头部片段的
    # 真实长度量出来，和 _head_len 的返回值对齐。
    probe = "z" * 2000
    for lim in (None, 1, 0, -5, 37, 600):
        s = summarize_text(probe, limit=lim)
        expected = _head_len(lim)
        # 形如 text(len=2000,lines=1,sha1=xxxxxxxx):'zzz…'…'zzz'
        head_repr = s.split(":", 1)[1].split("…")[0]
        measured = len(head_repr) - 2  # 去掉 repr 的两个引号
        assert measured == expected, (
            f"limit={lim}: summarize_text 实际头部片段 {measured} 字符，"
            f"_head_len 说应该是 {expected} —— 规则漂了"
        )

    # 两个入口对"长内容"的处理必须完全一致：summarize_obj 对长字符串
    # 就是转调默认阈值的 summarize_text，这条断言防止它将来另写一套切法。
    long_text = "y" * 500
    assert summarize_obj(long_text) == summarize_text(long_text), (
        "summarize_obj 对长字符串不再走 summarize_text 了"
    )


# ===================== webhook Raw payload：凭据 + 用户内容 =====================
#
# 这组测试针对的不是刷屏，而是**凭据泄漏**，比隐私更严重：
#
# 原实现 `logger.debug(f"📨 Raw payload(截断): {str(data)[:500]}")` 在
# **明文模式**（只配 verification_token、未配 encrypt_key）下，前 500 字符里
# 既有用户消息正文，也有真实的 verification_token —— str() 把两者一起打出去。
# 配了 encrypt_key 时 _authenticate_feishu_payload 直接拒明文，日志里只有密文，
# 所以这个泄漏只在明文模式暴露，也正因如此容易被忽略。
#
# 关键结论（实测得出，不是预防性设计）：**换成 summarize_obj 本身挡不住 token**。
# verification_token 是 32 字符单行字符串，正好落在「短且单行 → 原样透传」那一侧，
# 实测 `summarize_obj({"token": "<32字符>"})` 会把原值打出来。所以必须另加按
# **字段名**遮蔽的 SECRET_KEY_HINTS 机制 —— 隐私靠长度判定，凭据只能靠字段名判定。


_FAKE_TOKEN = "v9kTd2BxQ1mNpL7sYhRzCgWfXaEuJ3iO"  # 32 字符，典型 verification_token 形态


class _PlaintextAdapter:
    """明文模式适配器：只配 verification_token，不配 encrypt_key。

    这是复现本 bug 的**必要条件** —— 配了 encrypt_key 时鉴权会直接拒明文报文，
    日志里只剩密文，测试就测不到泄漏了。
    """

    encrypt_key = ""
    verification_token = _FAKE_TOKEN


def _make_webhook_client():
    from ai_assistant.webhook_server import WebhookServer

    server = WebhookServer(host="127.0.0.1", port=0, web_frontend_enabled=False)
    server.feishu_adapter = _PlaintextAdapter()
    server.event_queue = __import__("queue").Queue()
    return server, server.app.test_client()


def _plaintext_payload(user_text: str) -> dict:
    """明文模式报文：顶层带真实 token，event 里带用户正文。"""
    return {
        "token": _FAKE_TOKEN,
        "type": "event_callback",
        "event": {
            "type": "message",
            "message": {
                "chat_id": "oc_abc123",
                "message_id": "om_x1",
                "message_type": "text",
                "content": '{"text":"' + user_text + '"}',
            },
            "sender": {"sender_id": {"open_id": "ou_abc123"}},
        },
    }


def test_webhook_raw_payload_does_not_log_verification_token(log_sink):
    """
    /webhook/feishu 的 Raw payload 日志**不得出现 verification_token 原值**。

    这是凭据不是隐私：token 泄漏意味着任何读到日志的人都能伪造通过鉴权的报文。
    断言对象是 token **原值**，不是长度 —— `<redacted:len=32>` 是允许的。
    """
    _server, client = _make_webhook_client()
    resp = client.post("/webhook/feishu", json=_plaintext_payload("订单服务起不来"))

    out = log_sink.getvalue()
    assert _FAKE_TOKEN not in out, "verification_token 原值被打进日志（凭据泄漏）"
    # 确认报文确实走到了打日志那一行（否则是"没执行到"的假绿灯）
    assert "Raw payload" in out, f"没走到 Raw payload 日志，响应={resp.status_code}"
    # 仍要留下可排查信息：遮蔽后至少能看出"配了一个 32 字符的 token"
    assert "redacted" in out


def test_webhook_raw_payload_does_not_log_full_user_text(log_sink):
    """同一行日志里，用户消息正文也不得原样出现（隐私，靠长度摘要）。"""
    _server, client = _make_webhook_client()
    stack = _long_stacktrace()
    client.post("/webhook/feishu", json=_plaintext_payload(stack.replace("\n", " ")))

    out = log_sink.getvalue()
    assert "Raw payload" in out
    # 探针用**不含换行的单行片段**：payload 经 json 序列化后换行形态会变，
    # 直接拿原文做 not in 判断会因转义差异漏判（已踩过的假绿灯）。
    probe = "at com.example.order.OrderServiceImpl.handle(OrderServiceImpl.java:160)"
    assert probe not in out, "用户提问正文被原样打进日志"
    assert "sha1=" in out, "没有留下可比对的内容指纹"


def test_webhook_card_raw_payload_redacts_token_too(log_sink):
    """
    /webhook/feishu/card 必须和 /webhook/feishu 用同一套摘要。

    防回归要点：这两个端点的鉴权绕过当初就是「只修了一个」造成的
    （见 webhook_server.py 里 eddce98 的注释）。日志泄漏同理，只改一处
    会把凭据泄漏留在卡片端点上。
    """
    _server, client = _make_webhook_client()
    client.post(
        "/webhook/feishu/card",
        json={
            "token": _FAKE_TOKEN,
            "type": "event_callback",
            "event": {"type": "card.action.trigger", "operator": {"open_id": "ou_abc"}},
        },
    )

    out = log_sink.getvalue()
    assert "Card raw payload" in out, "没走到卡片端点的 Raw payload 日志"
    assert _FAKE_TOKEN not in out, "卡片端点仍泄漏 verification_token"


def test_secret_fields_redacted_by_name_not_by_length():
    """
    凭据必须按**字段名**遮蔽，不能靠值的长度判定。

    这条是本轮的核心发现：token / app_secret 都是短的单行字符串，会走
    summarize_obj 的「原样透传」分支 —— 单靠长度摘要一定漏。红灯验证时
    把 dict 分支的 _is_secret_key 判断去掉，这条会以 AssertionError 失败。
    """
    from ai_assistant.utils.log_summary import summarize_obj, redact_secret

    secrets = {
        "token": _FAKE_TOKEN,
        "app_secret": "s3cr3t-app-secret-value",
        "Authorization": "Bearer abcdefghijklmnop",
        "encrypt_key": "k" * 16,
        "api_key": "sk-proj-0123456789",
    }
    rendered = repr(summarize_obj(dict(secrets, chat_id="oc_abc123")))

    for key, value in secrets.items():
        assert value not in rendered, f"{key} 的原值被打进日志（凭据泄漏）"
    # 非凭据的短标识仍须原样可读，否则排查成本陡增
    assert "oc_abc123" in rendered

    # 遮蔽结果本身不得带原文片段，也不得带指纹：
    # 片段等于部分泄漏；sha1 对固定凭据等于一个稳定的可比对标识。
    red = redact_secret(_FAKE_TOKEN)
    assert red == "<redacted:len=32>"
    assert "sha1" not in red
    assert _FAKE_TOKEN[:4] not in red


# ============ P0：凭据字段名的匹配方式（归一化，而非补名单） ============
#
# 复现（修复前实测）：
#   summarize_obj({'password': 'SENTINEL'})        → <redacted:len=8>   挡住了
#   summarize_obj({'key_passphrase': 'SENTINEL'})  → 'SENTINEL'         漏了
# 同一个 SSHConfig（core/operations_models.py:28）里 password 遮了、
# key_passphrase 没遮 —— "邻居遮了我没遮"比整体漏更难发现。
#
# 另一类漏法是分隔符：名单里有 `api_key`，真实 HTTP 头是 `X-Api-Key`。
# 所以根因在**匹配方式**不在名单长度 —— 靠补 apikey/api-key/apiKey 永远
# 追不上下一种拼法。修法是键名先归一（小写 + 驼峰拆分 + 非字母数字转下划线）
# 再比子串。下面第一组断言覆盖各种拼法，第二组守住"不许遮过头"。


def test_secret_key_matching_covers_all_spellings():
    """
    凭据字段名的各种拼法都必须被遮蔽：下划线 / 连字符 / 驼峰 / 点 / 空格。

    红灯验证：把 `_is_secret_key` 改回 `str(key).lower()` 直接比子串
    （即去掉归一化），`X-Api-Key` / `api-key` / `APIKey` 这几条会
    AssertionError；若同时去掉新增 hints，`key_passphrase` 等也会红。
    """
    from ai_assistant.utils.log_summary import summarize_obj

    sentinel = "SENTINEL-VALUE-XYZ"
    must_redact = [
        # P0 报告里点名的漏网
        "key_passphrase",   # SSHConfig 实际字段名
        "passphrase",
        "private_key",
        "privateKey",       # 驼峰
        "cookie",
        "jwt",
        "bearer",
        "pwd",
        # 分隔符形态：真实 HTTP 头
        "X-Api-Key",
        "api-key",
        "apiKey",
        "APIKey",           # 连续大写接小写
        "my.secret.value",  # 点分隔
        "ACCESS KEY",       # 空格分隔 + 全大写
        "X-Signature",
        # 无分隔符连写（归一化拆不开，必须靠去分隔符匹配兜）
        # 回归来源：修 X-Api-Key 时删掉 hints 里的 "apikey"/"accesskey"，
        # 误以为归一化能接管，但归一化只拆分隔符和驼峰 —— 这 6 条从
        # 「遮蔽」变成了「泄漏」，比原来的漏网更糟（删 hint 时留下的注释
        # 还会主动劝阻后人补回来）。
        "apikey",
        "APIKEY",
        "accesskey",
        "ACCESSKEY",
        "privatekey",
        "encryptkey",
        # 原有的不能回退
        "token",
        "verification_token",
        "app_secret",
        "password",
        "encrypt_key",
        "Authorization",
        "access_key",
        "credential",
    ]
    for key in must_redact:
        rendered = repr(summarize_obj({key: sentinel}))
        assert sentinel not in rendered, f"{key} 的值被原样打进日志（凭据泄漏）"
        assert "redacted" in rendered, f"{key} 没走遮蔽分支"


def test_secret_key_matching_does_not_over_redact_identifiers():
    """
    归一化不许改过头：排查标识必须保持可读。

    这条和上一条是一对 —— 只测"遮住了"会让人倾向于把名单写得越来越宽，
    最后把 app_id / chat_id 也遮掉，日志就失去定位能力（"哪个用户哪个
    会话出的问题"再也看不出来）。

    特别是 `sign` 这个子串：评审建议过加它，但它会误伤
    design / assign / signal / assignee / designer。真正要挡的签名字段
    已由 `signature` 覆盖，所以 `sign` 刻意没有加入。
    `session_id` 同理 —— 在本项目里它就是 chat_id
    （feishu_bot.py:1203 `session_id=chat_id`），遮它纯属自伤。
    """
    from ai_assistant.utils.log_summary import NEVER_SECRET_KEYS, summarize_obj

    ident = "oc_abc123"
    # 白名单**直接 import**，不在这里手抄一份：手抄的两份清单必然漂移
    # （往 NEVER_SECRET_KEYS 加了字段但忘了同步测试，这条测试就守不住它）。
    # 下面另外一组是「不在白名单、也不该被命中」的普通字段 —— 它们靠的是
    # 「没有哪条 hint 会命中」，不是靠白名单，所以必须单独列、不能混进去。
    not_whitelisted_but_safe = [
        # 含 "sign" 子串：`sign` 刻意没有加入 hints，否则这些全被误伤
        "design",
        "assign",
        "signal",
        "assignee",
        "designer",
        "design_doc",
        # 去分隔符匹配放宽了匹配面，这几个普通字段用来守住误伤边界
        "host",
        "username",
        "key_path",
        "content",
        "file_path",
        "duration",
    ]
    for key in list(NEVER_SECRET_KEYS) + not_whitelisted_but_safe:
        rendered = repr(summarize_obj({key: ident}))
        assert ident in rendered, f"{key} 被误遮，日志失去定位能力"
        assert "redacted" not in rendered, f"{key} 走了遮蔽分支"


def test_normalize_key_semantics():
    """键名归一化本身的语义（驼峰拆分 / 分隔符统一 / 转小写）。"""
    from ai_assistant.utils.log_summary import _normalize_key

    assert _normalize_key("X-Api-Key") == "x_api_key"
    assert _normalize_key("apiKey") == "api_key"
    assert _normalize_key("APIKey") == "api_key"
    assert _normalize_key("api.key") == "api_key"
    assert _normalize_key("ACCESS KEY") == "access_key"
    assert _normalize_key("already_snake") == "already_snake"
    # 容错：非字符串键不能把摘要搞崩
    assert _normalize_key(123) == "123"
    assert _normalize_key(None) == "none"
    # 归一化**不**负责拆连写：这是分工边界，写进测试免得后人再误判一次
    assert _normalize_key("apikey") == "apikey"
    assert _normalize_key("APIKEY") == "apikey"


def test_whitelist_must_stay_exact_match_not_substring():
    """
    `NEVER_SECRET_KEYS` 必须保持**精确匹配**，不许放宽成任何部分匹配。

    为什么这条反直觉、而且必须有测试守着：白名单用精确匹配，hints 侧用
    归一化 + 去分隔符子串，**两边粒度不对称**，看起来像个待修的不一致。
    白名单确实也窄（hints 里万一加了宽子串，它只救得下恰好等于白名单词的
    拼法，`X-App-Id` → `x_app_id` 就救不下来）。所以"把白名单也改成部分
    匹配，和 hints 对齐"是个很容易冒出来的念头。

    但「白名单先行」和「精确匹配」是一对性质，不能只留一半 —— 白名单命中
    就直接 return，hints 根本没机会跑。一旦放宽成部分匹配（子串 / 前缀 /
    后缀 / 按下划线的词边界都一样），任何**含凭据词、但末尾长得像标识**的
    键名就绕过全部 hints。而 `xxx_user_id` / `xxx_app_id` 这种命名在真实
    代码里极常见，不是构造出来的边角反例。

    本测试的做法：把白名单换成"按词边界的后缀匹配"（评审提过的对齐改法），
    断言下面这些键从「遮蔽」翻成「可读」—— 即证明放宽会泄漏凭据。
    注意它验的是**放宽之后会坏**，不是只验当前实现是绿的（后者挡不住
    下一个人去改）。
    """
    import ai_assistant.utils.log_summary as mod

    leaky_keys = (
        "secret_app_id",
        "password_user_id",
        "api_key_app_id",
        "access_token_user_id",
        "token_app_id",
    )

    # 前提：当前实现下这些键都被正确识别为凭据
    for key in leaky_keys:
        assert mod._is_secret_key(key), f"{key} 在当前实现下就没被识别为凭据"

    original = mod._matched_secret_hints

    def suffix_word_whitelist(key):
        """评审建议的"对齐粒度"改法：白名单改成按词边界的后缀匹配。"""
        normalized = mod._normalize_key(key)
        if not normalized:
            return ()
        for allowed in mod.NEVER_SECRET_KEYS:
            if normalized == allowed or normalized.endswith("_" + allowed):
                return ()
        squashed = normalized.replace("_", "")
        return tuple(
            hint
            for hint in mod.SECRET_KEY_HINTS
            if hint in normalized or hint.replace("_", "") in squashed
        )

    try:
        mod._matched_secret_hints = suffix_word_whitelist
        leaked = [key for key in leaky_keys if not mod._is_secret_key(key)]
        assert leaked == list(leaky_keys), (
            "放宽白名单为后缀词匹配后，这些含凭据词的键本应全部泄漏，"
            f"实际只泄漏了 {leaked} —— 说明本测试没能复现放宽的后果，失去判别力"
        )
        # 真正的危害：值被原样打进日志
        sentinel = "SENTINEL-VALUE-XYZ"
        rendered = repr(mod.summarize_obj({"secret_app_id": sentinel}))
        assert sentinel in rendered, "反例构造失败：放宽后 secret_app_id 的值仍未泄漏"
    finally:
        mod._matched_secret_hints = original

    # 还原后必须回到遮蔽状态，确认本测试没有污染后续用例
    assert mod._matched_secret_hints is original
    for key in leaky_keys:
        assert mod._is_secret_key(key), f"{key} 在还原后仍未被识别为凭据（测试污染了模块状态）"


def test_squashed_matching_is_what_covers_run_together_spellings():
    """
    连写拼法是靠**去分隔符匹配**覆盖的，不是靠归一化，也不是靠往名单补变体。

    这条测试锁的是「分工」而不只是结果：上一条测试确认归一化拆不开
    `apikey`，这条确认 hints 里只有下划线形态时连写仍被命中。两条合起来
    说明：删掉 hints 里的 `apikey`/`accesskey` 是安全的，前提是
    `_matched_secret_hints` 里的 squashed 比对在。

    红灯验证：去掉 `_matched_secret_hints` 里 `hint.replace("_","") in squashed`
    这半边条件，下面 6 条会 AssertionError（这正是本轮回归的形态）。
    """
    from ai_assistant.utils.log_summary import SECRET_KEY_HINTS, _is_secret_key

    # 前提：名单里只有下划线形态，没有连写变体。若后人把变体补回来，
    # 这条断言会提醒他「该修的是匹配方式」。
    for variant in ("apikey", "accesskey", "privatekey", "encryptkey"):
        assert variant not in SECRET_KEY_HINTS, (
            f"{variant} 被补进了 hints —— 连写应由去分隔符匹配覆盖，不要补变体"
        )

    for key in ("apikey", "APIKEY", "accesskey", "ACCESSKEY", "privatekey", "encryptkey"):
        assert _is_secret_key(key), f"{key} 没被命中：去分隔符匹配失效"


def test_numeric_counter_fields_stay_readable_but_numeric_secrets_do_not():
    """
    `token` 作为裸子串会命中 `max_tokens` / `input_tokens` 这类计数字段，
    打成 `max_tokens: '<redacted:len=4>'` 既荒谬又有害 ——
    anthropic_provider.py 里 input_tokens 是刻意打出来排查兼容网关的。

    但「纯数值一律放过」是**错的**：实测 YAML 里不加引号的纯数字密码
    会被解析成 int（`password: 1234567` → `type=int`），一律放过等于给
    纯数字密码开后门。所以判据挂在 hint 词汇上（NUMERIC_OK_HINTS），
    只有本身有「数量」含义的 hint 才允许数值透传。

    红灯验证：把 `_should_redact` 改成 `_is_plain_number(value)` 就放过
    （不检查 hint 归属），`password: 1234567` 那条会 AssertionError。
    """
    from ai_assistant.utils.log_summary import summarize_obj

    # 计数字段：数值必须可读
    for key in ("max_tokens", "input_tokens", "output_tokens", "token_count", "total_tokens"):
        rendered = summarize_obj({key: 4096})
        assert rendered == {key: 4096}, f"{key} 的计数值被遮了：{rendered}"

    # 同名字段但值是字符串 → 仍然是凭据
    rendered = summarize_obj({"access_token": "tok_abcdef123456"})
    assert "tok_abcdef123456" not in repr(rendered)
    assert "redacted" in repr(rendered)

    # 纯数值的真凭据 → 必须仍然遮（YAML 纯数字密码）
    for key, value in (
        ("password", 1234567),
        ("key_passphrase", 998877),
        ("api_key", 12345),
        ("app_secret", 24680),
        # 同时命中 token 和 password：只要有一个 hint 不属于 NUMERIC_OK_HINTS
        # 就必须遮（`_should_redact` 里用 all() 而非 any() 的原因）
        ("token_password", 1234567),
    ):
        rendered = repr(summarize_obj({key: value}))
        assert str(value) not in rendered, f"{key}={value} 纯数字凭据被原样打出"
        assert "redacted" in rendered, f"{key} 没走遮蔽分支"

    # bool 不是计数：isinstance(True, int) 为 True，不能让它溜过去
    assert "redacted" in repr(summarize_obj({"token": True}))


def test_numeric_passthrough_does_not_cover_token_credentials():
    """
    数值豁免**不得**覆盖 token 类凭据 —— 这是 `NUMERIC_OK_HINTS` 单独使用时
    的缺陷：实测 12 个数值型 token 凭据被原样打进日志。

    根因：`SECRET_KEY_HINTS` 里那条 hint 是光秃秃的 `token`，所以凭据
    `access_token` 和计数 `max_tokens` 命中的 hint **完全相同**（都是
    `('token',)`），只看 hint 区分不开两者。`bearer_token` 当时侥幸没泄漏，
    是因为它还命中了 `bearer` —— 这种"靠第二个 hint 兜住"的运气恰恰说明
    判据有洞，所以它也在下面的必遮清单里。

    修法是叠加**第二道键名判据**（`NUMERIC_COUNTER_MARKS`）。本测试三个
    方向一起守，因为它们会互相拉扯：只修凭据方向会把计数字段全误遮
    （实测 `NUMERIC_OK_HINTS = ("tokens","token_count")` 这个改法让
    计数字段 10/10 变遮蔽态），只修计数方向就是现在这个泄漏。

    红灯验证：把 `_should_redact` 里第二道键名判据删掉（恢复成
    `NUMERIC_OK_HINTS` 单独使用），第一组断言会 AssertionError。
    """
    from ai_assistant.utils.log_summary import summarize_obj

    numeric = 1234567890123456

    # 方向一：数值型 token 凭据必须遮
    token_credentials = [
        "token",
        "access_token",
        "refresh_token",
        "verification_token",   # config.example.yaml 的真实字段，明文模式下就是身份凭据
        "auth_token",
        "tenant_access_token",
        "app_access_token",
        "user_access_token",
        "app_token",
        "api_token",
        "session_token",
        "id_token",
        "bearer_token",         # 当时靠 bearer 侥幸逃过，不能依赖这种运气
    ]
    for key in token_credentials:
        rendered = repr(summarize_obj({key: numeric}))
        assert str(numeric) not in rendered, (
            f"{key}={numeric} 数值型 token 凭据被原样打进日志"
        )
        assert "redacted" in rendered, f"{key} 没走遮蔽分支"

    # 方向二：数值型计数字段必须保持可读（含 camelCase，靠 _normalize_key 拆开）
    counters = [
        "max_tokens",
        "input_tokens",
        "output_tokens",
        "token_count",
        "total_tokens",
        "cache_read_input_tokens",
        "tokens_used",
        "num_tokens",
        "prompt_tokens",
        "completion_tokens",
        "maxTokens",
        "inputTokens",
        "total_token_count",
        "usage_tokens",
        "tokens_remaining",
        "token_limit",
        "num_tokens_used",
    ]
    for key in counters:
        rendered = summarize_obj({key: 4096})
        assert rendered == {key: 4096}, f"{key} 的计数值被误遮：{rendered}"

    # 方向三：键名含计数词、但本体是凭据的，必须仍然遮。
    # 这组守的是「两道判据必须叠加」—— 只用键名判据会把它们当计数放过。
    counter_looking_credentials = ["max_app_secret", "total_password", "num_api_key"]
    for key in counter_looking_credentials:
        rendered = repr(summarize_obj({key: numeric}))
        assert str(numeric) not in rendered, (
            f"{key} 含计数词就被放过了 —— 键名判据不能替代 hint 判据"
        )
        assert "redacted" in rendered, f"{key} 没走遮蔽分支"


# ============ P1：summary() 的返回值不是可信成品 ============


def test_summary_method_return_value_is_not_trusted():
    """
    `summarize_obj` 不得原样返回对象 `summary()` 的结果。

    修复前实测：
      class S:
          def summary(self): return {'secret_token':'LEAKED-XYZ'}
      summarize_obj(S()) → {'secret_token': 'LEAKED-XYZ'}      遮蔽被完全绕过

    当前仓库里两个真实 summary()（core/models.py）都返回已脱敏的 str，
    所以**无实际风险**；但这是结构性缺口 —— 将来谁写个返回 dict 的
    summary()，凭据遮蔽和长文摘要就被静默跳过。修法是把返回值当普通数据
    再过一遍 summarize_obj。

    红灯验证：把该分支改回 `return summary_fn()`，本测试 AssertionError。
    """
    from ai_assistant.utils.log_summary import summarize_obj

    class _DictSummary:
        def summary(self):
            return {"secret_token": "LEAKED-XYZ", "key_passphrase": "PHRASE-XYZ"}

    rendered = repr(summarize_obj(_DictSummary()))
    assert "LEAKED-XYZ" not in rendered, "summary() 返回的 dict 绕过了凭据遮蔽"
    assert "PHRASE-XYZ" not in rendered
    assert "redacted" in rendered

    class _LongSummary:
        def summary(self):
            return "X" * 5000

    rendered_long = repr(summarize_obj(_LongSummary()))
    assert "X" * 5000 not in rendered_long, "summary() 返回的超长 str 没被摘要"
    assert "len=5000" in rendered_long


def test_real_model_summary_stays_readable():
    """
    回归：P1 的加固不能把真实 summary() 的可读性搞坏。

    `Content.summary()` 返回 'log_file(app.log, text_len=50000)' 这种短单行，
    递归后应走「原样透传」保持原样 —— 若改成无条件 summarize_text，
    它会变成 `text(len=33,lines=1):'log_file(...)'`，整条结构日志没法读。
    """
    from ai_assistant.core.models import Content
    from ai_assistant.utils.log_summary import summarize_obj

    c = Content(type="log_file", data={"name": "app.log", "text": "Y" * 50000})
    rendered = repr(summarize_obj({"log_contents": [c]}))

    assert "log_file(app.log, text_len=50000)" in rendered
    assert "Y" * 50000 not in rendered
    assert "text(len=" not in rendered, "短 summary 被多套了一层摘要，可读性变差"


def test_short_content_full_text_boundary_is_documented():
    """
    已知边界的**行为锚定**：短内容在日志里仍是完整原文，这是有意取舍。

    实测精确结果（不是"≤120 全部可还原"）：
      - len <= 80   → 走整段显示分支，日志里就是原文
      - len == 120  → head80 + tail40 恰好无缝衔接，拼起来也是原文
      - 81 ~ 119    → 中间有缺口，还原不出

    这条测试不主张"应该这样"，而是把现状钉住：哪天有人调
    PREVIEW_HEAD_CHARS / PREVIEW_TAIL_CHARS，这里会失败，提醒他边界挪了、
    `log_summary.py` 顶部那段「已知边界」说明需要同步更新。
    """
    import random
    import re
    import string
    from ai_assistant.utils.log_summary import (
        summarize_text,
        PREVIEW_HEAD_CHARS,
        PREVIEW_TAIL_CHARS,
    )

    def _reconstructable(n: int) -> bool:
        random.seed(n)
        s = "".join(random.choice(string.ascii_letters + string.digits) for _ in range(n))
        frags = re.findall(r"'((?:[^'\\]|\\.)*)'", summarize_text(s))
        return "".join(frags) == s

    assert _reconstructable(PREVIEW_HEAD_CHARS) is True
    assert _reconstructable(PREVIEW_HEAD_CHARS + PREVIEW_TAIL_CHARS) is True
    assert _reconstructable(PREVIEW_HEAD_CHARS + 1) is False
    assert _reconstructable(PREVIEW_HEAD_CHARS + PREVIEW_TAIL_CHARS - 1) is False
    assert _reconstructable(PREVIEW_HEAD_CHARS + PREVIEW_TAIL_CHARS + 1) is False


def test_identifier_whitelist_survives_an_over_broad_hint():
    """
    "不许遮过头"必须是**机制**而不是约定。

    `NEVER_SECRET_KEYS` 先于 hints 判定，所以哪天有人往 SECRET_KEY_HINTS 里
    加了个宽子串（最可能的是裸 "key" 或 "id"），排查标识也不会被连带遮掉。
    这里临时注入一个最坏情况的宽 hint 来验证这条防线真的存在 —— 只验白名单
    这一层，不改变生产配置（用完即恢复）。

    红灯验证：把 `_is_secret_key` 里的白名单判断去掉，本测试 AssertionError。
    """
    from ai_assistant.utils import log_summary as ls

    original = ls.SECRET_KEY_HINTS
    try:
        # 最坏情况：有人加了裸 "id"，它会命中 app_id/open_id/chat_id/message_id/session_id
        ls.SECRET_KEY_HINTS = original + ("id",)
        for key in ("app_id", "open_id", "user_id", "chat_id", "message_id", "session_id"):
            rendered = repr(ls.summarize_obj({key: "oc_abc123"}))
            assert "oc_abc123" in rendered, f"宽 hint 把排查标识 {key} 连带遮掉了"
        # 同时确认这个宽 hint 确实是"宽"的（否则本测试没有判别力）：
        # 一个不在白名单里、含 id 的字段应当被它命中
        assert "redacted" in repr(ls.summarize_obj({"some_id": "x"}))
    finally:
        ls.SECRET_KEY_HINTS = original


def test_enum_is_not_expanded_into_internals():
    """
    Enum 不得走通用 __dict__ 递归。

    实测（加固前）：`SSHConfig.method` 被摊成
    `{'_value_': 'password', '_name_': 'PASSWORD', '__objclass__': '<mappingproxy depth-limit>', '_sort_order_': 0}`
    —— 既刷屏，又让人看不出它其实就是 PASSWORD。

    红灯验证：删掉 Enum 分支，本测试 AssertionError。
    """
    from ai_assistant.core.operations_models import SSHConfig
    from ai_assistant.utils.log_summary import summarize_obj

    cfg = SSHConfig(
        host="10.0.0.1",
        username="root",
        password="PLAIN-PWD-1",
        key_passphrase="PHRASE-SECRET-2",
    )
    rendered = repr(summarize_obj(cfg))

    # Enum 读起来就是它的名字
    assert "SSHMethod.PASSWORD" in rendered
    assert "_value_" not in rendered, "Enum 内部字段被摊开"
    assert "__objclass__" not in rendered
    # 顺带守住 P0 的真实触发点：同一个 SSHConfig 里两个凭据都要遮住
    assert "PLAIN-PWD-1" not in rendered, "password 泄漏"
    assert "PHRASE-SECRET-2" not in rendered, "key_passphrase 泄漏（P0 原始漏网）"
    # 排查必需的信息仍可读
    assert "10.0.0.1" in rendered
    assert "root" in rendered
