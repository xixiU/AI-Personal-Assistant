import pyperclip
from loguru import logger


class ReplyExecutor:
    """回复执行器，负责将 AI 回复发送到 IM 窗口"""

    def __init__(self, mode: str = "clipboard", notification: bool = True):
        """
        初始化回复执行器

        Args:
            mode: 回复模式 ("clipboard" | "auto_input")
            notification: 是否显示通知
        """
        self.mode = mode
        self.notification = notification

    def execute(self, reply: str) -> bool:
        """
        执行回复

        Args:
            reply: AI 生成的回复内容

        Returns:
            True 如果执行成功，否则 False
        """
        try:
            if self.mode == "clipboard":
                return self._copy_to_clipboard(reply)
            elif self.mode == "auto_input":
                logger.warning("auto_input mode not implemented yet")
                return self._copy_to_clipboard(reply)
            else:
                logger.error(f"Unknown reply mode: {self.mode}")
                return False
        except Exception as e:
            logger.error(f"Failed to execute reply: {e}")
            return False

    def _copy_to_clipboard(self, text: str) -> bool:
        """
        复制文本到剪贴板

        Args:
            text: 要复制的文本

        Returns:
            True 如果成功，否则 False
        """
        try:
            pyperclip.copy(text)
            logger.info(f"Reply copied to clipboard: {len(text)} characters")

            if self.notification:
                self._show_notification("AI 回复已复制到剪贴板")

            return True
        except Exception as e:
            logger.error(f"Failed to copy to clipboard: {e}")
            return False

    def _show_notification(self, message: str) -> None:
        """
        显示系统通知

        Args:
            message: 通知消息
        """
        try:
            # Windows 系统通知（简单实现）
            logger.info(f"Notification: {message}")
            # 可以使用 win10toast 或其他库实现更好的通知
            print(f"\n🔔 {message}\n")
        except Exception as e:
            logger.warning(f"Failed to show notification: {e}")
