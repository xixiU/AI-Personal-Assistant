from abc import ABC, abstractmethod
from typing import List
from ai_assistant.core.models import Message


class AIProvider(ABC):
    """AI Provider 抽象基类"""

    @abstractmethod
    def send_message(self, messages: List[Message]) -> str:
        """
        发送消息到 AI 模型并获取回复

        Args:
            messages: 消息列表（包含上下文）

        Returns:
            AI 生成的回复文本
        """
        pass

    @abstractmethod
    def check_health(self) -> bool:
        """
        检查 AI 服务健康状态

        Returns:
            True 如果服务可用，否则 False
        """
        pass
