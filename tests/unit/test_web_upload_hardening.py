"""
Web 端日志文件上传的安全加固测试

防回归要点：
1. 两阶段落地：先全量校验再统一写盘。原实现边校验边落地，第 2 个文件非法时整个
   请求返回 400，但第 1 个已写进磁盘且不回滚——等于一条未授权写盘路径。
2. session_id 格式白名单：它是落地子目录名，路径穿越虽已由 sanitize_filename 挡住，
   但不限制格式就能在落地目录下无限创建子目录。
3. MAX_CONTENT_LENGTH：不设上限时 request.get_json() 会把整个 body 读进内存。
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
from ai_assistant.utils.log_files import LogFileManager


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


# ============ 请求体上限 ============

def test_max_content_length_configured():
    """Flask 必须设了请求体上限，且覆盖得下合法的最大请求"""
    srv = WebhookServer(port=0, web_frontend_enabled=True)
    assert srv.app.config["MAX_CONTENT_LENGTH"] == _MAX_CONTENT_LENGTH
    # 上限要大于 5 个 20MB 文件的 base64 体积（膨胀约 1.37 倍）
    assert _MAX_CONTENT_LENGTH > _MAX_WEB_LOG_FILES * 20 * 1024 * 1024 * 1.37
