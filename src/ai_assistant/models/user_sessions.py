"""
用户会话模型

支持服务端不透明 token、会话过期管理
"""

import re
from datetime import datetime, timedelta
from sqlalchemy import Column, String, DateTime, ForeignKey, Index
from sqlalchemy.orm import validates

from ai_assistant.core.database import Base


class UserSession(Base):
    """用户会话表（服务端 session 存储）"""

    __tablename__ = "user_sessions"

    # 主键
    session_id = Column(String(64), primary_key=True, comment="会话唯一标识（UUID）")

    # 关联用户
    user_id = Column(
        String(64),
        ForeignKey("users.user_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="关联的用户 ID"
    )

    # Token 哈希（sha256）
    token_hash = Column(String(64), unique=True, nullable=False, index=True, comment="Token 的 SHA256 哈希值")

    # 时间戳
    created_at = Column(DateTime, nullable=False, default=datetime.now, comment="会话创建时间")
    expires_at = Column(DateTime, nullable=False, comment="会话过期时间")

    # 索引
    __table_args__ = (
        Index("idx_user_sessions_user_id", "user_id"),
        Index("idx_user_sessions_token_hash", "token_hash"),
        Index("idx_user_sessions_expires_at", "expires_at"),
    )

    @validates("session_id")
    def validate_session_id(self, key, value):
        """验证 session_id 格式"""
        if not value:
            raise ValueError("session_id 不能为空")
        return value

    @validates("user_id")
    def validate_user_id(self, key, value):
        """验证 user_id 格式"""
        if not value:
            raise ValueError("user_id 不能为空")
        return value

    @validates("token_hash")
    def validate_token_hash(self, key, value):
        """验证 token_hash 格式"""
        if not value or len(value) != 64:
            raise ValueError("token_hash 必须是 64 字符的 SHA256 哈希值")
        if not re.match(r'^[0-9a-f]{64}$', value):
            raise ValueError("token_hash 必须是有效的 SHA256 十六进制字符串")
        return value

    def is_expired(self) -> bool:
        """
        检查会话是否过期

        Returns:
            过期返回 True，否则返回 False
        """
        return datetime.now() >= self.expires_at

    def __repr__(self):
        return (
            f"<UserSession(session_id={self.session_id}, user_id={self.user_id}, "
            f"expires_at={self.expires_at})>"
        )

    def to_dict(self) -> dict:
        """转换为字典"""
        return {
            "session_id": self.session_id,
            "user_id": self.user_id,
            "created_at": self.created_at.strftime("%Y-%m-%d %H:%M:%S"),
            "expires_at": self.expires_at.strftime("%Y-%m-%d %H:%M:%S"),
            "is_expired": self.is_expired(),
        }
