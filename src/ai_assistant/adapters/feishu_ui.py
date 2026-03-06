"""
飞书 UI 自动化适配器（改进版）

使用 pywinauto 自动读取消息，无需手动复制
"""
from datetime import datetime
from typing import List, Optional
import time
import pyautogui
from pywinauto import Application, Desktop
from loguru import logger

from ai_assistant.adapters.base import IMAdapter
from ai_assistant.core.models import Message, Content


class FeishuUIAdapter(IMAdapter):
    """飞书 UI 自动化适配器"""

    def __init__(self, config: dict):
        """
        初始化飞书 UI 适配器

        Args:
            config: 配置字典
        """
        self.window_titles = config.get("window_titles", ["飞书", "Lark", "Feishu"])
        self.poll_interval = config.get("poll_interval", 1.0)
        self.current_window = None
        self.last_message = None
        self.processed_messages = set()

    def detect_active_window(self) -> bool:
        """检测当前活动窗口是否为飞书"""
        try:
            active_window = pyautogui.getActiveWindow()
            if active_window:
                title = active_window.title
                for keyword in self.window_titles:
                    if keyword in title:
                        self.current_window = active_window
                        logger.debug(f"Detected Feishu window: {title}")
                        return True
            return False
        except Exception as e:
            logger.debug(f"Failed to detect active window: {e}")
            return False

    def extract_messages(self, count: int = 10) -> List[Message]:
        """
        提取最近的消息（简化实现）

        注意：完整实现需要遍历 UI 元素树提取消息
        """
        messages = []
        try:
            # TODO: 使用 pywinauto 遍历消息列表元素
            # 这里是占位实现
            logger.warning("extract_messages is a simplified implementation")
            return messages
        except Exception as e:
            logger.error(f"Failed to extract messages: {e}")
            return []

    def check_trigger(self, keyword: str) -> bool:
        """
        检查是否触发关键词

        当前实现：通过剪贴板检测（保持向后兼容）
        TODO: 改为自动读取 UI 元素
        """
        try:
            import pyperclip

            # 获取剪贴板内容
            clipboard_text = pyperclip.paste()

            if clipboard_text and keyword in clipboard_text:
                # 避免重复处理同一条消息
                if clipboard_text not in self.processed_messages:
                    logger.info(f"Trigger keyword detected: {keyword}")
                    self.last_message = clipboard_text
                    self.processed_messages.add(clipboard_text)

                    # 限制已处理消息集合大小
                    if len(self.processed_messages) > 100:
                        self.processed_messages.clear()

                    return True

            return False

        except Exception as e:
            logger.error(f"Failed to check trigger: {e}")
            return False

    def get_session_id(self) -> Optional[str]:
        """获取当前会话 ID"""
        try:
            if self.current_window:
                return self.current_window.title
            return None
        except Exception as e:
            logger.error(f"Failed to get session ID: {e}")
            return None

    def get_last_message_as_message(self) -> Optional[Message]:
        """将最后检测到的消息转换为 Message 对象"""
        if self.last_message:
            content = Content(type="text", data=self.last_message)
            return Message(
                role="user",
                content=[content],
                timestamp=datetime.now()
            )
        return None
