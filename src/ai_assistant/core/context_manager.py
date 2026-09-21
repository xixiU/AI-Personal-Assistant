from datetime import datetime
from typing import Dict, List
import threading
from ai_assistant.core.models import Message, Session


class ContextManager:
    """上下文管理器，管理多个会话的消息历史（线程安全）"""

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
        self.lock = threading.RLock()  # 可重入锁，支持嵌套调用

    def get_or_create_session(self, session_id: str) -> Session:
        """获取或创建会话（线程安全）"""
        with self.lock:
            if session_id not in self.sessions:
                self.sessions[session_id] = Session(
                    session_id=session_id,
                    messages=[],
                    max_messages=self.max_messages
                )
            return self.sessions[session_id]

    def add_message(self, session_id: str, message: Message) -> None:
        """向会话添加消息（线程安全）"""
        with self.lock:
            session = self.get_or_create_session(session_id)
            session.add_message(message)

    def get_context(self, session_id: str) -> List[Message]:
        """获取会话的上下文消息（线程安全）"""
        with self.lock:
            session = self.get_or_create_session(session_id)
            return session.get_context_messages()

    def clear_context(self, session_id: str) -> None:
        """
        清空指定会话的上下文消息（线程安全）

        用于消息编辑重答场景：编辑的是同一条消息、context_key 不变，
        若不清空则"改错前的问题 + 上次错答案 + 改对后的问题"会一起送给 AI。

        只清消息、保留 Session 对象本身（避免并发下另一线程持有旧引用后写入丢失）。

        Args:
            session_id: 会话/上下文标识
        """
        with self.lock:
            session = self.sessions.get(session_id)
            if session is not None:
                session.messages.clear()

    def cleanup_expired_sessions(self) -> None:
        """清理过期的会话（线程安全）"""
        now = datetime.now()
        expired_sessions = []

        with self.lock:
            for session_id, session in self.sessions.items():
                if (now - session.last_active).total_seconds() > self.session_timeout:
                    expired_sessions.append(session_id)

            for session_id in expired_sessions:
                del self.sessions[session_id]
