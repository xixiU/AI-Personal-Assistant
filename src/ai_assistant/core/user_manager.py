"""
用户管理模块

负责用户的 CRUD 操作、密码管理、飞书用户自动注册等功能。
"""

import secrets
import threading
import uuid
from typing import Optional, List, Dict
from loguru import logger
from sqlalchemy.exc import IntegrityError

from ai_assistant.core.database import DatabaseManager
from ai_assistant.models.user import User, UserRole


class UserManager:
    """
    用户管理器

    提供用户增删改查、认证、密码管理等功能。
    """

    def __init__(self, db_manager: DatabaseManager, admin_config: Optional[Dict] = None):
        """
        初始化用户管理器

        Args:
            db_manager: 数据库管理器
            admin_config: 管理员配置（从 config.yaml 读取）
        """
        self.db_manager = db_manager
        self.admin_config = admin_config or {}
        self._lock = threading.Lock()
        logger.info("用户管理器初始化完成")

    def create_user(
        self,
        username: str,
        password: str,
        feishu_open_id: Optional[str] = None,
        display_name: Optional[str] = None,
        role: str = "user",
    ) -> Optional[str]:
        """
        创建用户

        Args:
            username: 登录用户名（唯一）
            password: 密码（明文，内部会加密存储）
            feishu_open_id: 飞书 open_id（可选，用于关联飞书用户）
            display_name: 显示名称（可选）
            role: 角色（默认 "user"，可选 "admin"）

        Returns:
            创建成功返回 user_id，失败返回 None
        """
        try:
            with self.db_manager.get_session() as session:
                # 生成 UUID 作为 user_id
                user_id = str(uuid.uuid4())

                # 创建用户对象
                user = User(
                    user_id=user_id,
                    username=username,
                    name=display_name,
                    role=UserRole.ADMIN if role == "admin" else UserRole.USER,
                    feishu_open_id=feishu_open_id,
                )

                # 设置密码（自动 bcrypt 加密）
                user.set_password(password)

                # 保存到数据库
                session.add(user)
                session.commit()

                logger.info(f"用户创建成功: user_id={user_id}, username={username}")
                return user_id

        except IntegrityError as e:
            logger.error(f"用户创建失败（唯一约束冲突）: username={username}, {e}")
            return None
        except Exception as e:
            logger.error(f"用户创建失败: username={username}, {e}", exc_info=True)
            return None

    def authenticate(self, username: str, password: str) -> Optional[Dict]:
        """
        用户认证

        Args:
            username: 登录用户名
            password: 密码（明文）

        Returns:
            认证成功返回用户信息字典，失败返回 None
            用户信息包含: user_id, username, name, role, created_at
        """
        try:
            with self.db_manager.get_session() as session:
                user = session.query(User).filter(User.username == username).first()

                if not user:
                    logger.warning(f"用户认证失败: 用户不存在 username={username}")
                    return None

                # 验证密码
                if not user.check_password(password):
                    logger.warning(f"用户认证失败: 密码错误 username={username}")
                    return None

                logger.info(f"用户认证成功: user_id={user.user_id}, username={username}")
                return user.to_dict()

        except Exception as e:
            logger.error(f"用户认证异常: username={username}, {e}", exc_info=True)
            return None

    def get_user(self, user_id: str) -> Optional[Dict]:
        """
        获取用户信息

        Args:
            user_id: 用户 ID

        Returns:
            用户信息字典，不存在返回 None
        """
        try:
            with self.db_manager.get_session() as session:
                user = session.query(User).filter(User.user_id == user_id).first()

                if not user:
                    return None

                return user.to_dict()

        except Exception as e:
            logger.error(f"获取用户信息失败: user_id={user_id}, {e}", exc_info=True)
            return None

    def get_user_by_feishu_open_id(self, feishu_open_id: str) -> Optional[Dict]:
        """
        通过飞书 open_id 获取用户信息

        Args:
            feishu_open_id: 飞书 open_id

        Returns:
            用户信息字典，不存在返回 None
        """
        try:
            with self.db_manager.get_session() as session:
                user = session.query(User).filter(
                    User.feishu_open_id == feishu_open_id
                ).first()

                if not user:
                    return None

                return user.to_dict()

        except Exception as e:
            logger.error(f"通过飞书 open_id 获取用户失败: {feishu_open_id}, {e}", exc_info=True)
            return None

    def list_users(
        self,
        limit: int = 100,
        offset: int = 0,
        role: Optional[str] = None
    ) -> List[Dict]:
        """
        列出用户

        Args:
            limit: 返回数量限制
            offset: 偏移量（分页）
            role: 按角色过滤（可选）

        Returns:
            用户信息列表
        """
        try:
            with self.db_manager.get_session() as session:
                query = session.query(User)

                # 角色过滤
                if role:
                    role_enum = UserRole.ADMIN if role == "admin" else UserRole.USER
                    query = query.filter(User.role == role_enum)

                # 分页
                users = query.order_by(User.created_at.desc()).offset(offset).limit(limit).all()

                return [user.to_dict() for user in users]

        except Exception as e:
            logger.error(f"列出用户失败: {e}", exc_info=True)
            return []

    def update_password(self, user_id: str, old_password: str, new_password: str) -> bool:
        """
        更新用户密码

        Args:
            user_id: 用户 ID
            old_password: 旧密码（明文）
            new_password: 新密码（明文）

        Returns:
            更新成功返回 True，失败返回 False
        """
        try:
            with self.db_manager.get_session() as session:
                user = session.query(User).filter(User.user_id == user_id).first()

                if not user:
                    logger.warning(f"更新密码失败: 用户不存在 user_id={user_id}")
                    return False

                # 验证旧密码
                if not user.check_password(old_password):
                    logger.warning(f"更新密码失败: 旧密码错误 user_id={user_id}")
                    return False

                # 设置新密码
                user.set_password(new_password)
                session.commit()

                logger.info(f"密码更新成功: user_id={user_id}")
                return True

        except Exception as e:
            logger.error(f"更新密码失败: user_id={user_id}, {e}", exc_info=True)
            return False

    def reset_password(self, user_id: str, new_password: str) -> bool:
        """
        重置用户密码（管理员操作，不需要旧密码）

        Args:
            user_id: 用户 ID
            new_password: 新密码（明文）

        Returns:
            重置成功返回 True，失败返回 False
        """
        try:
            with self.db_manager.get_session() as session:
                user = session.query(User).filter(User.user_id == user_id).first()

                if not user:
                    logger.warning(f"重置密码失败: 用户不存在 user_id={user_id}")
                    return False

                # 设置新密码
                user.set_password(new_password)
                session.commit()

                logger.info(f"密码重置成功: user_id={user_id}")
                return True

        except Exception as e:
            logger.error(f"重置密码失败: user_id={user_id}, {e}", exc_info=True)
            return False

    def delete_user(self, user_id: str) -> bool:
        """
        删除用户（硬删除，慎用）

        Args:
            user_id: 用户 ID

        Returns:
            删除成功返回 True，失败返回 False
        """
        try:
            with self.db_manager.get_session() as session:
                user = session.query(User).filter(User.user_id == user_id).first()

                if not user:
                    logger.warning(f"删除用户失败: 用户不存在 user_id={user_id}")
                    return False

                session.delete(user)
                session.commit()

                logger.info(f"用户删除成功: user_id={user_id}")
                return True

        except Exception as e:
            logger.error(f"删除用户失败: user_id={user_id}, {e}", exc_info=True)
            return False

    @staticmethod
    def generate_random_password(length: int = 8) -> str:
        """
        生成随机密码

        Args:
            length: 密码长度（默认 8 位）

        Returns:
            随机密码字符串
        """
        # 使用字母和数字组合，避免特殊字符（用户输入不便）
        alphabet = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
        return ''.join(secrets.choice(alphabet) for _ in range(length))

    def is_admin(self, user_id: str) -> bool:
        """
        检查用户是否为管理员

        Args:
            user_id: 用户 ID

        Returns:
            是管理员返回 True，否则返回 False
        """
        try:
            with self.db_manager.get_session() as session:
                user = session.query(User).filter(User.user_id == user_id).first()

                if not user:
                    return False

                return user.role == UserRole.ADMIN

        except Exception as e:
            logger.error(f"检查管理员权限失败: user_id={user_id}, {e}", exc_info=True)
            return False
