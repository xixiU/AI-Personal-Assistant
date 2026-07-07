"""
Git 只读代码工具

基于 git 命令提供代码检索和浏览能力，所有操作都是只读的，不修改工作目录。
支持并发安全：多个线程可以同时查询不同分支的代码，互不干扰。
"""

import subprocess
import os
from pathlib import Path
from typing import List, Dict, Any, Optional
from loguru import logger


class GitTools:
    """Git 只读代码工具集"""

    def __init__(self, repo_path: str, default_ref: str = "origin/main"):
        """
        初始化 Git 工具

        Args:
            repo_path: 仓库根目录路径
            default_ref: 默认引用（分支/tag），用户未指定时使用
        """
        self.repo_path = Path(repo_path).resolve()
        self.default_ref = default_ref

        if not self.repo_path.exists():
            raise ValueError(f"仓库路径不存在: {repo_path}")

        if not (self.repo_path / ".git").exists():
            raise ValueError(f"不是有效的 git 仓库: {repo_path}")

        logger.info(f"GitTools 初始化: repo={self.repo_path}, default_ref={default_ref}")

    def _run_git_command(self, args: List[str], timeout: int = 30) -> str:
        """
        执行 git 命令（只读操作）

        Args:
            args: git 命令参数列表
            timeout: 超时时间（秒）

        Returns:
            命令输出

        Raises:
            subprocess.TimeoutExpired: 超时
            subprocess.CalledProcessError: 命令执行失败
        """
        try:
            result = subprocess.run(
                ["git"] + args,
                cwd=str(self.repo_path),
                capture_output=True,
                text=True,
                timeout=timeout,
                check=True
            )
            return result.stdout
        except subprocess.TimeoutExpired as e:
            logger.error(f"Git 命令超时: {' '.join(args)}")
            raise
        except subprocess.CalledProcessError as e:
            logger.error(f"Git 命令失败: {' '.join(args)}, stderr={e.stderr}")
            raise

    def _validate_ref(self, ref: str) -> bool:
        """
        验证 ref 是否存在（安全检查，防止命令注入）

        Args:
            ref: git 引用（分支/tag/commit）

        Returns:
            ref 是否存在
        """
        try:
            self._run_git_command(["rev-parse", "--verify", ref], timeout=5)
            return True
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
            return False

    def _validate_path(self, path: str) -> bool:
        """
        验证路径是否在仓库内（防止目录穿越）

        Args:
            path: 相对于仓库根目录的路径

        Returns:
            路径是否合法
        """
        try:
            # 解析为绝对路径并检查是否在仓库内
            full_path = (self.repo_path / path).resolve()
            return full_path.is_relative_to(self.repo_path)
        except (ValueError, OSError):
            return False

    def list_refs(self, pattern: Optional[str] = None) -> Dict[str, Any]:
        """
        列出分支和 tag

        Args:
            pattern: 可选的过滤模式（如 "4.3" 匹配包含 4.3 的分支/tag）

        Returns:
            包含 branches 和 tags 的字典
        """
        try:
            # 获取远程分支
            branches_output = self._run_git_command(["branch", "-r"])
            branches = [
                line.strip().replace("origin/", "")
                for line in branches_output.strip().split("\n")
                if line.strip() and "HEAD" not in line
            ]

            # 获取 tags
            tags_output = self._run_git_command(["tag", "-l"])
            tags = [line.strip() for line in tags_output.strip().split("\n") if line.strip()]

            # 如果有 pattern，过滤结果
            if pattern:
                pattern_lower = pattern.lower()
                branches = [b for b in branches if pattern_lower in b.lower()]
                tags = [t for t in tags if pattern_lower in t.lower()]

            logger.info(f"list_refs: pattern={pattern}, branches={len(branches)}, tags={len(tags)}")
            return {
                "branches": branches[:20],  # 限制返回数量
                "tags": tags[:20],
                "default_ref": self.default_ref
            }
        except Exception as e:
            logger.error(f"list_refs 失败: {e}")
            return {"branches": [], "tags": [], "error": str(e)}

    def search_code(
        self,
        query: str,
        ref: Optional[str] = None,
        path_filter: Optional[str] = None,
        max_results: int = 50
    ) -> Dict[str, Any]:
        """
        在代码中搜索关键词（git grep）

        Args:
            query: 搜索关键词
            ref: git 引用（分支/tag），为 None 时使用默认
            path_filter: 可选的路径过滤（如 "*.py" 只搜 Python 文件）
            max_results: 最大返回结果数

        Returns:
            搜索结果字典
        """
        ref = ref or self.default_ref

        # 安全检查
        if not self._validate_ref(ref):
            return {"error": f"无效的 ref: {ref}", "results": []}

        try:
            args = ["grep", "-n", "-i", query, ref]  # -n 显示行号，-i 忽略大小写
            if path_filter:
                args.append("--")
                args.append(path_filter)

            output = self._run_git_command(args)
            lines = output.strip().split("\n") if output.strip() else []

            # 解析结果：格式为 "ref:path:line_number:content"
            results = []
            for line in lines[:max_results]:
                parts = line.split(":", 3)
                if len(parts) >= 4:
                    results.append({
                        "file": parts[1],
                        "line": int(parts[2]),
                        "content": parts[3].strip()
                    })

            logger.info(f"search_code: query='{query}', ref={ref}, results={len(results)}")
            return {
                "query": query,
                "ref": ref,
                "results": results,
                "total": len(results),
                "truncated": len(lines) > max_results
            }
        except subprocess.CalledProcessError as e:
            # grep 未找到结果时返回 exit code 1
            if e.returncode == 1:
                logger.info(f"search_code: query='{query}', ref={ref}, no results")
                return {"query": query, "ref": ref, "results": [], "total": 0}
            logger.error(f"search_code 失败: {e}")
            return {"error": str(e), "results": []}
        except Exception as e:
            logger.error(f"search_code 异常: {e}")
            return {"error": str(e), "results": []}

    def read_file(
        self,
        path: str,
        ref: Optional[str] = None,
        start_line: Optional[int] = None,
        end_line: Optional[int] = None
    ) -> Dict[str, Any]:
        """
        读取文件内容（git show）

        Args:
            path: 文件路径（相对于仓库根目录）
            ref: git 引用，为 None 时使用默认
            start_line: 起始行号（1-based，包含），为 None 时从第一行开始
            end_line: 结束行号（1-based，包含），为 None 时到最后一行

        Returns:
            文件内容字典
        """
        ref = ref or self.default_ref

        # 安全检查
        if not self._validate_ref(ref):
            return {"error": f"无效的 ref: {ref}"}

        if not self._validate_path(path):
            return {"error": f"无效的路径: {path}"}

        try:
            output = self._run_git_command(["show", f"{ref}:{path}"])
            lines = output.split("\n")

            # 截取指定行范围
            if start_line is not None or end_line is not None:
                start = (start_line - 1) if start_line else 0
                end = end_line if end_line else len(lines)
                lines = lines[start:end]

            content = "\n".join(lines)
            logger.info(
                f"read_file: path={path}, ref={ref}, "
                f"lines={start_line or 1}-{end_line or len(lines)}, size={len(content)}"
            )

            return {
                "path": path,
                "ref": ref,
                "content": content,
                "start_line": start_line or 1,
                "end_line": end_line or len(lines),
                "total_lines": len(lines)
            }
        except subprocess.CalledProcessError as e:
            if "does not exist" in e.stderr or "Path" in e.stderr:
                logger.warning(f"read_file: 文件不存在: {path} @ {ref}")
                return {"error": f"文件不存在: {path}"}
            logger.error(f"read_file 失败: {e}")
            return {"error": str(e)}
        except Exception as e:
            logger.error(f"read_file 异常: {e}")
            return {"error": str(e)}

    def list_dir(self, path: str = "", ref: Optional[str] = None) -> Dict[str, Any]:
        """
        列出目录内容（git ls-tree）

        Args:
            path: 目录路径（相对于仓库根目录），空字符串表示根目录
            ref: git 引用，为 None 时使用默认

        Returns:
            目录内容字典
        """
        ref = ref or self.default_ref

        # 安全检查
        if not self._validate_ref(ref):
            return {"error": f"无效的 ref: {ref}"}

        if path and not self._validate_path(path):
            return {"error": f"无效的路径: {path}"}

        try:
            target = f"{ref}:{path}" if path else ref
            output = self._run_git_command(["ls-tree", "--name-only", target])
            entries = [line.strip() for line in output.strip().split("\n") if line.strip()]

            logger.info(f"list_dir: path={path or '/'}, ref={ref}, entries={len(entries)}")
            return {
                "path": path or "/",
                "ref": ref,
                "entries": entries
            }
        except subprocess.CalledProcessError as e:
            if "not a tree" in e.stderr:
                return {"error": f"不是目录: {path}"}
            logger.error(f"list_dir 失败: {e}")
            return {"error": str(e)}
        except Exception as e:
            logger.error(f"list_dir 异常: {e}")
            return {"error": str(e)}

    def fetch_updates(self) -> bool:
        """
        从远程拉取最新引用（定期调用以保持 ref 最新）

        Returns:
            是否成功
        """
        try:
            self._run_git_command(["fetch", "--all", "--tags", "--prune"], timeout=60)
            logger.info("Git fetch 完成")
            return True
        except Exception as e:
            logger.error(f"Git fetch 失败: {e}")
            return False


# Anthropic tool schema 定义（供 Provider 调用）
GIT_TOOLS_SCHEMA = [
    {
        "name": "list_refs",
        "description": "列出代码仓库的分支和标签，用于确认用户提到的版本号对应哪个分支或 tag。可选过滤模式匹配版本号。",
        "input_schema": {
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": "可选的过滤模式，如 '4.3' 会匹配包含 4.3 的分支/tag"
                }
            }
        }
    },
    {
        "name": "search_code",
        "description": "在指定分支/tag 的代码中搜索关键词（如异常类名、错误信息、函数名）。返回匹配的文件、行号和代码片段。",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "要搜索的关键词（支持正则表达式）"
                },
                "ref": {
                    "type": "string",
                    "description": "分支或 tag 名称（如 'release/4.3.6'），不指定则使用默认分支"
                },
                "path_filter": {
                    "type": "string",
                    "description": "可选的路径过滤（如 '*.java' 只搜 Java 文件）"
                }
            },
            "required": ["query"]
        }
    },
    {
        "name": "read_file",
        "description": "读取指定分支/tag 中某个文件的内容。可以指定行号范围，用于查看异常抛出位置的上下文代码。",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "文件路径（相对于仓库根目录）"
                },
                "ref": {
                    "type": "string",
                    "description": "分支或 tag 名称，不指定则使用默认分支"
                },
                "start_line": {
                    "type": "integer",
                    "description": "起始行号（1-based），不指定则从第一行开始"
                },
                "end_line": {
                    "type": "integer",
                    "description": "结束行号（1-based），不指定则到最后一行"
                }
            },
            "required": ["path"]
        }
    },
    {
        "name": "list_dir",
        "description": "列出指定分支/tag 中某个目录的内容，用于探索代码结构。",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "目录路径（相对于仓库根目录），空字符串表示根目录"
                },
                "ref": {
                    "type": "string",
                    "description": "分支或 tag 名称，不指定则使用默认分支"
                }
            }
        }
    }
]
