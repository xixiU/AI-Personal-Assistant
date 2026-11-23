"""
用户管理模块

负责用户的 CRUD 操作、密码管理、飞书用户自动注册、会话管理等功能。
"""

import secrets
import threading
import uuid
import hashlib
from datetime import datetime, timedelta
from typing import Optional, List, Dict
from loguru import logger
from sqlalchemy.exc import IntegrityError

from ai_assistant.core.database import DatabaseManager
from ai_assistant.models.user import User, UserRole
from ai_assistant.models.user_sessions import UserSession


class UserManager:
    """
    用户管理器

    提供用户增删改查、认证、密码管理等功能。
    """

    def __init__(self, db_manager: DatabaseManager, admin_config: Optional[Dict] = None, session_ttl_days: int = 7):
        """
        初始化用户管理器

        Args:
            db_manager: 数据库管理器
            admin_config: 管理员配置（从 config.yaml 读取）
            session_ttl_days: 会话有效期（天数，默认 7 天）
        """
        self.db_manager = db_manager
        self.admin_config = admin_config or {}
        self.session_ttl_days = session_ttl_days
        self._lock = threading.Lock()

        # 启动时清理过期会话
        self.cleanup_all_expired_sessions()

        # 自动创建管理员账号（如果配置了且不存在）
        self._ensure_admin_exists()

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
            用户信息包含: user_id, username, name, role, is_active, created_at
        """
        try:
            with self.db_manager.get_session() as session:
                user = session.query(User).filter(User.username == username).first()

                if not user:
                    logger.warning(f"用户认证失败: 用户不存在 username={username}")
                    return None

                # 检查账号是否启用
                if not user.is_active:
                    logger.warning(f"用户认证失败: 账号已停用 username={username}")
                    return None

                # 验证密码
                if not user.check_password(password):
                    logger.warning(f"用户认证失败: 密码错误 username={username}")
                    return None

                # 更新最后登录时间
                user.last_login_at = datetime.now()
                session.commit()

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
        keyword: Optional[str] = None,
        role: Optional[str] = None,
        is_active: Optional[bool] = None
    ) -> List[Dict]:
        """
        列出用户

        Args:
            limit: 返回数量限制
            offset: 偏移量（分页）
            keyword: 搜索关键词（username 或 name）
            role: 按角色过滤（可选）
            is_active: 按激活状态过滤（可选）

        Returns:
            用户信息列表
        """
        try:
            with self.db_manager.get_session() as session:
                query = session.query(User)

                # 关键词搜索（转义特殊字符）
                if keyword:
                    # 转义 LIKE 通配符
                    escaped_keyword = keyword.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')
                    search_pattern = f"%{escaped_keyword}%"
                    query = query.filter(
                        (User.username.like(search_pattern, escape='\\')) |
                        (User.name.like(search_pattern, escape='\\'))
                    )

                # 角色过滤
                if role:
                    role_enum = UserRole.ADMIN if role == "admin" else UserRole.USER
                    query = query.filter(User.role == role_enum)

                # 激活状态过滤
                if is_active is not None:
                    query = query.filter(User.is_active == is_active)

                # 分页
                users = query.order_by(User.created_at.desc()).offset(offset).limit(limit).all()

                return [user.to_dict() for user in users]

        except Exception as e:
            logger.error(f"列出用户失败: {e}", exc_info=True)
            return []

    def update_password(self, user_id: str, old_password: str, new_password: str) -> bool:
        """
        更新用户密码（用户自己改密码）

        Args:
            user_id: 用户 ID
            old_password: 旧密码（明文）
            new_password: 新密码（明文）

        Returns:
            更新成功返回 True，失败返回 False

        注意：修改密码后会删除该用户的所有会话，强制重新登录
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

                # 删除该用户的所有会话（强制重新登录）
                deleted_count = self.delete_user_sessions(user_id)
                logger.info(f"密码更新后删除了 {deleted_count} 个会话")

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

        注意：重置密码后会删除该用户的所有会话，强制重新登录
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

                # 删除该用户的所有会话（强制重新登录）
                deleted_count = self.delete_all_sessions_for_user(user_id)
                logger.info(f"密码重置后删除了 {deleted_count} 个会话")

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

    def update_user(
        self,
        user_id: str,
        username: Optional[str] = None,
        name: Optional[str] = None,
        role: Optional[str] = None,
        is_active: Optional[bool] = None,
        feishu_open_id: Optional[str] = None
    ) -> bool:
        """
        更新用户信息（管理员操作）

        Args:
            user_id: 用户 ID
            username: 用户名（可选）
            name: 显示名称（可选）
            role: 角色 "user" 或 "admin"（可选）
            is_active: 账号是否启用（可选）
            feishu_open_id: 飞书 open_id（可选）

        Returns:
            更新成功返回 True，失败返回 False
        """
        try:
            with self.db_manager.get_session() as session:
                user = session.query(User).filter(User.user_id == user_id).first()

                if not user:
                    logger.warning(f"更新用户失败: 用户不存在 user_id={user_id}")
                    return False

                # 更新字段
                if username is not None:
                    user.username = username
                if name is not None:
                    user.name = name
                if role is not None:
                    user.role = UserRole.ADMIN if role == "admin" else UserRole.USER
                if is_active is not None:
                    user.is_active = is_active
                if feishu_open_id is not None:
                    user.feishu_open_id = feishu_open_id

                session.commit()

                logger.info(f"用户信息更新成功: user_id={user_id}")
                return True

        except IntegrityError as e:
            logger.error(f"更新用户失败（唯一约束冲突）: user_id={user_id}, {e}")
            return False
        except Exception as e:
            logger.error(f"更新用户失败: user_id={user_id}, {e}", exc_info=True)
            return False

    def create_session(self, user_id: str) -> Optional[str]:
        """
        创建会话（登录成功后调用）

        Args:
            user_id: 用户 ID

        Returns:
            成功返回 token（32 字节以上随机字符串），失败返回 None
            token 需要存入 Cookie（ai_session, HttpOnly, SameSite=Lax)
        """
        try:
            with self.db_manager.get_session() as session:
                # 检查用户是否存在且启用
                user = session.query(User).filter(User.user_id == user_id).first()
                if not user or not user.is_active:
                    logger.warning(f"创建会话失败: 用户不存在或已停用 user_id={user_id}")
                    return None

                # 清理该用户的过期会话
                self._cleanup_expired_sessions(session, user_id)

                # 限制单用户会话数（保留最近 10 个）
                self._limit_user_sessions(session, user_id, max_sessions=10)

                # 生成随机 token（至少 32 字节）
                token = secrets.token_urlsafe(32)

                # 计算 token 的 SHA256 哈希
                token_hash = hashlib.sha256(token.encode()).hexdigest()

                # 计算过期时间
                expires_at = datetime.now() + timedelta(days=self.session_ttl_days)

                # 创建会话记录
                user_session = UserSession(
                    session_id=str(uuid.uuid4()),
                    user_id=user_id,
                    token_hash=token_hash,
                    expires_at=expires_at,
                )

                session.add(user_session)
                session.commit()

                logger.info(f"会话创建成功: user_id={user_id}, expires_at={expires_at}")
                return token

        except Exception as e:
            logger.error(f"创建会话失败: user_id={user_id}, {e}", exc_info=True)
            return None

    def _cleanup_expired_sessions(self, session, user_id: Optional[str] = None):
        """
        清理过期会话

        Args:
            session: 数据库会话
            user_id: 用户 ID（可选，仅清理该用户的过期会话）
        """
        try:
            query = session.query(UserSession).filter(UserSession.expires_at < datetime.now())

            if user_id:
                query = query.filter(UserSession.user_id == user_id)

            expired_sessions = query.all()
            count = len(expired_sessions)

            for expired_session in expired_sessions:
                session.delete(expired_session)

            if count > 0:
                session.commit()
                logger.info(f"清理过期会话: {'user_id=' + user_id if user_id else '全局'}, count={count}")

        except Exception as e:
            logger.error(f"清理过期会话失败: {e}", exc_info=True)

    def _limit_user_sessions(self, session, user_id: str, max_sessions: int = 10):
        """
        限制单用户会话数量

        Args:
            session: 数据库会话
            user_id: 用户 ID
            max_sessions: 最大会话数（默认 10）
        """
        try:
            # 查询该用户的所有会话，按创建时间倒序
            user_sessions = session.query(UserSession).filter(
                UserSession.user_id == user_id
            ).order_by(UserSession.created_at.desc()).all()

            # 如果超过限制，删除最旧的会话
            if len(user_sessions) >= max_sessions:
                sessions_to_delete = user_sessions[max_sessions - 1:]  # 保留 max_sessions - 1 个（为新会话留空间）
                for old_session in sessions_to_delete:
                    session.delete(old_session)
                session.commit()
                logger.info(f"限制用户会话数: user_id={user_id}, 删除了 {len(sessions_to_delete)} 个旧会话")

        except Exception as e:
            logger.error(f"限制用户会话数失败: user_id={user_id}, {e}", exc_info=True)

    def validate_session(self, token: str) -> Optional[Dict]:
        """
        验证会话 token，返回用户信息

        Args:
            token: 会话 token（从 Cookie 读取）

        Returns:
            会话有效返回用户信息字典（含 user_id, username, role, is_active 等），
            会话无效/过期/账号停用返回 None

        注意：
            - 账号停用（is_active=False）后立即返回 None
            - 过期会话自动删除
        """
        try:
            with self.db_manager.get_session() as session:
                # 计算 token 哈希
                token_hash = hashlib.sha256(token.encode()).hexdigest()

                # 查找会话
                user_session = session.query(UserSession).filter(
                    UserSession.token_hash == token_hash
                ).first()

                if not user_session:
                    logger.debug("会话验证失败: token 不存在")
                    return None

                # 检查会话是否过期
                if user_session.is_expired():
                    logger.info(f"会话已过期，自动删除: user_id={user_session.user_id}")
                    session.delete(user_session)
                    session.commit()
                    return None

                # 获取用户信息
                user = session.query(User).filter(User.user_id == user_session.user_id).first()

                if not user:
                    logger.warning(f"会话关联的用户不存在: user_id={user_session.user_id}")
                    return None

                # 检查账号是否启用
                if not user.is_active:
                    logger.info(f"会话验证失败: 账号已停用 user_id={user.user_id}")
                    return None

                logger.debug(f"会话验证成功: user_id={user.user_id}")
                return user.to_dict()

        except Exception as e:
            logger.error(f"验证会话失败: {e}", exc_info=True)
            return None

    def delete_session(self, token: str) -> bool:
        """
        删除会话（登出）

        Args:
            token: 会话 token

        Returns:
            删除成功返回 True，失败返回 False
        """
        try:
            with self.db_manager.get_session() as session:
                # 计算 token 哈希
                token_hash = hashlib.sha256(token.encode()).hexdigest()

                # 查找并删除会话
                user_session = session.query(UserSession).filter(
                    UserSession.token_hash == token_hash
                ).first()

                if not user_session:
                    logger.debug("删除会话失败: token 不存在")
                    return False

                session.delete(user_session)
                session.commit()

                logger.info(f"会话删除成功: user_id={user_session.user_id}")
                return True

        except Exception as e:
            logger.error(f"删除会话失败: {e}", exc_info=True)
            return False

    def delete_user_sessions(self, user_id: str) -> int:
        """
        删除指定用户的所有会话（用户改密码时调用）

        Args:
            user_id: 用户 ID

        Returns:
            删除的会话数量
        """
        try:
            with self.db_manager.get_session() as session:
                # 查找用户的所有会话
                user_sessions = session.query(UserSession).filter(
                    UserSession.user_id == user_id
                ).all()

                count = len(user_sessions)

                # 删除所有会话
                for user_session in user_sessions:
                    session.delete(user_session)

                session.commit()

                logger.info(f"删除用户所有会话: user_id={user_id}, count={count}")
                return count

        except Exception as e:
            logger.error(f"删除用户会话失败: user_id={user_id}, {e}", exc_info=True)
            return 0

    def delete_all_sessions_for_user(self, user_id: str) -> int:
        """
        删除指定用户的所有会话（管理员重置密码时调用）

        Args:
            user_id: 用户 ID

        Returns:
            删除的会话数量

        注意：这是管理员操作，与 delete_user_sessions 功能相同，
             但语义上是管理员触发（比如重置密码后强制登出）
        """
        return self.delete_user_sessions(user_id)

    def cleanup_all_expired_sessions(self):
        """
        清理所有过期会话（启动时调用）
        """
        try:
            with self.db_manager.get_session() as session:
                self._cleanup_expired_sessions(session)
        except Exception as e:
            logger.error(f"清理所有过期会话失败: {e}", exc_info=True)

    def _ensure_admin_exists(self):
        """
        确保管理员账号存在（启动时自动创建）

        从 admin_config 读取管理员配置，如果数据库中不存在该管理员则自动创建。
        """
        if not self.admin_config:
            logger.debug("未配置管理员账号，跳过自动创建")
            return

        admin_username = self.admin_config.get('username')
        admin_password = self.admin_config.get('password')

        if not admin_username or not admin_password:
            logger.warning("管理员配置不完整（缺少 username 或 password），跳过自动创建")
            return

        try:
            with self.db_manager.get_session() as session:
                # 检查管理员是否已存在
                existing_admin = session.query(User).filter(User.username == admin_username).first()

                if existing_admin:
                    # 验证密码是否匹配
                    if not existing_admin.check_password(admin_password):
                        logger.warning(
                            f"管理员账号已存在但密码不匹配: username={admin_username}。"
                            f"请检查 config.yaml 中的密码配置是否正确。"
                        )
                    else:
                        logger.info(f"管理员账号已存在: username={admin_username}, role={existing_admin.role.value}")
                    return

                # 创建管理员账号
                admin_name = self.admin_config.get('name', '系统管理员')
                user_id = self.create_user(
                    username=admin_username,
                    password=admin_password,
                    name=admin_name,
                    role='admin'
                )

                if user_id:
                    logger.info(f"管理员账号自动创建成功: user_id={user_id}, username={admin_username}")
                else:
                    logger.error(f"管理员账号自动创建失败: username={admin_username}")

        except Exception as e:
            logger.error(f"检查/创建管理员账号失败: {e}", exc_info=True)

    def count_users(
        self,
        keyword: Optional[str] = None,
        role: Optional[str] = None,
        is_active: Optional[bool] = None
    ) -> int:
        """
        统计用户总数（支持过滤）

        Args:
            keyword: 搜索关键词（username 或 name）
            role: 角色过滤
            is_active: 激活状态过滤

        Returns:
            用户总数
        """
        try:
            with self.db_manager.get_session() as session:
                query = session.query(User)

                # 关键词搜索（转义特殊字符）
                if keyword:
                    # 转义 LIKE 通配符
                    escaped_keyword = keyword.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')
                    search_pattern = f"%{escaped_keyword}%"
                    query = query.filter(
                        (User.username.like(search_pattern, escape='\\')) |
                        (User.name.like(search_pattern, escape='\\'))
                    )

                # 角色过滤
                if role:
                    role_enum = UserRole.ADMIN if role == "admin" else UserRole.USER
                    query = query.filter(User.role == role_enum)

                # 激活状态过滤
                if is_active is not None:
                    query = query.filter(User.is_active == is_active)

                return query.count()

        except Exception as e:
            logger.error(f"统计用户失败: {e}", exc_info=True)
            return 0

    def count_admins(self, exclude_user_id: Optional[str] = None) -> int:
        """
        统计管理员数量

        Args:
            exclude_user_id: 排除的用户 ID（用于检查"如果停用/降级这个用户后还剩几个管理员"）

        Returns:
            管理员数量
        """
        try:
            with self.db_manager.get_session() as session:
                query = session.query(User).filter(User.role == UserRole.ADMIN)

                # 排除指定用户
                if exclude_user_id:
                    query = query.filter(User.user_id != exclude_user_id)

                return query.count()

        except Exception as e:
            logger.error(f"统计管理员失败: {e}", exc_info=True)
            return 0
