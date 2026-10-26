"""
Web 端日志文件上传的安全加固测试

防回归要点：
1. 两阶段落地：先全量校验再统一写盘。原实现边校验边落地，第 2 个文件非法时整个
   请求返回 400，但第 1 个已写进磁盘且不回滚——等于一条未授权写盘路径。
2. session_id 格式白名单：它是落地子目录名，路径穿越虽已由 sanitize_filename 挡住，
   但不限制格式就能在落地目录下无限创建子目录。
3. MAX_CONTENT_LENGTH：不设上限时 request.get_json() 会把整个 body 读进内存。
4. 文件名清洗必须保留非 ASCII：旧实现的字符白名单只覆盖 CJK 统一汉字区，日文
   假名/西里尔/谚文被替换成下划线后，strip("._") 连带吃掉分隔点，"ログ.log"
   变成 "log"，扩展名退化导致合法日志被 400 拒收。放宽字符集的同时，路径穿越、
   NTFS 数据流、Windows 保留设备名这些安全属性一条都不能退化。
"""
import os
import shutil
import tempfile

import pytest

from ai_assistant.webhook_server import (
    WebhookServer,
    _MAX_WEB_LOG_FILES,
    _MAX_CONTENT_LENGTH,
    _SESSION_ID_RE,
)
from ai_assistant.utils.log_files import LogFileManager, sanitize_filename


@pytest.fixture
def server_with_manager():
    """带真实 LogFileManager 的服务器实例（落地到临时目录）"""
    tmp = tempfile.mkdtemp(prefix="logfiles_test_")
    manager = LogFileManager(enabled=True, base_dir=tmp, max_size_mb=1)
    srv = WebhookServer(port=0, web_frontend_enabled=True)
    srv.set_log_file_manager(manager)
    yield srv, manager, tmp
    shutil.rmtree(tmp, ignore_errors=True)


def _b64(text: str) -> str:
    import base64
    return base64.b64encode(text.encode()).decode()


def _count_files(root: str) -> int:
    return sum(len(files) for _, _, files in os.walk(root))


# ============ 两阶段落地 ============

def test_invalid_second_file_leaves_no_partial_write(server_with_manager):
    """第 2 个文件非法时，第 1 个合法文件不得残留在磁盘上"""
    srv, _manager, tmp = server_with_manager

    contents, error = srv._collect_web_log_files("sess1", [
        {"name": "good.log", "data": _b64("ERROR boom")},
        {"name": "evil.exe", "data": _b64("MZ")},          # 非白名单扩展名
    ])

    assert error is not None
    assert contents == []
    assert _count_files(tmp) == 0, "非法请求不应留下任何落地文件"


def test_bad_base64_in_second_file_leaves_no_partial_write(server_with_manager):
    """第 2 个文件 base64 损坏时同样不得有部分写盘"""
    srv, _manager, tmp = server_with_manager

    contents, error = srv._collect_web_log_files("sess1", [
        {"name": "good.log", "data": _b64("ok")},
        {"name": "broken.log", "data": "!!!not-base64!!!"},
    ])

    assert error is not None
    assert _count_files(tmp) == 0


def test_oversize_second_file_leaves_no_partial_write(server_with_manager):
    """第 2 个文件超大时同样不得有部分写盘"""
    srv, _manager, tmp = server_with_manager

    contents, error = srv._collect_web_log_files("sess1", [
        {"name": "good.log", "data": _b64("ok")},
        {"name": "huge.log", "data": _b64("x" * (2 * 1024 * 1024))},  # 上限 1MB
    ])

    assert error is not None
    assert "超过大小上限" in error
    assert _count_files(tmp) == 0


def test_all_valid_files_are_written(server_with_manager):
    """全部合法时才落地，且内容块数量正确"""
    srv, _manager, tmp = server_with_manager

    contents, error = srv._collect_web_log_files("sess1", [
        {"name": "a.log", "data": _b64("ERROR a")},
        {"name": "b.txt", "data": _b64("ERROR b")},
    ])

    assert error is None
    assert len(contents) == 2
    assert _count_files(tmp) == 2
    # Content 结构须与飞书侧同构，否则下游注入不了
    for c in contents:
        assert c.type == "log_file"
        assert set(c.data.keys()) == {"name", "path", "text", "note"}


def test_file_count_limit(server_with_manager):
    """超过文件数上限直接拒绝，不落地"""
    srv, _manager, tmp = server_with_manager

    files = [
        {"name": f"f{i}.log", "data": _b64("x")}
        for i in range(_MAX_WEB_LOG_FILES + 1)
    ]
    contents, error = srv._collect_web_log_files("sess1", files)

    assert error is not None
    assert _count_files(tmp) == 0


def test_manager_not_injected_returns_error():
    """未注入 LogFileManager 时返回明确错误，而不是静默丢附件"""
    srv = WebhookServer(port=0, web_frontend_enabled=True)
    contents, error = srv._collect_web_log_files("sess1", [
        {"name": "a.log", "data": _b64("x")},
    ])
    assert contents == []
    assert error is not None


# ============ session_id 格式 ============

@pytest.mark.parametrize("sid", [
    "web-default", "conv_123", "a.b-c_1", "A1", "x" * 64,
])
def test_valid_session_ids(sid):
    assert _SESSION_ID_RE.match(sid)


@pytest.mark.parametrize("sid", [
    "", "../../etc", "a/b", "a\\b", "x" * 65, "sess id", "sess;rm", "会话1",
])
def test_invalid_session_ids(sid):
    assert not _SESSION_ID_RE.match(sid)


# ============ 文件名清洗：非 ASCII 不得被拒收 ============
#
# 回归背景：旧实现用字符白名单 [^A-Za-z0-9._\-一-龥]，只覆盖 CJK 统一汉字区
# （U+4E00-U+9FA5）。日文假名、西里尔、谚文被整体替换成下划线后，紧跟的
# strip("._") 会把下划线连同分隔点一起剥掉，于是 "ログ.log" 变成 "log"——
# 扩展名退化成名字主体，is_allowed 取到空后缀，合法日志被 400 拒收。
# 两个缺陷叠加：区段不全决定哪些语言受害，strip 跨点决定后果是拒收而非改名。

@pytest.mark.parametrize("raw, expect_stem", [
    ("ログ.log", "ログ"),          # 片假名 U+30ED U+30B0
    ("лог.log", "лог"),           # 西里尔 U+043B U+043E U+0433
    ("로그.log", "로그"),          # 谚文 U+B85C U+ADF8
    ("日志.log", "日志"),          # 中文（旧实现唯一能过的非 ASCII）
    ("龦.log", "龦"),             # U+9FA6，旧白名单上界 U+9FA5 之外的汉字
    ("app.log", "app"),           # 纯 ASCII
])
def test_sanitize_preserves_non_ascii_names_and_ext(raw, expect_stem):
    """各语言文件名的主体与扩展名都必须原样保留"""
    assert sanitize_filename(raw, fallback="attachment.log") == f"{expect_stem}.log"


@pytest.mark.parametrize("raw", [
    "ログ.log", "лог.log", "로그.log", "日志.log", "龦.log", "app.log",
])
def test_non_ascii_names_pass_extension_whitelist(raw):
    """清洗后的名字必须仍能通过扩展名白名单——这是原 bug 的直接表现"""
    manager = LogFileManager(enabled=False, base_dir="/tmp/unused")
    assert manager.is_allowed(sanitize_filename(raw, fallback="attachment.log"))


def test_non_ascii_upload_is_accepted_end_to_end(server_with_manager):
    """端到端：日文名日志文件必须被接收并落地，而不是返回 400"""
    srv, _manager, tmp = server_with_manager

    contents, error = srv._collect_web_log_files("sess1", [
        {"name": "ログ.log", "data": _b64("ERROR 障害発生")},
    ])

    assert error is None, f"合法的日文名日志被拒收: {error}"
    assert len(contents) == 1
    assert contents[0].data["name"] == "ログ.log"
    assert _count_files(tmp) == 1


# ============ 文件名清洗：扩展名判定独立于主体净化 ============

def test_extension_survives_full_stem_rewrite():
    """主体被整体改写时扩展名仍须保留（原 bug 的核心机制）"""
    # 控制字符会被替换成下划线，但 .log 不能跟着消失
    assert sanitize_filename("\x01\x02.log", fallback="attachment.log").endswith(".log")


def test_split_name_ext_independent_of_stem():
    """split_name_ext 对主体不做任何净化，只负责切分"""
    # 延迟导入：该辅助函数是本次修复新增的。放在模块顶层 import 会让旧实现下
    # 整个文件收集失败（ImportError），把其余行为断言的红/绿信号一起掩盖掉。
    from ai_assistant.utils.log_files import split_name_ext

    assert split_name_ext("ログ.log") == ("ログ", ".log")
    assert split_name_ext("logfile") == ("logfile", "")
    assert split_name_ext("a.log.txt") == ("a.log", ".txt")


def test_double_extension_uses_last_segment():
    """双扩展名按最后一段判定，a.log.txt 的后缀是 .txt"""
    manager = LogFileManager(enabled=False, base_dir="/tmp/unused")
    assert sanitize_filename("a.log.txt", fallback="attachment.log") == "a.log.txt"
    assert manager.is_allowed("a.log.txt")
    # .exe 结尾不得因为中间有 .log 就被放过
    assert not manager.is_allowed("a.log.exe")


def test_no_extension_still_rejected():
    """无扩展名必须仍被白名单拒绝——修复不能放宽扩展名校验"""
    manager = LogFileManager(enabled=False, base_dir="/tmp/unused")
    assert not manager.is_allowed(sanitize_filename("logfile", fallback="attachment.log"))
    assert not manager.is_allowed("noext")


# ============ 文件名清洗：安全属性不得因放宽字符集而退化 ============

@pytest.mark.parametrize("raw", [
    "../../etc/passwd.log",
    r"..\..\windows\system32\cfg.log",
    "/absolute/path/app.log",
    "C:/tmp/app.log",
])
def test_sanitize_strips_path_traversal(raw):
    """黑名单放宽了字符集，但路径穿越必须照旧挡住"""
    safe = sanitize_filename(raw, fallback="attachment.log")
    assert "/" not in safe and "\\" not in safe
    assert ".." not in safe
    assert not os.path.isabs(safe)


@pytest.mark.parametrize("raw", [
    "CON.log", "con.log", "NUL.txt", "prn.log", "AUX.log", "COM1.log", "lpt9.txt",
])
def test_windows_reserved_names_are_escaped(raw):
    """Windows 保留设备名字符过滤挡不住，必须整名比对后加前缀规避"""
    safe = sanitize_filename(raw, fallback="attachment.log")
    stem = safe.rsplit(".", 1)[0]
    assert stem.upper() not in {
        "CON", "PRN", "AUX", "NUL", "COM1", "LPT9",
    }, f"保留设备名未被规避: {safe}"
    # 规避后扩展名仍须完整，否则文件会被拒收
    assert safe.endswith(raw[raw.rindex("."):])


def test_ntfs_stream_suffix_is_dropped():
    """NTFS 数据流后缀必须被截断，且不得把 .exe 洗成合法扩展名"""
    manager = LogFileManager(enabled=False, base_dir="/tmp/unused")
    safe = sanitize_filename("app.log:evil.exe", fallback="attachment.log")
    assert ":" not in safe
    assert safe == "app.log"
    assert manager.is_allowed(safe)
    # 冒号前是非法扩展名时不得被洗成合法的
    assert not manager.is_allowed(sanitize_filename("a:evil.exe", fallback="attachment.log"))


def test_control_characters_are_removed():
    """控制字符在文件名里不可见，易被用来伪装，必须剔除"""
    safe = sanitize_filename("a\x00b\x1fc.log", fallback="attachment.log")
    assert not any(ord(c) < 0x20 or ord(c) == 0x7f for c in safe)
    assert safe.endswith(".log")


def test_long_filename_is_truncated_keeping_extension():
    """超长文件名截断后扩展名必须完整，否则 is_allowed 会失效"""
    manager = LogFileManager(enabled=False, base_dir="/tmp/unused")
    safe = sanitize_filename("x" * 300 + ".log", fallback="attachment.log")
    assert len(safe) <= 120
    assert safe.endswith(".log")
    assert manager.is_allowed(safe)


def test_empty_and_dot_only_names_use_fallback():
    """空串与纯点号用兜底名，且兜底名本身要能过白名单"""
    manager = LogFileManager(enabled=False, base_dir="/tmp/unused")
    for raw in ["", "...", "   ", "/", "\\", "../.."]:
        safe = sanitize_filename(raw, fallback="attachment.log")
        assert safe == "attachment.log", f"{raw!r} -> {safe!r}"
        assert manager.is_allowed(safe)


def test_fallback_without_extension_preserved():
    """兜底名不带扩展名时原样返回，保持旧有调用方的行为"""
    assert sanitize_filename("", fallback="attachment") == "attachment"


def test_trailing_dots_and_spaces_stripped():
    """Windows 会静默去掉首尾点与空格，清洗阶段就得去掉以免名字对不上"""
    assert sanitize_filename("app.log ", fallback="attachment.log") == "app.log"
    assert sanitize_filename(" app.log", fallback="attachment.log") == "app.log"


# ============ 请求体上限 ============

def test_max_content_length_configured():
    """Flask 必须设了请求体上限，且覆盖得下合法的最大请求"""
    srv = WebhookServer(port=0, web_frontend_enabled=True)
    assert srv.app.config["MAX_CONTENT_LENGTH"] == _MAX_CONTENT_LENGTH
    # 上限要大于 5 个 20MB 文件的 base64 体积（膨胀约 1.37 倍）
    assert _MAX_CONTENT_LENGTH > _MAX_WEB_LOG_FILES * 20 * 1024 * 1024 * 1.37
