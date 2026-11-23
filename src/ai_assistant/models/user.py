"""
用户模型

支持用户账号体系、权限管理、与飞书用户关联
"""

from datetime import datetime
from sqlalchemy import Column, String, DateTime, Enum, Boolean, Index
from sqlalchemy.orm import validates
import bcrypt
import enum

from ai_assistant.core.database import Base


class UserRole(enum.Enum):
    """用户角色枚举"""
    USER = "user"      # 普通用户
    ADMIN = "admin"    # 管理员


class User(Base):
    """用户表"""

    __tablename__ = "users"

    # 主键
    user_id = Column(String(64), primary_key=True, comment="用户唯一标识")

    # 基本信息
    username = Column(String(64), unique=True, nullable=False, index=True, comment="用户名（登录用）")
    password_hash = Column(String(128), nullable=False, comment="密码哈希（bcrypt）")
    name = Column(String(128), nullable=True, comment="用户昵称/真实姓名")

    # 权限
    role = Column(
        Enum(UserRole),
        nullable=False,
        default=UserRole.USER,
        comment="用户角色"
    )

    # 飞书关联
    feishu_open_id = Column(
        String(128),
        unique=True,
        nullable=True,
        index=True,
        comment="飞书用户 open_id（用于关联飞书账号）"
    )

    # 账号状态
    is_active = Column(Boolean, nullable=False, default=True, comment="账号是否启用")

    # 时间戳
    created_at = Column(DateTime, nullable=False, default=datetime.now, comment="创建时间")
    updated_at = Column(
        DateTime,
        nullable=False,
        default=datetime.now,
        onupdate=datetime.now,
        comment="更新时间"
    )
    last_login_at = Column(DateTime, nullable=True, comment="最后登录时间")

    # 索引
    __table_args__ = (
        Index("idx_username", "username"),
        Index("idx_feishu_open_id", "feishu_open_id"),
        Index("idx_created_at", "created_at"),
    )

    def set_password(self, password: str):
        """
        设置密码（自动加密）

        Args:
            password: 明文密码
        """
        if not password:
            raise ValueError("密码不能为空")

        # 使用 bcrypt 加密（自动加盐）
        password_bytes = password.encode("utf-8")
        salt = bcrypt.gensalt()
        self.password_hash = bcrypt.hashpw(password_bytes, salt).decode("utf-8")

    def check_password(self, password: str) -> bool:
        """
        验证密码

        Args:
            password: 待验证的明文密码

        Returns:
            密码正确返回 True，否则返回 False
        """
        if not password or not self.password_hash:
            return False

        password_bytes = password.encode("utf-8")
        hash_bytes = self.password_hash.encode("utf-8")

        return bcrypt.checkpw(password_bytes, hash_bytes)

    @validates("username")
    def validate_username(self, key, value):
        """验证用户名格式"""
        if not value or len(value) < 3:
            raise ValueError("用户名长度至少 3 个字符")
        if len(value) > 64:
            raise ValueError("用户名长度不能超过 64 个字符")
        return value

    @validates("user_id")
    def validate_user_id(self, key, value):
        """验证 user_id 格式"""
        if not value:
            raise ValueError("user_id 不能为空")
        if len(value) > 64:
            raise ValueError("user_id 长度不能超过 64 个字符")
        return value

    def __repr__(self):
        return (
            f"<User(user_id={self.user_id}, username={self.username}, "
            f"role={self.role.value}, feishu_open_id={self.feishu_open_id})>"
        )

    def to_dict(self) -> dict:
        """转换为字典（不包含密码哈希）"""
        return {
            "user_id": self.user_id,
            "username": self.username,
            "display_name": self.name,  # 映射为 display_name
            "role": self.role.value,
            "is_active": self.is_active,
            "feishu_open_id": self.feishu_open_id,  # 添加 feishu_open_id 字段
            "feishu_bound": bool(self.feishu_open_id),  # 映射为布尔值
            "created_at": self.created_at.strftime("%Y-%m-%d %H:%M:%S"),
            "updated_at": self.updated_at.strftime("%Y-%m-%d %H:%M:%S"),
            "last_login_at": self.last_login_at.strftime("%Y-%m-%d %H:%M:%S") if self.last_login_at else None,
        }
