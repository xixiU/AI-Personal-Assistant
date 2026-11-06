"""
日志附件只读工具

供 Agentic 模式下的 AI 在【本次调用附带的日志附件】里按关键词搜索、按行号读取。
用户发来的大日志（几十 MB）和 zip 包只内联开头或只给文件清单，没有这组工具时
AI 读不到正文，只能凭空作答。

安全边界（改动前务必读完）：
- 可读范围 = 构造时传入的白名单（本次调用 messages 里 log_file 的 path），工具
  不自己扫描 log_files 目录。该目录按会话分子目录存着其他群/用户的日志，AI 可能
  被日志或提问里的注入文本诱导去读别人的文件。
- 白名单由调用方按调用构造、以局部变量传入，**不要挂到共享的 provider 实例上**：
  provider 是单例且被线程池并发调用，挂上去会让并发的另一个群读到本群白名单。
- 路径只接受绝对路径、拒绝 ".."，resolve() 之后再比对：文件根精确相等，目录根
  用 is_relative_to（不能用字符串前缀，xxx_unzipped2 会被当成 xxx_unzipped 的子路径）。
  目录内每个文件都单独 resolve 复核，指向白名单外的符号链接/junction 会被丢弃。
- 只读：只有 open(..., "rb")，没有写、删、执行。
- 服务端日志只记工具名、文件数、关键词个数、命中条数、行数、耗时。关键词（常是
  案号等用户数据）和命中正文一律不记，连指纹也不记：案号空间很小，8 位 sha1
  可以离线枚举还原。
"""

import os
import time
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional, Tuple

from loguru import logger

from ai_assistant.utils.text_stream import clip_line, decode_line, is_binary, iter_chunks

# ============ 输出与资源上限 ============

MAX_HITS = 50                 # 单次搜索最多返回的命中条数（超出只计数）
MAX_KEYWORDS = 10             # 单次搜索最多关键词个数
MIN_KEYWORD_LEN = 2           # 关键词最短长度，防止单字符把整份日志全部命中
MAX_LINE_CHARS = 500          # 单行最多返回的字符数（以命中位置为中心截取）
MAX_READ_LINES = 200          # 单次读行最多返回的行数
MAX_LIST_FILES = 200          # 列出/搜索的文件个数上限（zip 解压条目上限同为 200）
SEARCH_TIME_BUDGET_S = 20.0   # 单次搜索的时间预算，须小于并行工具的 30s 等待上限
BINARY_SNIFF_BYTES = 8192     # 文件头含 NUL 字节即视为二进制，不读
READ_CHUNK_BYTES = 1 << 20    # 单次 readline 的字节上限：无换行的大文件也不会整体进内存
_DEADLINE_CHECK_EVERY = 10000  # 每扫多少块检查一次时间预算

LOG_TOOL_NAMES = frozenset({"list_log_files", "search_log", "read_log_lines"})

# 附件说明里点名日志工具的提示（Anthropic / OpenAI 两个通道共用，措辞只维护这一份）。
# 只能在本轮确实提供了日志工具时注入，否则是对 AI 说假话。
LOG_TOOLS_ATTACHMENT_HINT = (
    "（可用 search_log 在该日志中按关键词搜索、read_log_lines 读取指定行，"
    "list_log_files 列出可读文件。大日志或压缩包请先按用户提问里的关键词"
    "（案号、单号、traceId、报错类名、时间点）搜索，再读命中行前后的上下文，"
    "不要只看下面内联的开头部分。）"
)

_OUT_OF_SCOPE = "路径不在本轮日志附件范围内（或文件不存在）"


def collect_log_paths(messages: Iterable[Any]) -> List[str]:
    """
    从消息列表里收集所有 log_file 内容块的落地路径，作为日志工具的白名单。

    不区分 role、不区分是不是当前轮：log_file 只由服务端的附件落地流程生成
    （飞书 _collect_log_files / Web _collect_web_log_files），用户文本伪造不出来；
    引用回查到的附件已并入当前轮，Web 端的历史轮来自同一会话的内存上下文。

    Args:
        messages: Message 列表

    Returns:
        非空 path 列表（保持出现顺序、去重）
    """
    paths: List[str] = []
    for msg in messages or []:
        for content in getattr(msg, "content", None) or []:
            if getattr(content, "type", "") != "log_file":
                continue
            data = getattr(content, "data", None)
            if not isinstance(data, dict):
                continue
            path = data.get("path") or ""
            if isinstance(path, str) and path and path not in paths:
                paths.append(path)
    return paths


def collect_current_turn_log_paths(messages: List[Any]) -> List[str]:
    """
    只收"当前这一轮"（末尾连续的 user 消息）里 log_file 的落地路径。

    用于判断"无 Git 工具时要不要进工具模式"：历史轮带过日志不应让后续每轮
    追问都进工具模式。各 Provider 共用这一份判定，不要各自再写一遍。

    Args:
        messages: Message 列表

    Returns:
        非空 path 列表（保持出现顺序、去重）
    """
    tail = []
    for msg in reversed(messages or []):
        if getattr(msg, "role", None) != "user":
            break
        tail.append(msg)
    tail.reverse()
    return collect_log_paths(tail)


# 以下四个是 utils/text_stream.py 的薄包装：常量在调用时读本模块的，
# 测试 monkeypatch lt.READ_CHUNK_BYTES 等仍然生效。

def _iter_chunks(path: Path) -> Iterator[Tuple[int, bytes]]:
    """逐行流式产出 (行号, 原始字节)，单块不超过 READ_CHUNK_BYTES"""
    return iter_chunks(path, READ_CHUNK_BYTES)


_decode = decode_line


def _is_binary(path: Path) -> bool:
    """文件头含 NUL 字节视为二进制（如解压失败时退回的 .zip 本身）"""
    return is_binary(path, BINARY_SNIFF_BYTES)


def _clip(text: str, pos: int = 0) -> str:
    """把超长行截到 MAX_LINE_CHARS，截取窗口以 pos 为中心，保证命中点可见"""
    return clip_line(text, MAX_LINE_CHARS, pos)


def _normalize_keywords(keywords: Any) -> Tuple[List[str], str]:
    """校验并去重关键词，返回 (关键词列表, 错误信息)"""
    if isinstance(keywords, str):
        # 容错：模型偶尔把单个关键词直接传成字符串
        keywords = [keywords]
    if not isinstance(keywords, list):
        return [], "keywords 必须是字符串数组"
    kws: List[str] = []
    for k in keywords:
        if isinstance(k, str):
            k = k.strip()
            if k and k not in kws:
                kws.append(k)
    if not kws:
        return [], "keywords 不能为空"
    if len(kws) > MAX_KEYWORDS:
        return [], f"关键词最多 {MAX_KEYWORDS} 个，请分批搜索"
    if any(len(k) < MIN_KEYWORD_LEN for k in kws):
        return [], f"每个关键词至少 {MIN_KEYWORD_LEN} 个字符"
    return kws, ""


def _to_int(value: Any) -> Optional[int]:
    """把模型传来的行号（可能是 int / 数字字符串）转成 int，非法返回 None"""
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


class LogTools:
    """日志附件只读工具集（白名单在构造时固定）"""

    def __init__(self, allowed_paths: Iterable[str]):
        """
        Args:
            allowed_paths: 允许读取的路径（日志文件，或 zip 解压目录）。
                不存在的路径直接丢弃。
        """
        self._files: List[Path] = []
        self._dirs: List[Path] = []
        for raw in allowed_paths or []:
            if not isinstance(raw, str) or not raw:
                continue
            try:
                p = Path(raw).resolve(strict=True)
            except (OSError, RuntimeError):
                continue
            if p.is_dir():
                if p not in self._dirs:
                    self._dirs.append(p)
            elif p.is_file():
                if p not in self._files:
                    self._files.append(p)

    @property
    def has_roots(self) -> bool:
        """白名单里是否至少有一个仍存在的文件或目录"""
        return bool(self._files or self._dirs)

    # ============ 路径校验 ============

    def _is_allowed(self, p: Path) -> bool:
        """p 必须是已 resolve 的路径"""
        if p in self._files:
            return True
        return any(p == d or p.is_relative_to(d) for d in self._dirs)

    def _resolve_allowed(self, path: Any) -> Tuple[Optional[Path], str]:
        """
        把 AI 传来的路径解析成白名单内的真实路径。

        越界和不存在返回同一句错误，不给"白名单外某文件是否存在"当探针。

        Returns:
            (解析后的路径, 错误信息)
        """
        if not isinstance(path, str) or not path.strip():
            return None, "path 不能为空"
        raw = Path(path.strip())
        if not raw.is_absolute():
            return None, "只接受绝对路径，请使用 list_log_files 返回的完整路径"
        if ".." in raw.parts:
            return None, "路径中不允许出现 .."
        try:
            p = raw.resolve(strict=True)
        except (OSError, RuntimeError):
            return None, _OUT_OF_SCOPE
        if not self._is_allowed(p):
            return None, _OUT_OF_SCOPE
        return p, ""

    def _allowed_files(self, base: Optional[Path] = None) -> Tuple[List[Path], bool]:
        """
        列出范围内的文本文件。

        目录内每个文件都重新 resolve 并复核白名单：os.walk 不跟随符号链接目录，
        但会列出符号链接文件，Windows 上还会走进 junction，单靠遍历方式挡不住越界。

        Args:
            base: 只列这个文件/目录（须已通过 _resolve_allowed）；None 表示全部白名单

        Returns:
            (文件列表, 是否因 MAX_LIST_FILES 被截断)
        """
        targets = [base] if base is not None else self._files + self._dirs
        out: List[Path] = []
        seen = set()
        for target in targets:
            if target.is_file():
                candidates: Iterable[Path] = [target]
            else:
                candidates = self._walk(target)
            for cand in candidates:
                try:
                    r = cand.resolve(strict=True)
                except (OSError, RuntimeError):
                    continue
                if r in seen or not r.is_file() or not self._is_allowed(r):
                    continue
                if _is_binary(r):
                    continue
                seen.add(r)
                out.append(r)
                if len(out) >= MAX_LIST_FILES:
                    return out, True
        return out, False

    @staticmethod
    def _walk(root: Path) -> Iterator[Path]:
        """按字典序遍历目录下的所有文件（不跟随符号链接目录）"""
        for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
            dirnames.sort()
            for name in sorted(filenames):
                yield Path(dirpath) / name

    # ============ 工具实现 ============

    def list_files(self) -> Dict[str, Any]:
        """列出本轮可读的日志文件"""
        files, capped = self._allowed_files()
        items = []
        for f in files:
            try:
                size_kb = round(f.stat().st_size / 1024, 1)
            except OSError:
                size_kb = None
            items.append({"path": str(f), "size_kb": size_kb})
        result: Dict[str, Any] = {"files": items, "total": len(items)}
        if capped:
            result["capped"] = True
            result["hint"] = f"文件数超过 {MAX_LIST_FILES}，只列出前 {MAX_LIST_FILES} 个"
        return result

    def search(self, keywords: Any, path: Any = None, ignore_case: bool = False) -> Dict[str, Any]:
        """
        在日志里按字面子串搜索，任一关键词命中即算该行命中。

        Args:
            keywords: 关键词列表
            path: 只搜这个文件/目录；为空时搜全部可读文件
            ignore_case: 是否忽略大小写

        Returns:
            按文件分组的命中结果；超出 MAX_HITS 的只计数
        """
        kws, err = _normalize_keywords(keywords)
        if err:
            return {"error": err}
        base = None
        if path:
            base, err = self._resolve_allowed(path)
            if err:
                return {"error": err, "allowed": [str(p) for p in self._files + self._dirs]}

        files, files_capped = self._allowed_files(base)
        needles = [k.lower() for k in kws] if ignore_case else kws

        grouped: Dict[str, List[Dict[str, Any]]] = {}
        total = shown = searched = 0
        incomplete = False
        deadline = time.monotonic() + SEARCH_TIME_BUDGET_S

        for f in files:
            if time.monotonic() > deadline:
                incomplete = True
                break
            last_hit_line = 0
            try:
                for n, (line_no, raw) in enumerate(_iter_chunks(f)):
                    if n % _DEADLINE_CHECK_EVERY == 0 and n and time.monotonic() > deadline:
                        incomplete = True
                        break
                    if line_no == last_hit_line:
                        continue  # 超长行的后续块：该行已计过一次
                    text = _decode(raw).rstrip("\r\n")
                    hay = text.lower() if ignore_case else text
                    positions = [p for p in (hay.find(k) for k in needles) if p >= 0]
                    if not positions:
                        continue
                    last_hit_line = line_no
                    total += 1
                    if shown < MAX_HITS:
                        grouped.setdefault(str(f), []).append(
                            {"line": line_no, "text": _clip(text, min(positions))}
                        )
                        shown += 1
            except OSError:
                continue
            searched += 1
            if incomplete:
                break

        result: Dict[str, Any] = {
            "files_searched": searched,
            "total_hits": total,
            "shown": shown,
            "results": [{"file": k, "hits": v} for k, v in grouped.items()],
        }
        if total > shown:
            result["omitted"] = total - shown
            result["hint"] = (
                f"还有 {total - shown} 条命中未显示。请换更具体的关键词"
                f"（完整案号/单号/traceId/时间点），或用 path 限定文件"
            )
        if incomplete:
            result["incomplete"] = True
            result["incomplete_hint"] = (
                f"超过 {SEARCH_TIME_BUDGET_S:.0f}s 时间预算，只扫了部分内容，"
                f"结果不完整。请用 path 限定文件或换更具体的关键词再搜"
            )
        if files_capped:
            result["files_capped"] = True
        if not files:
            result["hint"] = "范围内没有可读的文本日志文件"
        return result

    def read_lines(self, path: Any, start_line: Any, end_line: Any = None) -> Dict[str, Any]:
        """
        读取日志文件指定行范围（1-based，含首尾）。

        Returns:
            "行号| 内容" 形式的文本；未到文件末尾时给出 next_start_line
        """
        p, err = self._resolve_allowed(path)
        if err:
            return {"error": err, "allowed": [str(x) for x in self._files + self._dirs]}
        if p.is_dir():
            return {"error": "这是目录，请先用 list_log_files 或 search_log 找到具体文件"}
        if _is_binary(p):
            return {"error": "该文件是二进制文件，不可按行读取"}

        start = _to_int(start_line)
        if start is None or start < 1:
            return {"error": "start_line 必须是 >= 1 的整数"}
        if end_line is None:
            end = start + MAX_READ_LINES - 1
        else:
            end = _to_int(end_line)
            if end is None or end < start:
                return {"error": "end_line 必须是 >= start_line 的整数"}
        capped = end - start + 1 > MAX_READ_LINES
        if capped:
            end = start + MAX_READ_LINES - 1

        lines: List[Tuple[int, str]] = []
        eof = True
        for line_no, raw in _iter_chunks(p):
            if line_no < start:
                continue
            if line_no > end:
                eof = False
                break
            if lines and lines[-1][0] == line_no:
                continue  # 超长行的后续块：首块已超过 MAX_LINE_CHARS，截断展示即可
            lines.append((line_no, _clip(_decode(raw).rstrip("\r\n"))))

        if not lines:
            return {"file": str(p), "content": "", "eof": True, "hint": "start_line 超出文件总行数"}

        result: Dict[str, Any] = {
            "file": str(p),
            "start_line": lines[0][0],
            "end_line": lines[-1][0],
            "content": "\n".join(f"{n}| {t}" for n, t in lines),
            "eof": eof,
        }
        if not eof:
            result["next_start_line"] = lines[-1][0] + 1
        if capped:
            result["hint"] = f"单次最多读取 {MAX_READ_LINES} 行，已截到第 {end} 行"
        return result

    def execute(self, tool_name: str, tool_input: Any) -> Dict[str, Any]:
        """
        工具分发入口：不抛异常，任何失败都转成 {"error": ...} 返回给 AI。

        Args:
            tool_name: LOG_TOOL_NAMES 之一
            tool_input: 模型给的参数（不保证是 dict）

        Returns:
            工具结果字典
        """
        started = time.monotonic()
        args = tool_input if isinstance(tool_input, dict) else {}
        try:
            if tool_name == "list_log_files":
                result = self.list_files()
            elif tool_name == "search_log":
                result = self.search(
                    args.get("keywords"),
                    path=args.get("path"),
                    ignore_case=bool(args.get("ignore_case", False)),
                )
            elif tool_name == "read_log_lines":
                result = self.read_lines(
                    args.get("path"), args.get("start_line"), args.get("end_line")
                )
            else:
                result = {"error": f"未知日志工具: {tool_name}"}
        except Exception as e:
            # 异常文本可能带路径（含会话 ID 与文件名），服务端只记类型
            logger.warning(f"日志工具 {tool_name} 执行异常: {type(e).__name__}")
            result = {"error": f"日志工具执行失败: {type(e).__name__}"}
        self._log_call(tool_name, args, result, time.monotonic() - started)
        return result

    @staticmethod
    def _log_call(tool_name: str, args: Dict[str, Any], result: Dict[str, Any], elapsed: float) -> None:
        """只记规模与耗时，不记关键词、路径与命中正文（见模块 docstring）"""
        parts = [f"tool={tool_name}"]
        if tool_name == "search_log":
            kws = args.get("keywords")
            parts.append(f"keywords={len(kws) if isinstance(kws, list) else 1 if kws else 0}")
            parts.append(f"files={result.get('files_searched', 0)}")
            parts.append(f"hits={result.get('total_hits', 0)}")
            parts.append(f"shown={result.get('shown', 0)}")
            if result.get("incomplete"):
                parts.append("incomplete=1")
        elif tool_name == "read_log_lines":
            content = result.get("content") or ""
            parts.append(f"lines={content.count(chr(10)) + 1 if content else 0}")
        elif tool_name == "list_log_files":
            parts.append(f"files={result.get('total', 0)}")
        if "error" in result:
            parts.append("error=1")
        parts.append(f"elapsed_ms={int(elapsed * 1000)}")
        logger.info("日志工具调用: " + ", ".join(parts))


# Anthropic tool schema 定义（供 Provider 调用，风格同 GIT_TOOLS_SCHEMA）
LOG_TOOLS_SCHEMA = [
    {
        "name": "list_log_files",
        "description": "列出用户本轮上传的、可读取的日志文件（含 zip 解压出的全部文本文件，包括 debug.log.1 这类轮转文件），返回完整路径和大小。zip 包或不确定有哪些文件时先调它。",
        "input_schema": {"type": "object", "properties": {}}
    },
    {
        "name": "search_log",
        "description": f"在用户上传的日志里按关键词搜索（字面子串匹配，不支持正则），多个关键词任一命中即返回该行。返回文件路径、行号和该行内容，最多 {MAX_HITS} 条，超出时会告诉你还剩多少条。大日志和 zip 包请先用它定位，再用 read_log_lines 读命中行前后的上下文。",
        "input_schema": {
            "type": "object",
            "properties": {
                "keywords": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": f"关键词列表（1~{MAX_KEYWORDS} 个，每个至少 {MIN_KEYWORD_LEN} 个字符），如案号、单号、traceId、异常类名、时间点 '2026-11-05 10:2'"
                },
                "path": {
                    "type": "string",
                    "description": "可选，只搜这个文件或目录（用 list_log_files 或日志附件说明里给出的完整路径）；不填则搜全部日志附件"
                },
                "ignore_case": {
                    "type": "boolean",
                    "description": "是否忽略大小写，默认 false"
                }
            },
            "required": ["keywords"]
        }
    },
    {
        "name": "read_log_lines",
        "description": f"读取某个日志文件的指定行范围（行号从 1 开始），单次最多 {MAX_READ_LINES} 行，超长行截断到 {MAX_LINE_CHARS} 字符。用于查看 search_log 命中行前后的上下文，例如命中第 1200 行就读 1170~1260。",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "日志文件完整路径（来自 search_log / list_log_files 的结果）"
                },
                "start_line": {
                    "type": "integer",
                    "description": "起始行号（1-based）"
                },
                "end_line": {
                    "type": "integer",
                    "description": f"结束行号（含），不填则读 {MAX_READ_LINES} 行"
                }
            },
            "required": ["path", "start_line"]
        }
    }
]
