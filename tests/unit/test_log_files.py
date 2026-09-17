"""
日志文件附件处理测试

覆盖：扩展名白名单、大小上限、zip 安全解压（zip slip / 解压炸弹防护）、
正文截断、定时清理的保留期判定。

安全重点：zip 条目可能包含 ../ 或绝对路径试图写到目录外（zip slip），
必须拒绝并且不在目标目录外留下任何文件。
"""
import os
import time
import zipfile

import pytest

from ai_assistant.utils.log_files import LogFileManager, sanitize_filename


@pytest.fixture
def manager(tmp_path):
    return LogFileManager(
        enabled=True,
        base_dir=str(tmp_path / "logs"),
        max_size_mb=1,
        inline_chars=100,
        cleanup_hour=23,
        retention_hours=24,
    )


# ============ 文件名清洗 ============

def test_sanitize_strips_path_separators():
    assert "/" not in sanitize_filename("../../etc/passwd")
    assert "\\" not in sanitize_filename(r"..\..\windows\system32\cfg")


def test_sanitize_keeps_chinese_and_ext():
    assert sanitize_filename("服务日志.log") == "服务日志.log"


def test_sanitize_empty_uses_fallback():
    assert sanitize_filename("", fallback="attachment") == "attachment"


# ============ 扩展名白名单 ============

def test_allowed_extensions(manager):
    assert manager.is_allowed("app.log")
    assert manager.is_allowed("out.TXT")
    assert manager.is_allowed("logs.zip")


def test_disallowed_extensions(manager):
    assert not manager.is_allowed("setup.exe")
    assert not manager.is_allowed("report.docx")
    assert not manager.is_allowed("noext")


# ============ 落地与大小上限 ============

def test_save_bytes_writes_file(manager):
    path = manager.save_bytes("oc_chat1", "app.log", b"hello log")
    assert path is not None
    assert os.path.isfile(path)
    with open(path, "rb") as f:
        assert f.read() == b"hello log"


def test_save_bytes_rejects_oversize(manager):
    """超过 max_size_mb 的文件不落地，返回 None（由上层告知 AI）"""
    assert manager.save_bytes("oc_chat1", "big.log", b"x" * (2 * 1024 * 1024)) is None


def test_save_bytes_no_overwrite(manager):
    """同名文件不互相覆盖"""
    p1 = manager.save_bytes("oc_chat1", "app.log", b"first")
    p2 = manager.save_bytes("oc_chat1", "app.log", b"second")
    assert p1 != p2
    assert os.path.isfile(p1) and os.path.isfile(p2)


def test_save_bytes_isolates_sessions(manager):
    """不同会话落到不同子目录"""
    p1 = manager.save_bytes("oc_a", "app.log", b"a")
    p2 = manager.save_bytes("oc_b", "app.log", b"b")
    assert os.path.dirname(p1) != os.path.dirname(p2)


def test_disabled_manager_saves_nothing(tmp_path):
    m = LogFileManager(enabled=False, base_dir=str(tmp_path / "logs"))
    assert m.save_bytes("oc_a", "app.log", b"x") is None


# ============ 正文读取与截断 ============

def test_read_text_head_truncates(manager):
    path = manager.save_bytes("oc_chat1", "app.log", ("A" * 500).encode())
    text = manager.read_text_head(path)
    assert "已截断" in text
    # 截断到 inline_chars=100，加上说明文字
    assert text.count("A") == 100


def test_read_text_head_full_when_small(manager):
    path = manager.save_bytes("oc_chat1", "small.log", b"short content")
    assert manager.read_text_head(path) == "short content"


def test_read_text_head_handles_bad_encoding(manager):
    """日志编码不确定，非法字节不应导致读取失败"""
    path = manager.save_bytes("oc_chat1", "gbk.log", b"\xff\xfe\x00bad bytes")
    assert manager.read_text_head(path) != ""


def test_read_text_head_missing_file(manager):
    assert manager.read_text_head("/nonexistent/path.log") == ""


# ============ zip 解压与安全防护 ============

def _make_zip(path, entries):
    with zipfile.ZipFile(path, "w") as zf:
        for name, data in entries:
            zf.writestr(name, data)


def test_extract_zip_collects_text_logs(manager, tmp_path):
    z = tmp_path / "logs.zip"
    _make_zip(z, [("a.log", "line1"), ("sub/b.txt", "line2"), ("c.bin", "x")])
    out_dir, extracted = manager.extract_zip(str(z))
    assert out_dir is not None
    # 只收集文本日志，.bin 不计入
    assert len(extracted) == 2
    assert all(os.path.isfile(p) for p in extracted)


def test_extract_zip_rejects_path_traversal(manager, tmp_path):
    """zip slip 防护：../ 条目必须被拒绝，且目录外不留文件"""
    z = tmp_path / "evil.zip"
    _make_zip(z, [("../escaped.log", "pwned"), ("safe.log", "ok")])
    out_dir, extracted = manager.extract_zip(str(z))
    assert not os.path.exists(str(tmp_path / "escaped.log")), "路径穿越条目被写到了目录外"
    # 安全条目仍正常解出
    assert any(p.endswith("safe.log") for p in extracted)


def test_extract_zip_rejects_absolute_path(manager, tmp_path):
    """绝对路径条目同样被拒绝"""
    z = tmp_path / "abs.zip"
    _make_zip(z, [("/tmp/abs_escaped.log", "pwned"), ("safe.log", "ok")])
    out_dir, extracted = manager.extract_zip(str(z))
    assert not any("abs_escaped" in p for p in extracted)


def test_extract_zip_on_non_zip(manager):
    path = manager.save_bytes("oc_chat1", "fake.zip", b"not a zip at all")
    out_dir, extracted = manager.extract_zip(path)
    assert out_dir is None
    assert extracted == []


# ============ 清理 ============

def test_cleanup_removes_old_files(manager):
    old = manager.save_bytes("oc_chat1", "old.log", b"old")
    new = manager.save_bytes("oc_chat1", "new.log", b"new")
    # 把 old 的 mtime 推到保留期之外
    stale = time.time() - manager.retention_seconds - 60
    os.utime(old, (stale, stale))

    removed = manager.cleanup_once()
    assert removed == 1
    assert not os.path.exists(old)
    assert os.path.exists(new), "保留期内的文件被误删"


def test_cleanup_on_empty_dir(manager):
    assert manager.cleanup_once() == 0


def test_cleanup_disabled_noop(tmp_path):
    m = LogFileManager(enabled=False, base_dir=str(tmp_path / "logs"))
    assert m.cleanup_once() == 0


def test_cleanup_hour_clamped(tmp_path):
    """非法清理时刻被夹到合法范围，避免调度线程永不触发"""
    assert LogFileManager(base_dir=str(tmp_path / "a"), cleanup_hour=99).cleanup_hour == 23
    assert LogFileManager(base_dir=str(tmp_path / "b"), cleanup_hour=-5).cleanup_hour == 0
