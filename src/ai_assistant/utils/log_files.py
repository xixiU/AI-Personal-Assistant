"""
日志文件附件处理（用户上传 .log/.txt 或 zip 日志包）

用途：用户在飞书上传日志文件追问时，把文件落地到本地目录，小文件正文直接注入
AI 上下文，大文件/压缩包只告知 AI 本地路径，让它用代码排查工具自行读取。
带日志文件的消息会自动进入代码排查（Agentic）模式。

设计要点：
- 只接受白名单扩展名，避免把可执行文件、办公文档等无关附件拉下来。
- 单文件大小上限（默认 20MB），超限跳过并在上下文中说明，不静默丢弃。
- zip 解压做了防护：拒绝绝对路径与 ../ 穿越条目（zip slip），限制解压总量与条目数。
- 按会话分子目录存放，便于定位与整体清理。
- 定时清理（默认每天 23:00 清理超过 24 小时的文件），避免磁盘无限增长。
"""
import os
import re
import shutil
import threading
import time
import zipfile
from datetime import datetime
from typing import List, Optional, Tuple

from loguru import logger

# 默认允许的扩展名：以文本日志为主，zip 用于整包日志
_DEFAULT_ALLOWED_EXT = [".log", ".txt", ".out", ".err", ".zip"]

# zip 解压防护上限
_ZIP_MAX_ENTRIES = 200
_ZIP_MAX_TOTAL_BYTES = 200 * 1024 * 1024  # 解压后总量上限 200MB，防解压炸弹

# 文件名清洗：只保留常见安全字符，避免路径穿越与命令注入风险
_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._\-一-龥]")


def sanitize_filename(name: str, fallback: str = "attachment") -> str:
    """
    清洗文件名：去掉目录分隔符与特殊字符，仅保留字母数字中文点横线下划线。

    Args:
        name: 原始文件名
        fallback: 清洗后为空时使用的兜底名

    Returns:
        安全的文件名（不含路径分隔符）
    """
    base = os.path.basename(name or "")
    cleaned = _SAFE_NAME_RE.sub("_", base).strip("._") or fallback
    return cleaned[:120]


class LogFileManager:
    """日志文件附件的落地、读取与定时清理"""

    def __init__(
        self,
        enabled: bool = True,
        base_dir: str = "./data/log_files",
        max_size_mb: int = 20,
        inline_chars: int = 20000,
        cleanup_hour: int = 23,
        retention_hours: int = 24,
        allowed_ext: Optional[List[str]] = None,
    ):
        """
        Args:
            enabled: 是否启用日志文件处理
            base_dir: 落地根目录
            max_size_mb: 单文件大小上限（MB）
            inline_chars: 直接注入 AI 的文本字符上限，超出只给路径
            cleanup_hour: 每日清理时刻（0-23）
            retention_hours: 文件保留时长（小时）
            allowed_ext: 允许的扩展名列表（None 用内置默认）
        """
        self.enabled = enabled
        self.base_dir = os.path.abspath(base_dir)
        self.max_size_bytes = max(1, int(max_size_mb)) * 1024 * 1024
        self.inline_chars = max(0, int(inline_chars))
        self.cleanup_hour = max(0, min(23, int(cleanup_hour)))
        self.retention_seconds = max(1, int(retention_hours)) * 3600
        self.allowed_ext = [
            e.lower() if e.startswith(".") else f".{e.lower()}"
            for e in (allowed_ext or _DEFAULT_ALLOWED_EXT)
        ]
        self._cleanup_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()

        if self.enabled:
            os.makedirs(self.base_dir, exist_ok=True)
            logger.info(
                f"日志文件处理已启用: dir={self.base_dir}, 单文件上限={max_size_mb}MB, "
                f"注入上限={self.inline_chars}字符, 每日{self.cleanup_hour}:00清理超过{retention_hours}小时的文件"
            )

    # ============ 扩展名与大小校验 ============

    def is_allowed(self, file_name: str) -> bool:
        """判断扩展名是否在白名单内"""
        ext = os.path.splitext(file_name or "")[1].lower()
        return ext in self.allowed_ext

    # ============ 落地 ============

    def save_bytes(self, session_id: str, file_name: str, data: bytes) -> Optional[str]:
        """
        把下载到的文件字节落地到会话子目录。

        Args:
            session_id: 会话 ID（用于分目录）
            file_name: 原始文件名
            data: 文件内容

        Returns:
            落地后的绝对路径；未启用/超限/写入失败时返回 None
        """
        if not self.enabled or not data:
            return None
        if len(data) > self.max_size_bytes:
            logger.warning(
                f"日志文件超过大小上限，已跳过: name={file_name}, "
                f"size={len(data) / 1024 / 1024:.1f}MB, 上限={self.max_size_bytes / 1024 / 1024:.0f}MB"
            )
            return None

        safe_session = sanitize_filename(session_id, fallback="session")
        safe_name = sanitize_filename(file_name, fallback="attachment.log")
        target_dir = os.path.join(self.base_dir, safe_session)
        try:
            os.makedirs(target_dir, exist_ok=True)
            # 同名文件加时间戳后缀，避免多次上传互相覆盖
            path = os.path.join(target_dir, safe_name)
            if os.path.exists(path):
                stem, ext = os.path.splitext(safe_name)
                path = os.path.join(target_dir, f"{stem}_{int(time.time())}{ext}")
            with open(path, "wb") as f:
                f.write(data)
            logger.info(f"日志文件已落地: {path} ({len(data) / 1024:.1f}KB)")
            return path
        except Exception as e:
            logger.warning(f"日志文件落地失败: name={file_name}, error={e}")
            return None

    def extract_zip(self, zip_path: str) -> Tuple[Optional[str], List[str]]:
        """
        解压 zip 到同名子目录，带 zip slip 与解压炸弹防护。

        Args:
            zip_path: zip 文件路径

        Returns:
            (解压目录, 解压出的文本日志文件路径列表)；失败时 (None, [])
        """
        if not zipfile.is_zipfile(zip_path):
            logger.warning(f"不是有效的 zip 文件: {zip_path}")
            return None, []

        out_dir = os.path.splitext(zip_path)[0] + "_unzipped"
        extracted: List[str] = []
        try:
            os.makedirs(out_dir, exist_ok=True)
            out_root = os.path.abspath(out_dir)
            total = 0
            with zipfile.ZipFile(zip_path) as zf:
                infos = zf.infolist()
                if len(infos) > _ZIP_MAX_ENTRIES:
                    logger.warning(f"zip 条目过多({len(infos)})，只处理前 {_ZIP_MAX_ENTRIES} 个")
                    infos = infos[:_ZIP_MAX_ENTRIES]

                for info in infos:
                    if info.is_dir():
                        continue
                    # zip slip 防护：拒绝绝对路径与 ../ 穿越
                    name = info.filename.replace("\\", "/")
                    if name.startswith("/") or ".." in name.split("/"):
                        logger.warning(f"跳过可疑 zip 条目（路径穿越）: {info.filename}")
                        continue

                    total += info.file_size
                    if total > _ZIP_MAX_TOTAL_BYTES:
                        logger.warning("zip 解压总量超过上限，停止解压（疑似解压炸弹）")
                        break

                    target = os.path.abspath(os.path.join(out_root, name))
                    # 双重校验：解析后的目标必须仍在 out_root 内
                    if not target.startswith(out_root + os.sep) and target != out_root:
                        logger.warning(f"跳过越界 zip 条目: {info.filename}")
                        continue

                    os.makedirs(os.path.dirname(target), exist_ok=True)
                    with zf.open(info) as src, open(target, "wb") as dst:
                        shutil.copyfileobj(src, dst)
                    if self._is_text_log(target):
                        extracted.append(target)

            logger.info(f"zip 已解压: {zip_path} → {out_dir}, 文本日志 {len(extracted)} 个")
            return out_dir, extracted
        except Exception as e:
            logger.warning(f"zip 解压失败: path={zip_path}, error={e}")
            return None, []

    @staticmethod
    def _is_text_log(path: str) -> bool:
        """判断是否为可直接阅读的文本日志"""
        return os.path.splitext(path)[1].lower() in (".log", ".txt", ".out", ".err")

    # ============ 读取 ============

    def read_text_head(self, path: str, limit: Optional[int] = None) -> str:
        """
        读取文本文件内容（截断到 limit 字符）。

        Args:
            path: 文件路径
            limit: 字符上限（None 用配置的 inline_chars）

        Returns:
            文件文本（可能被截断，截断时带说明）；读取失败返回空串
        """
        cap = self.inline_chars if limit is None else limit
        if cap <= 0:
            return ""
        try:
            # 日志编码不确定，errors="replace" 保证不因个别乱码整体失败
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                text = f.read(cap + 1)
            if len(text) > cap:
                return text[:cap] + f"\n...[内容超过 {cap} 字符已截断，完整文件见上述路径]"
            return text
        except Exception as e:
            logger.warning(f"读取日志文件失败: path={path}, error={e}")
            return ""

    # ============ 定时清理 ============

    def start_cleanup_scheduler(self) -> None:
        """启动每日定时清理线程（守护线程，进程退出自动结束）"""
        if not self.enabled or self._cleanup_thread is not None:
            return
        self._cleanup_thread = threading.Thread(
            target=self._cleanup_loop, daemon=True, name="log-files-cleanup"
        )
        self._cleanup_thread.start()
        logger.info(f"日志文件清理调度已启动: 每日 {self.cleanup_hour}:00")

    def stop_cleanup_scheduler(self) -> None:
        """停止清理线程"""
        self._stop_event.set()

    def _cleanup_loop(self) -> None:
        """
        循环等待到配置的清理时刻执行清理。

        用"到点检测"而非 sleep 到精确时刻，避免系统休眠/时钟跳变导致错过。
        每 10 分钟检查一次，命中目标小时且当天未清理过则执行。
        """
        last_cleanup_date = None
        while not self._stop_event.is_set():
            try:
                now = datetime.now()
                today = now.date()
                if now.hour == self.cleanup_hour and last_cleanup_date != today:
                    self.cleanup_once()
                    last_cleanup_date = today
            except Exception as e:
                logger.warning(f"日志文件清理调度异常: {e}")
            # 10 分钟一次检查，兼顾及时性与开销
            self._stop_event.wait(600)

    def cleanup_once(self) -> int:
        """
        清理超过保留时长的文件与空目录。

        Returns:
            删除的文件数
        """
        if not self.enabled or not os.path.isdir(self.base_dir):
            return 0

        cutoff = time.time() - self.retention_seconds
        removed = 0
        try:
            for root, dirs, files in os.walk(self.base_dir, topdown=False):
                for name in files:
                    path = os.path.join(root, name)
                    try:
                        if os.path.getmtime(path) < cutoff:
                            os.remove(path)
                            removed += 1
                    except Exception as e:
                        logger.debug(f"删除日志文件失败: {path}, {e}")
                # 自底向上删空目录（不动 base_dir 自身）
                for name in dirs:
                    d = os.path.join(root, name)
                    try:
                        if not os.listdir(d):
                            os.rmdir(d)
                    except Exception:
                        pass
            logger.info(f"日志文件清理完成: 删除 {removed} 个文件（保留 {self.retention_seconds // 3600} 小时内）")
        except Exception as e:
            logger.warning(f"日志文件清理失败: {e}")
        return removed
