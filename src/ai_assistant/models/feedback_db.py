"""
反馈数据库模型

映射现有 feedback JSONL 字段到数据库表
支持按 record_id、session_id 等维度查询
"""

from datetime import datetime
from sqlalchemy import Column, String, Text, DateTime, Enum, Index, ForeignKey
from sqlalchemy.orm import validates, relationship
import enum

from ai_assistant.core.database import Base


class FeedbackType(enum.Enum):
    """反馈类型枚举"""
    LIKE = "like"        # 点赞
    DISLIKE = "dislike"  # 点踩


class FeedbackDB(Base):
    """反馈表（映射 JSONL 字段）"""

    __tablename__ = "feedback"

    # 主键
    feedback_id = Column(String(64), primary_key=True, comment="反馈记录唯一标识（UUID）")

    # 关联的对话记录
    record_id = Column(
        String(64),
        nullable=False,
        index=True,
        comment="关联的 chat_history 记录主键"
    )

    # 时间戳
    timestamp = Column(DateTime, nullable=False, index=True, comment="反馈时间")

    # 会话信息
    session_id = Column(String(128), nullable=False, index=True, comment="会话 ID")
    source = Column(String(32), nullable=False, comment="反馈来源（feishu / wechat / web）")

    # 反馈内容
    feedback_type = Column(
        Enum(FeedbackType),
        nullable=False,
        index=True,
        comment="反馈类型（like / dislike）"
    )
    feedback_text = Column(Text, nullable=True, comment="用户填写的反馈内容（点踩时可选填）")

    # 用户信息
    user_id = Column(String(128), nullable=True, index=True, comment="反馈人 ID（飞书 open_id 等）")
    user_name = Column(String(128), nullable=True, comment="反馈人昵称")

    # 时间戳
    created_at = Column(DateTime, nullable=False, default=datetime.now, comment="记录创建时间")

    # 复合索引
    __table_args__ = (
        Index("idx_feedback_record_id", "record_id"),
        Index("idx_feedback_session_source", "session_id", "source"),
        Index("idx_feedback_type", "feedback_type"),
        Index("idx_feedback_timestamp", "timestamp"),
        Index("idx_feedback_user_id", "user_id"),
        Index("idx_feedback_session_timestamp", "session_id", "timestamp"),
    )

    @validates("feedback_id")
    def validate_feedback_id(self, key, value):
        """验证 feedback_id 格式"""
        if not value:
            raise ValueError("feedback_id 不能为空")
        return value

    @validates("record_id")
    def validate_record_id(self, key, value):
        """验证 record_id 格式"""
        if not value:
            raise ValueError("record_id 不能为空")
        return value

    @validates("session_id")
    def validate_session_id(self, key, value):
        """验证 session_id 格式"""
        if not value:
            raise ValueError("session_id 不能为空")
        return value

    @validates("source")
    def validate_source(self, key, value):
        """验证 source 格式"""
        allowed_sources = ["feishu", "wechat", "web"]
        if value not in allowed_sources:
            raise ValueError(f"source 必须是以下之一: {allowed_sources}")
        return value

    def __repr__(self):
        return (
            f"<FeedbackDB(feedback_id={self.feedback_id}, record_id={self.record_id}, "
            f"type={self.feedback_type.value}, session_id={self.session_id})>"
        )

    def to_dict(self) -> dict:
        """转换为字典"""
        return {
            "feedback_id": self.feedback_id,
            "record_id": self.record_id,
            "timestamp": self.timestamp.strftime("%Y-%m-%d %H:%M:%S") if self.timestamp else None,
            "session_id": self.session_id,
            "source": self.source,
            "feedback_type": self.feedback_type.value,
            "feedback_text": self.feedback_text or "",
            "user_id": self.user_id or "",
            "user_name": self.user_name or "",
            "created_at": self.created_at.strftime("%Y-%m-%d %H:%M:%S") if self.created_at else None,
        }

    @classmethod
    def from_jsonl_record(cls, record: dict) -> "FeedbackDB":
        """
        从 JSONL 记录创建数据库对象

        Args:
            record: JSONL 记录字典

        Returns:
            FeedbackDB 实例
        """
        # 解析时间戳
        timestamp_str = record.get("timestamp")
        timestamp = None
        if timestamp_str:
            try:
                timestamp = datetime.strptime(timestamp_str, "%Y-%m-%d %H:%M:%S")
            except ValueError:
                timestamp = datetime.now()

        # 解析反馈类型
        feedback_type_str = record.get("feedback_type", "like")
        if feedback_type_str == "like":
            feedback_type = FeedbackType.LIKE
        elif feedback_type_str == "dislike":
            feedback_type = FeedbackType.DISLIKE
        else:
            feedback_type = FeedbackType.LIKE  # 默认值

        return cls(
            feedback_id=record.get("feedback_id"),
            record_id=record.get("record_id"),
            timestamp=timestamp or datetime.now(),
            session_id=record.get("session_id", ""),
            source=record.get("source", "feishu"),
            feedback_type=feedback_type,
            feedback_text=record.get("feedback_text", ""),
            user_id=record.get("user_id", ""),
            user_name=record.get("user_name", ""),
        )
