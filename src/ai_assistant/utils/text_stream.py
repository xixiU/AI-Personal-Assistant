"""
大文本文件的流式读取与逐行解码（只依赖标准库）

从 tools/log_tools.py 下沉而来：本地文档关键词直查（utils/local_doc_grep.py）
也要读几 MB 的 SQL，但导入 ai_assistant.tools.log_tools 会先执行
tools/__init__.py → operations_commands → paramiko，只装了 OpenAI 依赖的部署
没有 paramiko。log_tools 保留同名薄包装，传入它自己模块的常量，原有行为不变。
"""

from pathlib import Path
from typing import Iterator, Tuple, Union

PathLike = Union[str, Path]


def iter_chunks(path: PathLike, chunk_bytes: int) -> Iterator[Tuple[int, bytes]]:
    """
    逐行流式产出 (行号, 原始字节)。

    超过 chunk_bytes 的行切成多块产出，各块行号相同。不用 for line in f：
    没有换行的 50MB 文件会被当成一行整体读进内存。
    """
    line_no = 1
    with open(path, "rb") as f:
        while True:
            chunk = f.readline(chunk_bytes)
            if not chunk:
                return
            yield line_no, chunk
            if chunk.endswith(b"\n"):
                line_no += 1


def decode_line(raw: bytes) -> str:
    """
    逐行解码：先 UTF-8，失败按 GB18030（GBK 超集），再不行替换非法字节。

    逐行而不是按文件头嗅探一次：混合编码的日志（前半段纯 ASCII、后半段 GBK）
    按文件头判定会把后半段全部解成乱码，中文关键词就搜不到了。
    """
    for encoding in ("utf-8", "gb18030"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def is_binary(path: PathLike, sniff_bytes: int) -> bool:
    """文件头含 NUL 字节视为二进制（如解压失败时退回的 .zip 本身）"""
    try:
        with open(path, "rb") as f:
            return b"\x00" in f.read(sniff_bytes)
    except OSError:
        return True


def clip_line(text: str, max_chars: int, pos: int = 0) -> str:
    """把超长行截到 max_chars，截取窗口以 pos 为中心，保证命中点可见"""
    if len(text) <= max_chars:
        return text
    start = max(0, min(pos - max_chars // 2, len(text) - max_chars))
    end = start + max_chars
    return (
        ("…" if start > 0 else "")
        + text[start:end]
        + ("…" if end < len(text) else "")
        + "[行过长，已截断]"
    )
