"""
对话历史数据库模型

映射现有 chat_history JSONL 字段到数据库表
支持按 session_id、context_key、message_id 等维度查询
"""

from datetime import datetime
from sqlalchemy import Column, String, Integer, Text, DateTime, JSON, Index
from sqlalchemy.orm import validates

from ai_assistant.core.database import Base


class ChatHistoryDB(Base):
    """对话历史表（映射 JSONL 字段）"""

    __tablename__ = "chat_history"

    # 主键
    record_id = Column(String(64), primary_key=True, comment="记录唯一标识（UUID）")

    # 时间戳
    timestamp = Column(DateTime, nullable=False, index=True, comment="对话时间")

    # 会话信息
    session_id = Column(String(128), nullable=False, index=True, comment="会话 ID（飞书 chat_id / 微信窗口名）")
    source = Column(String(32), nullable=False, index=True, comment="来源（feishu / wechat / web）")

    # 对话内容
    query = Column(Text, nullable=False, comment="用户提问")
    answer = Column(Text, nullable=False, comment="AI 回复")

    # 性能指标
    latency_ms = Column(Integer, nullable=True, comment="响应耗时（毫秒）")

    # 用户信息
    user_id = Column(String(128), nullable=True, index=True, comment="用户标识（飞书 open_id 等）")
    user_name = Column(String(128), nullable=True, comment="用户昵称")

    # 引用链相关
    context_key = Column(String(128), nullable=True, index=True, comment="引用链标识（飞书 root_id 或首条 message_id）")
    chain_info = Column(JSON, nullable=True, comment="引用链排查字段（message_id / parent_id / root_id）")

    # 消息 ID（飞书场景专用）
    message_id = Column(String(128), nullable=True, index=True, comment="AI 回复的消息 ID（用于反馈查找）")

    # 元数据
    metadata = Column(JSON, nullable=True, comment="附加元数据（mode / tool_rounds / doc_count 等）")

    # 时间戳
    created_at = Column(DateTime, nullable=False, default=datetime.now, comment="记录创建时间")

    # 复合索引
    __table_args__ = (
        Index("idx_session_source", "session_id", "source"),
        Index("idx_context_key", "context_key"),
        Index("idx_message_id", "message_id"),
        Index("idx_user_id", "user_id"),
        Index("idx_timestamp", "timestamp"),
        Index("idx_session_timestamp", "session_id", "timestamp"),
    )

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
        allowed_sources = ["feishu", "wechat", "web", "unknown"]
        if value not in allowed_sources:
            raise ValueError(f"source 必须是以下之一: {allowed_sources}")
        return value

    def __repr__(self):
        return (
            f"<ChatHistoryDB(record_id={self.record_id}, session_id={self.session_id}, "
            f"source={self.source}, timestamp={self.timestamp})>"
        )

    def to_dict(self) -> dict:
        """转换为字典"""
        return {
            "record_id": self.record_id,
            "timestamp": self.timestamp.strftime("%Y-%m-%d %H:%M:%S") if self.timestamp else None,
            "session_id": self.session_id,
            "source": self.source,
            "query": self.query,
            "answer": self.answer,
            "latency_ms": self.latency_ms,
            "user_id": self.user_id,
            "user_name": self.user_name,
            "context_key": self.context_key,
            "chain_info": self.chain_info,
            "message_id": self.message_id,
            "metadata": self.metadata,
            "created_at": self.created_at.strftime("%Y-%m-%d %H:%M:%S") if self.created_at else None,
        }

    @classmethod
    def from_jsonl_record(cls, record: dict) -> "ChatHistoryDB":
        """
        从 JSONL 记录创建数据库对象

        Args:
            record: JSONL 记录字典

        Returns:
            ChatHistoryDB 实例
        """
        # 解析时间戳
        timestamp_str = record.get("timestamp")
        timestamp = None
        if timestamp_str:
            try:
                timestamp = datetime.strptime(timestamp_str, "%Y-%m-%d %H:%M:%S")
            except ValueError:
                timestamp = datetime.now()

        return cls(
            record_id=record.get("record_id"),
            timestamp=timestamp or datetime.now(),
            session_id=record.get("session_id", ""),
            source=record.get("source", "unknown"),
            query=record.get("query", ""),
            answer=record.get("answer", ""),
            latency_ms=record.get("latency_ms"),
            user_id=record.get("user_id"),
            user_name=record.get("user_name"),
            context_key=record.get("context_key"),
            chain_info=record.get("chain_info"),
            message_id=record.get("message_id"),
            metadata=record.get("metadata"),
        )
