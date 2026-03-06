from datetime import datetime
from typing import List, Optional
import pyautogui
from pywinauto import Application
from loguru import logger
from ai_assistant.adapters.base import IMAdapter
from ai_assistant.core.models import Message, Content


class FeishuAdapter(IMAdapter):
    """飞书适配器"""

    def __init__(self):
        self.window_titles = ["飞书", "Lark", "Feishu"]
        self.current_window = None
        self.last_message = None

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

        注意：这是一个简化的实现，实际使用中需要更复杂的 UI 自动化逻辑
        来准确提取飞书窗口中的消息内容
        """
        messages = []

        try:
            # 简化实现：使用截图和 OCR 或 UI 元素定位
            # 这里返回空列表，实际实现需要：
            # 1. 使用 pywinauto 定位消息区域
            # 2. 提取文本内容
            # 3. 识别发送者和时间戳

            logger.warning("extract_messages is a simplified implementation")

            # 占位实现：返回空消息列表
            return messages

        except Exception as e:
            logger.error(f"Failed to extract messages: {e}")
            return []

    def check_trigger(self, keyword: str) -> bool:
        """
        检查最新消息是否包含触发关键词

        简化实现：通过剪贴板检测
        """
        try:
            # 简化实现：假设用户选中了消息文本
            # 实际实现需要自动定位和读取最新消息

            import pyperclip

            # 尝试获取剪贴板内容（用户需要先复制消息）
            clipboard_text = pyperclip.paste()

            if clipboard_text and keyword in clipboard_text:
                logger.info(f"Trigger keyword detected: {keyword}")
                self.last_message = clipboard_text
                return True

            return False

        except Exception as e:
            logger.error(f"Failed to check trigger: {e}")
            return False

    def get_session_id(self) -> Optional[str]:
        """获取当前会话 ID"""
        try:
            if self.current_window:
                # 使用窗口标题作为会话 ID
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
