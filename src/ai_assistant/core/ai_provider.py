import time
from abc import ABC, abstractmethod
from typing import List, Optional
from loguru import logger
from ai_assistant.core.models import Message


class AIProvider(ABC):
    """AI Provider 抽象基类"""

    _chat_history = None  # 对话历史管理器（类级共享）

    @classmethod
    def set_chat_history(cls, chat_history):
        """设置对话历史管理器（所有 Provider 共享）"""
        cls._chat_history = chat_history

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
        统一入口：记录日志 + 计时 + 调用 send_message + 保存历史

        所有外部调用应使用此方法，而非直接调用 send_message。
        """
        provider_name = self.__class__.__name__
        model_name = getattr(self, 'model', 'unknown')

        logger.info(f"调用 AI: provider={provider_name}, model={model_name}, messages={messages}")

        start = time.time()
        reply = self.send_message(messages, session_id=session_id)
        duration = time.time() - start

        logger.info(f"AI 回复完成: {len(reply)} 字符, 耗时={duration:.2f}s")

        # 保存对话历史
        if self._chat_history:
            try:
                # 提取最后一条用户消息作为 query
                query = ""
                for msg in reversed(messages):
                    if msg.role == "user":
                        query = " ".join(c.data for c in msg.content if c.type == "text")
                        break
                if query:
                    self._chat_history.save(
                        session_id=session_id or "unknown",
                        query=query,
                        answer=reply,
                        latency_ms=int(duration * 1000),
                    )
            except Exception as e:
                logger.warning(f"保存对话历史失败: {e}")

        return reply

    @abstractmethod
    def check_health(self) -> bool:
        """
        检查 AI 服务健康状态

        Returns:
            True 如果服务可用，否则 False
        """
        pass
