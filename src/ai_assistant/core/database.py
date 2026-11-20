"""
数据库连接和会话管理

支持多数据库：SQLite（默认）、MySQL、PostgreSQL
通过配置文件切换数据库类型和连接参数
"""

from contextlib import contextmanager
from typing import Generator
from sqlalchemy import create_engine, event
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

    def close(self):
        """关闭数据库连接"""
        self.engine.dispose()
        logger.info("数据库连接已关闭")
