from datetime import datetime
from typing import Dict, List
from ai_assistant.core.models import Message, Session


class ContextManager:
    """上下文管理器，管理多个会话的消息历史"""

    def __init__(self, max_messages: int = 10, session_timeout: int = 3600):
        """
        初始化上下文管理器

        Args:
            max_messages: 每个会话保留的最大消息数
            session_timeout: 会话超时时间（秒）
        """
        self.max_messages = max_messages
        self.session_timeout = session_timeout
        self.sessions: Dict[str, Session] = {}

    def get_or_create_session(self, session_id: str) -> Session:
        """获取或创建会话"""
        if session_id not in self.sessions:
            self.sessions[session_id] = Session(
                session_id=session_id,
                messages=[],
                max_messages=self.max_messages
            )
        return self.sessions[session_id]

    def add_message(self, session_id: str, message: Message) -> None:
        """向会话添加消息"""
        session = self.get_or_create_session(session_id)
        session.add_message(message)

    def get_context(self, session_id: str) -> List[Message]:
        """获取会话的上下文消息"""
        session = self.get_or_create_session(session_id)
        return session.get_context_messages()

    def cleanup_expired_sessions(self) -> None:
        """清理过期的会话"""
        now = datetime.now()
        expired_sessions = []

        for session_id, session in self.sessions.items():
            if (now - session.last_active).total_seconds() > self.session_timeout:
                expired_sessions.append(session_id)

        for session_id in expired_sessions:
            del self.sessions[session_id]
