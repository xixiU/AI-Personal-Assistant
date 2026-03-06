from abc import ABC, abstractmethod
from typing import List, Optional
from ai_assistant.core.models import Message


class IMAdapter(ABC):
    """IM 适配器抽象基类"""

    @abstractmethod
    def detect_active_window(self) -> bool:
        """
        检测当前活动窗口是否为目标 IM 工具

        Returns:
            True 如果是目标 IM 窗口，否则 False
        """
        pass

    @abstractmethod
    def extract_messages(self, count: int = 10) -> List[Message]:
        """
        提取最近的消息

        Args:
            count: 要提取的消息数量

        Returns:
            消息列表
        """
        pass

    @abstractmethod
    def check_trigger(self, keyword: str) -> bool:
        """
        检查最新消息是否包含触发关键词

        Args:
            keyword: 触发关键词

        Returns:
            True 如果触发，否则 False
        """
        pass

    @abstractmethod
    def get_session_id(self) -> Optional[str]:
        """
        获取当前会话 ID（用于上下文管理）

        Returns:
            会话 ID，如果无法获取则返回 None
        """
        pass
