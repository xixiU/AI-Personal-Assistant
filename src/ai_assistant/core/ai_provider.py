import time
from abc import ABC, abstractmethod
from typing import List, Optional
from loguru import logger
from ai_assistant.core.models import Message


class AIProvider(ABC):
    """AI Provider 抽象基类"""

    @abstractmethod
    def send_message(self, messages: List[Message], session_id: Optional[str] = None) -> str:
        """
        发送消息到 AI 模型并获取回复

        Args:
            messages: 消息列表（包含上下文）
            session_id: 会话 ID，用于维持多用户对话上下文

        Returns:
            AI 生成的回复文本
        """
        pass

    def call(self, messages: List[Message], session_id: Optional[str] = None) -> str:
        """
        统一入口：记录日志 + 计时 + 调用 send_message

        所有外部调用应使用此方法，而非直接调用 send_message。
        """
        provider_name = self.__class__.__name__
        model_name = getattr(self, 'model', 'unknown')

        logger.info(f"调用 AI: provider={provider_name}, model={model_name}, messages={messages}")

        start = time.time()
        reply = self.send_message(messages, session_id=session_id)
        duration = time.time() - start

        logger.info(f"AI 回复完成: {len(reply)} 字符, 耗时={duration:.2f}s")
        return reply

    @abstractmethod
    def check_health(self) -> bool:
        """
        检查 AI 服务健康状态

        Returns:
            True 如果服务可用，否则 False
        """
        pass
