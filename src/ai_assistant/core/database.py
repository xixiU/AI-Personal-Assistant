"""
数据库连接和会话管理

支持多数据库：SQLite（默认）、MySQL、PostgreSQL
通过配置文件切换数据库类型和连接参数
"""

from contextlib import contextmanager
from typing import Generator
from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker, declarative_base, Session
from loguru import logger


# ORM 基类
Base = declarative_base()


class DatabaseManager:
    """数据库管理器，负责连接、会话管理"""

    def __init__(
        self,
        db_type: str = "sqlite",
        db_path: str = "./data/ai_assistant.db",
        db_host: str = "localhost",
        db_port: int = 3306,
        db_name: str = "ai_assistant",
        db_user: str = "",
        db_password: str = "",
        echo: bool = False,
    ):
        """
        初始化数据库管理器

        Args:
            db_type: 数据库类型（"sqlite" / "mysql" / "postgresql"）
            db_path: SQLite 数据库文件路径
            db_host: MySQL/PostgreSQL 主机地址
            db_port: MySQL/PostgreSQL 端口号
            db_name: MySQL/PostgreSQL 数据库名
            db_user: MySQL/PostgreSQL 用户名
            db_password: MySQL/PostgreSQL 密码
            echo: 是否打印 SQL 语句（调试用）
        """
        self.db_type = db_type.lower()
        self.connection_string = self._build_connection_string(
            db_type=self.db_type,
            db_path=db_path,
            db_host=db_host,
            db_port=db_port,
            db_name=db_name,
            db_user=db_user,
            db_password=db_password,
        )

        # 创建引擎
        self.engine = create_engine(
            self.connection_string,
            echo=echo,
            pool_pre_ping=True,  # 自动检测连接是否有效
            pool_recycle=3600,  # 1 小时后回收连接（避免 MySQL gone away）
        )

        # SQLite 启用外键约束
        if self.db_type == "sqlite":
            @event.listens_for(Engine, "connect")
            def set_sqlite_pragma(dbapi_conn, connection_record):
                cursor = dbapi_conn.cursor()
                cursor.execute("PRAGMA foreign_keys=ON")
                cursor.close()

        # 创建会话工厂
        self.SessionLocal = sessionmaker(
            autocommit=False, autoflush=False, bind=self.engine
        )

        logger.info(f"数据库管理器初始化完成: {self.db_type}")

    def _build_connection_string(
        self,
        db_type: str,
        db_path: str,
        db_host: str,
        db_port: int,
        db_name: str,
        db_user: str,
        db_password: str,
    ) -> str:
        """
        构建数据库连接字符串

        Args:
            db_type: 数据库类型
            db_path: SQLite 路径
            db_host: MySQL/PostgreSQL 主机
            db_port: MySQL/PostgreSQL 端口
            db_name: MySQL/PostgreSQL 数据库名
            db_user: MySQL/PostgreSQL 用户名
            db_password: MySQL/PostgreSQL 密码

        Returns:
            连接字符串

        Raises:
            ValueError: 不支持的数据库类型
        """
        if db_type == "sqlite":
            return f"sqlite:///{db_path}"

        elif db_type == "mysql":
            return (
                f"mysql+pymysql://{db_user}:{db_password}@"
                f"{db_host}:{db_port}/{db_name}?charset=utf8mb4"
            )

        elif db_type == "postgresql":
            return (
                f"postgresql+psycopg2://{db_user}:{db_password}@"
                f"{db_host}:{db_port}/{db_name}"
            )

        else:
            raise ValueError(
                f"不支持的数据库类型: {db_type}，仅支持 sqlite/mysql/postgresql"
            )

    def create_tables(self):
        """创建所有表（基于 ORM 模型定义）"""
        Base.metadata.create_all(bind=self.engine)
        logger.info("数据库表创建完成")

        # 补建已有表缺失的索引和列
        self._repair_existing_tables()

    def drop_tables(self):
        """删除所有表（慎用！会清空数据）"""
        Base.metadata.drop_all(bind=self.engine)
        logger.warning("数据库表已删除")

    @contextmanager
    def get_session(self) -> Generator[Session, None, None]:
        """
        获取数据库会话（上下文管理器）

        用法:
            with db_manager.get_session() as session:
                user = session.query(User).first()
                ...

        Yields:
            SQLAlchemy Session 对象
        """
        session = self.SessionLocal()
        try:
            yield session
            session.commit()
        except Exception as e:
            session.rollback()
            logger.error(f"数据库事务失败，已回滚: {e}")
            raise
        finally:
            session.close()

    def _repair_existing_tables(self):
        """
        修复已有表的索引和列（用于升级现有数据库）

        - 给 users 表补充 is_active、last_login_at 列
        - 给 chat_history 和 feedback 表补建缺失的索引
        """
        try:
            with self.get_session() as session:
                # 1. 检查 users 表是否存在，如果存在则补充列
                if self.engine.dialect.has_table(self.engine.connect(), "users"):
                    # 根据数据库类型使用不同的列定义
                    if self.db_type == "sqlite":
                        self._add_column_if_not_exists(session, "users", "is_active", "INTEGER DEFAULT 1")
                    elif self.db_type == "mysql":
                        self._add_column_if_not_exists(session, "users", "is_active", "BOOLEAN DEFAULT TRUE")
                    elif self.db_type == "postgresql":
                        self._add_column_if_not_exists(session, "users", "is_active", "BOOLEAN DEFAULT TRUE")

                    self._add_column_if_not_exists(session, "users", "last_login_at", "DATETIME")

                    # 补建 users 表的唯一索引
                    self._create_index_if_not_exists(session, "users", "ix_users_username", ["username"], unique=True)
                    self._create_index_if_not_exists(session, "users", "ix_users_feishu_open_id", ["feishu_open_id"], unique=True)

                # 2. 补建 chat_history 表的索引
                if self.engine.dialect.has_table(self.engine.connect(), "chat_history"):
                    self._create_index_if_not_exists(session, "chat_history", "idx_chat_history_session_source", ["session_id", "source"])
                    self._create_index_if_not_exists(session, "chat_history", "idx_chat_history_context_key", ["context_key"])
                    self._create_index_if_not_exists(session, "chat_history", "idx_chat_history_message_id", ["message_id"])
                    self._create_index_if_not_exists(session, "chat_history", "idx_chat_history_user_id", ["user_id"])
                    self._create_index_if_not_exists(session, "chat_history", "idx_chat_history_timestamp", ["timestamp"])
                    self._create_index_if_not_exists(session, "chat_history", "idx_chat_history_session_timestamp", ["session_id", "timestamp"])

                # 3. 补建 feedback 表的索引
                if self.engine.dialect.has_table(self.engine.connect(), "feedback"):
                    self._create_index_if_not_exists(session, "feedback", "idx_feedback_record_id", ["record_id"])
                    self._create_index_if_not_exists(session, "feedback", "idx_feedback_session_source", ["session_id", "source"])
                    self._create_index_if_not_exists(session, "feedback", "idx_feedback_type", ["feedback_type"])
                    self._create_index_if_not_exists(session, "feedback", "idx_feedback_timestamp", ["timestamp"])
                    self._create_index_if_not_exists(session, "feedback", "idx_feedback_user_id", ["user_id"])
                    self._create_index_if_not_exists(session, "feedback", "idx_feedback_session_timestamp", ["session_id", "timestamp"])

                logger.info("已有表修复完成（索引和列补建）")

        except Exception as e:
            logger.warning(f"修复已有表时出现异常（可能是新库首次启动）: {e}")

    def _add_column_if_not_exists(self, session, table_name: str, column_name: str, column_def: str):
        """
        给表添加列（如果不存在）

        Args:
            session: 数据库会话
            table_name: 表名
            column_name: 列名
            column_def: 列定义（SQL 片段，如 "INTEGER DEFAULT 1"）
        """
        try:
            # 检查列是否存在
            if self.db_type == "sqlite":
                # SQLite 通过 PRAGMA 检查列
                result = session.execute(text(f"PRAGMA table_info({table_name})"))
                columns = [row[1] for row in result]

                if column_name not in columns:
                    session.execute(text(f'ALTER TABLE "{table_name}" ADD COLUMN "{column_name}" {column_def}'))
                    session.commit()
                    logger.info(f"已添加列: {table_name}.{column_name}")

            elif self.db_type == "mysql":
                # MySQL 通过 information_schema 检查
                query = """
                    SELECT column_name
                    FROM information_schema.columns
                    WHERE table_schema = DATABASE()
                      AND table_name = :table_name
                      AND column_name = :column_name
                """
                result = session.execute(query, {"table_name": table_name, "column_name": column_name})

                if not result.fetchone():
                    session.execute(f'ALTER TABLE "{table_name}" ADD COLUMN "{column_name}" {column_def}')
                    session.commit()
                    logger.info(f"已添加列: {table_name}.{column_name}")

            elif self.db_type == "postgresql":
                # PostgreSQL 通过 information_schema 检查
                query = """
                    SELECT column_name
                    FROM information_schema.columns
                    WHERE table_schema = current_schema()
                      AND table_name = :table_name
                      AND column_name = :column_name
                """
                result = session.execute(query, {"table_name": table_name, "column_name": column_name})

                if not result.fetchone():
                    session.execute(f'ALTER TABLE "{table_name}" ADD COLUMN "{column_name}" {column_def}')
                    session.commit()
                    logger.info(f"已添加列: {table_name}.{column_name}")

        except Exception as e:
            logger.error(f"添加列失败 {table_name}.{column_name}: {e}")

    def _create_index_if_not_exists(self, session, table_name: str, index_name: str, columns: list, unique: bool = False):
        """
        创建索引（如果不存在）

        Args:
            session: 数据库会话
            table_name: 表名
            index_name: 索引名
            columns: 列名列表
            unique: 是否创建唯一索引
        """
        try:
            # 检查索引是否存在
            if self.db_type == "sqlite":
                result = session.execute(text(f"PRAGMA index_list({table_name})"))
                indexes = [row[1] for row in result]

                if index_name not in indexes:
                    columns_str = ", ".join([f'"{col}"' for col in columns])
                    unique_keyword = "UNIQUE " if unique else ""
                    session.execute(text(f'CREATE {unique_keyword}INDEX "{index_name}" ON "{table_name}" ({columns_str})'))
                    session.commit()
                    logger.info(f"已创建索引: {index_name} on {table_name}")

            elif self.db_type == "mysql":
                query = """
                    SELECT index_name
                    FROM information_schema.statistics
                    WHERE table_schema = DATABASE()
                      AND table_name = :table_name
                      AND index_name = :index_name
                """
                result = session.execute(query, {"table_name": table_name, "index_name": index_name})

                if not result.fetchone():
                    columns_str = ", ".join([f'"{col}"' for col in columns])
                    unique_keyword = "UNIQUE " if unique else ""
                    session.execute(f'CREATE {unique_keyword}INDEX "{index_name}" ON "{table_name}" ({columns_str})')
                    session.commit()
                    logger.info(f"已创建索引: {index_name} on {table_name}")

            elif self.db_type == "postgresql":
                query = """
                    SELECT indexname
                    FROM pg_indexes
                    WHERE schemaname = current_schema()
                      AND tablename = :table_name
                      AND indexname = :index_name
                """
                result = session.execute(query, {"table_name": table_name, "index_name": index_name})

                if not result.fetchone():
                    columns_str = ", ".join([f'"{col}"' for col in columns])
                    unique_keyword = "UNIQUE " if unique else ""
                    session.execute(f'CREATE {unique_keyword}INDEX "{index_name}" ON "{table_name}" ({columns_str})')
                    session.commit()
                    logger.info(f"已创建索引: {index_name} on {table_name}")

        except Exception as e:
            logger.error(f"创建索引失败 {index_name}: {e}")

    def close(self):
        """关闭数据库连接"""
        self.engine.dispose()
        logger.info("数据库连接已关闭")
