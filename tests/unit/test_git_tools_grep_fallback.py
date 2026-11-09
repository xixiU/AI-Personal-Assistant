"""
search_code 多级正则降级（pcre → ere → keywords → literal）

原 bug：git grep -E 不认 AI 习惯的 Perl 写法（(?i) \\d (?: 等），报 128 后退回 -F 把
整串正则当字面量搜，必然为空，AI 误判"代码里没有这个功能"，且不知道发生了降级。

复现：对任意仓库 search_code('(?i)(patch|train).*(show|menu)')，旧实现 results 为空。

约束：
- 只有"模式执行不了"才降级；"无匹配"（git grep 退出码 1）不能降级，否则正确的正则
  会被逐级放宽成关键词搜索，搜出无关结果
- 降级到关键词/字面量时结果里必须带 notice

顺带的 bug：core.quotePath 默认 true，中文路径输出成 "\\345\\211\\215..." 八进制转义，
AI 拿去 read_file 找不到文件。复现：仓库里放中文文件名，git config core.quotePath true，
search_code 后用返回的 file 调 read_file。
用真实 git 跑，不 mock 子进程。
"""

import subprocess
import sys
import types
from unittest.mock import Mock

import pytest

# tools/__init__.py 会 import operations_commands → paramiko，桩法与 test_log_tools.py 一致
if "paramiko" not in sys.modules:
    _stub = types.ModuleType("paramiko")
    _stub.__getattr__ = lambda _attr: Mock()  # type: ignore[attr-defined]
    sys.modules["paramiko"] = _stub

from ai_assistant.tools.git_tools import GitTools  # noqa: E402

CONTENT = (
    "public class CourtService {\n"
    "  getCourtInfo() {}\n"
    "  showPatchMenu = true;\n"
    "  // zzz and qqq\n"
    "}\n"
)


@pytest.fixture
def repo(tmp_path, monkeypatch):
    def git(*args):
        subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True)

    git("init", "-q", ".")
    (tmp_path / "CourtService.java").write_text(CONTENT, encoding="utf-8")
    git("add", "-A")
    git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init")
    # 探测结果是进程级缓存，每条用例各自决定，互不影响
    monkeypatch.setattr(GitTools, "_pcre_supported", None)
    return GitTools(str(tmp_path), default_ref="HEAD")


def test_search_code_pcre_runs_perl_style_regex(repo):
    """git 支持 -P 时，带 (?i) 的 Perl 写法按原正则直接命中，不附降级说明"""
    if not repo._pcre_available():
        pytest.skip("当前 git 未编译 libpcre2")
    r = repo.search_code(r"(?i)(patch|train).*(show|display|menu)|class\s+\w+Service")
    assert r["strategy"] == "pcre"
    assert {x["line"] for x in r["results"]} == {1, 3}
    assert "notice" not in r


def test_search_code_ere_conversion_when_pcre_unavailable(repo, monkeypatch):
    """-P 不可用时，(?i) \\b \\w 懒惰量词经转换后按 ERE 命中"""
    monkeypatch.setattr(GitTools, "_pcre_supported", False)
    r = repo.search_code(r"(?i)\bgetCourt\w+?\(\)")
    assert r["strategy"] == "ere"
    assert [x["line"] for x in r["results"]] == [2]


def test_search_code_keywords_fallback_carries_notice(repo):
    """pcre 与 ere 都执行不了时，拆成关键词搜，并告诉 AI 实际按什么搜的"""
    # {2,1} 在 PCRE 和 ERE 里都是非法区间，_perl_to_ere 原样放行，交给 git 判错
    r = repo.search_code("getCourtInfo{2,1}")
    assert r["strategy"] == "keywords"
    assert [x["line"] for x in r["results"]] == [2]
    assert "getCourtInfo" in r["notice"] and "结果为空不代表代码里没有" in r["notice"]


def test_search_code_chinese_path_readable(repo):
    """仓库 core.quotePath=true（git 默认）时，搜到的中文路径能直接拿去 read_file"""
    root = repo.repo_path
    (root / "功能模块").mkdir()
    (root / "功能模块" / "01-庭审流程控制.md").write_text("开庭前检查设备\n", encoding="utf-8")
    for args in (["config", "core.quotePath", "true"], ["add", "-A"],
                 ["-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "zh"]):
        subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)
    hit = repo.search_code("开庭前")["results"][0]["file"]
    assert hit == "功能模块/01-庭审流程控制.md"
    assert repo.read_file(hit)["content"].startswith("开庭前检查设备")


def test_search_code_no_match_does_not_degrade(repo):
    """正确但确实无匹配的正则不降级：拆成关键词 zzz/qqq 会误中第 4 行"""
    r = repo.search_code(r"zzz\d+qqq")
    assert r["total"] == 0
    assert r["strategy"] in ("pcre", "ere")
