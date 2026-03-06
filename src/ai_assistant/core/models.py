from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Union


@dataclass
class Content:
    """消息内容（支持多模态）"""
    type: str  # "text" | "image" | "video"
    data: Union[str, bytes]


@dataclass
class Message:
    """单条消息"""
    role: str  # "user" | "assistant"
    content: List[Content]
    timestamp: datetime


@dataclass
class Session:
    """会话上下文"""
    session_id: str
    messages: List[Message] = field(default_factory=list)
    context_mode: str = "short"
    max_messages: int = 10
    last_active: datetime = field(default_factory=datetime.now)

    def add_message(self, message: Message) -> None:
        """添加消息到会话，自动维护最大消息数限制"""
        self.messages.append(message)
        self.last_active = datetime.now()

        # 保持消息数量在限制内
        if len(self.messages) > self.max_messages:
            self.messages = self.messages[-self.max_messages:]

    def get_context_messages(self) -> List[Message]:
        """获取上下文消息"""
        return self.messages.copy()
