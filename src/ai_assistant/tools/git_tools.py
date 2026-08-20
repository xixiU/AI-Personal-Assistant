"""
Git 只读代码工具

基于 git 命令提供代码检索和浏览能力，所有操作都是只读的，不修改工作目录。
支持并发安全：多个线程可以同时查询不同分支的代码，互不干扰。
"""

import subprocess
import os
import re
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
                check=False,  # 不自动抛异常，让调用者处理退出码
                encoding='utf-8',
                errors='replace'  # 替换无法解码的字符，避免崩溃
            )
            # 检查退出码，但 grep 的1（没匹配）不算错误
            if result.returncode != 0:
                # git grep 返回1表示没匹配，属于正常情况
                if args[0] == "grep" and result.returncode == 1:
                    return ""  # 返回空字符串表示没结果
                # grep 返回 128 多为正则语法无效，调用方会退回字面量重试，
                # 这里不按 ERROR 记录，避免正常的重试流程看起来像故障
                if args[0] == "grep" and result.returncode == 128:
                    logger.debug(f"Git grep 正则无效: {' '.join(args)}, stderr={result.stderr}")
                else:
                    logger.error(f"Git 命令失败: {' '.join(args)}, returncode={result.returncode}, stderr={result.stderr}")
                raise subprocess.CalledProcessError(result.returncode, ["git"] + args, result.stdout, result.stderr)
            return result.stdout
        except subprocess.TimeoutExpired as e:
            logger.error(f"Git 命令超时: {' '.join(args)}")
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

    @staticmethod
    def _normalize_pathspec(path_filter: str) -> List[str]:
        """
        git pathspec 是从仓库根目录开始匹配的，裸文件名（如 "RecordList.vue"）
        只会匹配根目录下的同名文件，深层目录里的文件一个都匹配不到，且退出码为
        1（等同"没找到"），不会报错——这会让调用方误以为代码里没有该内容。

        因此同时给出原始写法和 `*` 前缀写法，两种都作为 pathspec 传入（git 对多个
        pathspec 取并集，匹配不到的那个不会报错）。

        Args:
            path_filter: 调用方给的路径过滤

        Returns:
            pathspec 列表
        """
        spec = path_filter.strip()
        specs = [spec]
        if not spec.startswith("*"):
            specs.append("*" + spec)
        # 末段不含 "." 时视为目录名，补一个 */dir/* 形式匹配目录下所有文件
        last_segment = spec.rstrip("/").rsplit("/", 1)[-1]
        if "." not in last_segment:
            specs.append("*" + spec.rstrip("/") + "/*")
        return specs

    def _grep_with_fallback(self, args_prefix: List[str], query: str, ref: str,
                            pathspecs: List[str]) -> tuple:
        """
        先按正则（-E）搜索，若 git 报"坏正则"（退出码 128）再退回字面量（-F）搜索。

        原实现按"是否含特殊字符"猜测，导致 'class.*Service' 这类真正的正则被当作
        字面量搜索，永远匹配不到内容却只返回"无结果"。

        Returns:
            (输出文本, 实际使用的模式)
        """
        for mode in ("-E", "-F"):
            args = ["grep", mode] + args_prefix + [query, ref]
            if pathspecs:
                args += ["--"] + pathspecs
            try:
                return self._run_git_command(args), mode
            except subprocess.CalledProcessError as e:
                # 128 = 正则语法错误；此时退回字面量再试一次
                if e.returncode == 128 and mode == "-E":
                    logger.info(f"正则搜索失败，退回字面量搜索: query='{query}'")
                    continue
                raise
        return "", "-F"

    def search_code(
        self,
        query: str,
        ref: Optional[str] = None,
        path_filter: Optional[str] = None,
        max_results: int = 50,
        context: int = 0
    ) -> Dict[str, Any]:
        """
        在代码中搜索关键词（git grep）

        Args:
            query: 搜索关键词（支持正则，失败自动退回字面量）
            ref: git 引用（分支/tag），为 None 时使用默认
            path_filter: 可选的路径过滤（如 "*.py"；裸文件名会自动补全匹配）
            max_results: 最大返回结果数
            context: 每个匹配额外返回的上下文行数（0 表示只返回匹配行）

        Returns:
            搜索结果字典
        """
        ref = ref or self.default_ref

        # 安全检查
        if not self._validate_ref(ref):
            return {"error": f"无效的 ref: {ref}", "results": []}

        try:
            args_prefix = ["-n", "-i"]
            if context > 0:
                args_prefix += ["-C", str(context)]

            pathspecs = self._normalize_pathspec(path_filter) if path_filter else []
            output, mode = self._grep_with_fallback(args_prefix, query, ref, pathspecs)

            if not output:
                logger.info(f"search_code: query='{query}', ref={ref}, no results")
                return {"query": query, "ref": ref, "results": [], "total": 0}

            lines = output.strip().split("\n") if output.strip() else []

            # 解析结果。git grep 对匹配行用 ":" 分隔，对上下文行用 "-" 分隔：
            #   匹配行：  ref:path:line:content
            #   上下文行：ref:path-line-content
            # 上下文行里的 path 可能含 "-"，所以借上一处匹配已知的 path 来切分。
            results = []
            truncated = False
            pending_context = []  # 出现在匹配行之前的上下文，先缓存再挂到下一个匹配
            last_file = None

            for line in lines:
                if not line or line == "--":
                    continue

                parts = line.split(":", 3)
                is_match = len(parts) >= 4 and parts[2].isdigit()

                if is_match:
                    if len(results) >= max_results:
                        truncated = True
                        break
                    last_file = parts[1]
                    item = {
                        "file": last_file,
                        "line": int(parts[2]),
                        "content": parts[3].strip()
                    }
                    if pending_context:
                        item["context"] = pending_context
                        pending_context = []
                    results.append(item)
                    continue

                if context <= 0:
                    continue

                # 上下文行：去掉 "ref:" 前缀后形如 "path-行号-内容"
                body = line.split(":", 1)[1] if ":" in line else ""
                ctx_match = re.match(r'^(.*?)-(\d+)-(.*)$', body)
                if not ctx_match:
                    continue

                ctx_file, ctx_line, ctx_text = ctx_match.groups()
                entry = f"{ctx_line}: {ctx_text}"
                # 属于当前匹配的后置上下文，否则是下一个匹配的前置上下文
                if results and ctx_file == results[-1]["file"] and \
                        int(ctx_line) > results[-1]["line"]:
                    results[-1].setdefault("context", []).append(entry)
                else:
                    pending_context.append(entry)

            logger.info(
                f"search_code: query='{query}', ref={ref}, mode={mode}, "
                f"results={len(results)}"
            )
            return {
                "query": query,
                "ref": ref,
                "results": results,
                "total": len(results),
                "truncated": truncated
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

    def find_files(
        self,
        name_pattern: str,
        ref: Optional[str] = None,
        max_results: int = 100
    ) -> Dict[str, Any]:
        """
        按文件名查找文件（git ls-files），返回完整路径。

        用于"只知道文件名、不知道在哪个目录"的场景。拿到完整路径后再配合
        read_file 或 search_code 的 path_filter 使用，避免反复 list_dir 试探。

        Args:
            name_pattern: 文件名或片段（如 "RecordList.vue" / "DataCenter"）
            ref: git 引用，为 None 时使用默认
            max_results: 最大返回数量

        Returns:
            匹配的文件路径列表
        """
        ref = ref or self.default_ref

        if not self._validate_ref(ref):
            return {"error": f"无效的 ref: {ref}", "files": []}

        try:
            pattern = name_pattern.strip()
            # 两头加 * 以支持"文件名片段"匹配，且能跨目录层级
            if not pattern.startswith("*"):
                pattern = "*" + pattern
            if not pattern.endswith("*") and "." not in pattern.rsplit("/", 1)[-1]:
                pattern = pattern + "*"

            output = self._run_git_command(
                ["ls-files", f"--with-tree={ref}", "--", pattern]
            )
            files = [line.strip() for line in output.strip().split("\n") if line.strip()]

            logger.info(
                f"find_files: pattern='{name_pattern}', ref={ref}, files={len(files)}"
            )
            return {
                "name_pattern": name_pattern,
                "ref": ref,
                "files": files[:max_results],
                "total": len(files),
                "truncated": len(files) > max_results
            }
        except subprocess.CalledProcessError as e:
            logger.error(f"find_files 失败: {e}")
            return {"error": str(e), "files": []}
        except Exception as e:
            logger.error(f"find_files 异常: {e}")
            return {"error": str(e), "files": []}

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

    def run_git_command(self, args: List[str]) -> Dict[str, Any]:
        """
        执行任意只读 git 命令（白名单控制）

        Args:
            args: git 命令参数列表（不包括 'git' 本身），如 ['log', '--oneline', '-10']

        Returns:
            包含 output 和 error 的字典
        """
        # 只读命令白名单（排除所有写操作）
        readonly_commands = {
            'log', 'show', 'diff', 'blame', 'annotate',
            'ls-files', 'ls-tree', 'cat-file',
            'rev-parse', 'rev-list', 'describe',
            'branch', 'tag', 'reflog',
            'grep', 'status', 'remote',
        }

        if not args or args[0] not in readonly_commands:
            return {"error": f"不允许的命令: {args[0] if args else '(空)'}，仅支持只读命令: {', '.join(sorted(readonly_commands))}"}

        try:
            output = self._run_git_command(args, timeout=60)
            logger.info(f"run_git_command: {' '.join(args)}, output_lines={len(output.splitlines())}")
            return {
                "command": ' '.join(args),
                "output": output,
                "lines": len(output.splitlines())
            }
        except subprocess.CalledProcessError as e:
            logger.error(f"run_git_command 失败: {' '.join(args)}, stderr={e.stderr}")
            return {"error": f"命令执行失败: {e.stderr}", "command": ' '.join(args)}
        except Exception as e:
            logger.error(f"run_git_command 异常: {e}")
            return {"error": str(e), "command": ' '.join(args)}

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
        "name": "run_git_command",
        "description": "执行只读 git 命令查询代码历史、变更、作者等信息。支持: log(查历史), blame(查作者), diff(查变更), show(查提交), describe(查版本), 等。示例: ['log', '--oneline', '-10', 'pom.xml'] 查最近10次提交; ['blame', '-L', '100,120', 'pom.xml'] 查100-120行作者; ['log', '-p', '--all', '--', 'pom.xml'] 查文件所有历史。",
        "input_schema": {
            "type": "object",
            "properties": {
                "args": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "git 命令参数列表（不含'git'本身）。只支持只读命令：log, show, diff, blame, annotate, ls-files, ls-tree, cat-file, rev-parse, rev-list, describe, branch, tag, reflog, grep, status, remote。"
                }
            },
            "required": ["args"]
        }
    },
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
        "description": "在指定分支/tag 的代码中搜索关键词（如异常类名、错误信息、函数名）。返回匹配的文件、行号和代码片段。支持正则（若正则语法无效会自动退回字面量搜索）。用 context 参数可同时拿到匹配行周围的代码，省去再调 read_file。",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "要搜索的关键词，支持正则表达式（如 'class.*Service'）"
                },
                "ref": {
                    "type": "string",
                    "description": "分支或 tag 名称（如 'release/4.3.6'），不指定则使用默认分支"
                },
                "path_filter": {
                    "type": "string",
                    "description": "可选的路径过滤。可用通配符（'*.java'）、目录（'src/main/java'）或直接给文件名（'RecordList.vue'，会自动匹配任意目录下的该文件）"
                },
                "context": {
                    "type": "integer",
                    "description": "每个匹配额外返回的上下文行数（如 5 表示前后各5行）。想看匹配点周围代码时用它，比再调一次 read_file 更快"
                }
            },
            "required": ["query"]
        }
    },
    {
        "name": "find_files",
        "description": "按文件名（或文件名片段）查找文件的完整路径。当你只知道文件名、不知道它在哪个目录时用这个，不要用 list_dir 一层层试探。拿到完整路径后再 read_file 或作为 search_code 的 path_filter。",
        "input_schema": {
            "type": "object",
            "properties": {
                "name_pattern": {
                    "type": "string",
                    "description": "文件名或片段，如 'RecordList.vue'、'DataCenterService'"
                },
                "ref": {
                    "type": "string",
                    "description": "分支或 tag 名称，不指定则使用默认分支"
                }
            },
            "required": ["name_pattern"]
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
