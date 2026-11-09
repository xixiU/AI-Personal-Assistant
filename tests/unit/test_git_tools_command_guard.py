"""
run_git_command 参数白名单

原 bug：白名单只检查 args[0]，后面的参数不过滤。AI 被用户消息里夹带的文本诱导后可以：
grep -O<命令> 执行任意命令、log --output=<路径> 写仓库外文件、blame --contents <路径>
读仓库外文件、branch/tag/remote add 改写仓库（remote add 后后台 fetch --all 会拉攻击者地址）。

复现：对任意仓库 run_git_command(['grep', '-Otouch /tmp/m', 'x'])，旧实现 /tmp/m 被创建；
run_git_command(['blame', '--cont=/path/secret', 'a.txt']) 输出 secret 内容。

约束：
- git 接受长参数无歧义缩写（--cont 即 --contents），按前缀判断，不能只比全名
- 合并短参数（-pO<文件>）要逐字母看
- diff 给出仓库外路径时会自动按 --no-index 比较，不写 --no-index 也能读任意文件
- branch/tag 保留给 --contains 查"修复进了哪些版本"，只放行列表模式；不带 --list 的
  位置参数等于创建分支/标签
- 正常的 log/blame/show 用法不能误伤，尤其是 blame 的 `--` 分隔符；diff 的版本参数
  （origin/dev-4.3.6、a..b）不能被当成路径
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


@pytest.fixture
def env(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()

    def git(*args):
        return subprocess.run(["git", *args], cwd=repo, check=True,
                              capture_output=True, text=True).stdout

    git("init", "-q", ".")
    for i in (1, 2):
        (repo / "a.txt").write_text(f"l1\nl2\nl3 v{i}\n", encoding="utf-8")
        git("add", "-A")
        git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", f"c{i}")
    git("tag", "v1", "HEAD~1")
    git("update-ref", "refs/remotes/origin/a", "HEAD~1")
    git("update-ref", "refs/remotes/origin/b", "HEAD")
    secret = tmp_path / "secret.txt"
    secret.write_text("TOP-SECRET-VALUE\n", encoding="utf-8")
    return GitTools(str(repo), default_ref="HEAD"), git, tmp_path, secret


def test_run_git_command_rejects_exec_write_read_and_ref_changes(env):
    """执行命令、写文件、读仓库外文件、改写引用四类写法全部被拒，且没有任何副作用"""
    tools, git, base, secret = env
    marker = base / "marker"
    out_file = base / "written.txt"
    s, m, w = secret.as_posix(), marker.as_posix(), out_file.as_posix()
    attacks = [
        # 执行命令
        ["grep", f"-Otouch {m}", "l1"],
        ["grep", f"--open-files-in-pager=touch {m}", "l1"],
        ["diff", "--ext-diff", "HEAD"],
        ["cat-file", "--textconv", "HEAD:a.txt"],
        # 写文件（含缩写、分开传值）
        ["log", f"--output={w}", "-1"],
        ["diff", f"--outp={w}", "HEAD", "HEAD"],
        ["show", "--output", w, "HEAD"],
        # 读仓库外文件（含缩写、合并短参数、隐式 no-index）
        ["blame", "--contents", s, "a.txt"],
        ["blame", f"--cont={s}", "a.txt"],
        ["blame", "-S", s, "a.txt"],
        ["diff", "--no-index", s, "a.txt"],
        ["diff", s, "a.txt"],
        ["log", f"-pO{s}", "-1"],
        ["ls-files", f"-oX{s}"],
        # 改写引用（含缩写 --del、--contains 后跟位置参数）
        ["branch", "evil"],
        ["branch", "--contains", "HEAD", "evil"],
        ["branch", "-dr", "origin/a"],
        ["branch", "--del", "-r", "origin/a"],
        ["branch", "-m", "renamed"],
        ["tag", "evil"],
        ["tag", "-d", "v1"],
        ["tag", "-f", "v1", "HEAD"],
        ["remote", "add", "evil", "https://evil.example/x.git"],
        ["reflog", "expire", "--expire=now", "--all"],
    ]
    refs_before = git("for-each-ref") + git("remote", "-v") + git("reflog")

    results = [(a, tools.run_git_command(list(a))) for a in attacks]

    # 必须是参数检查拒绝的，不能是 git 自己执行失败（diff --no-index 有差异时退出码 1，
    # 同样会返回 error，但那时外部文件已经被读过了）
    allowed = [a for a, r in results if "不允许" not in r.get("error", "")]
    assert allowed == []
    assert not any("TOP-SECRET" in str(r) for _, r in results)
    assert not marker.exists()
    assert not out_file.exists()
    assert git("for-each-ref") + git("remote", "-v") + git("reflog") == refs_before


def test_run_git_command_keeps_normal_readonly_usage(env):
    """常规只读用法照常可用，输出正确"""
    tools, _, _, _ = env
    cases = [
        (["log", "--oneline", "-5"], "c2"),
        (["blame", "-L", "1,3", "--", "a.txt"], "l3 v2"),
        (["show", "HEAD:a.txt"], "l3 v2"),
        (["log", "-c", "-1", "--oneline"], "c2"),
        (["diff", "--text", "HEAD~1", "HEAD", "--", "a.txt"], "+l3 v2"),
        (["diff", "origin/a", "origin/b"], "+l3 v2"),
        (["diff", "HEAD~1..HEAD", "--stat"], "a.txt"),
        (["branch", "-r", "--contains", "HEAD"], "origin/b"),
        (["tag", "--contains", "HEAD~1"], "v1"),
        (["tag", "-l", "v*"], "v1"),
    ]
    for args, expected in cases:
        result = tools.run_git_command(list(args))
        assert "error" not in result, (args, result)
        assert expected in result["output"], (args, result)
