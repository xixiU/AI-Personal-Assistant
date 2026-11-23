"""
数据迁移脚本 - 方案 A：统一使用 UUID

将现有 JSONL 数据（chat_history 和 feedback）迁移到数据库
支持增量迁移，避免重复导入
自动映射 user_id 到 users 表的 UUID
"""

import json
import sys
from pathlib import Path
from datetime import datetime
from typing import Tuple, Optional
from loguru import logger

# 添加项目根目录到 Python 路径
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root / "src"))

from ai_assistant.core.database import DatabaseManager
from ai_assistant.core.user_manager import UserManager
from ai_assistant.models import ChatHistoryDB, FeedbackDB


def map_user_id_to_uuid(user_manager: UserManager, old_user_id: str, user_name: str) -> Optional[str]:
    """
    将旧的 user_id（飞书 open_id 或域账号）映射到 users 表的 UUID

    Args:
        user_manager: 用户管理器
        old_user_id: 旧的 user_id（飞书 open_id）
        user_name: 用户名（域账号）

    Returns:
        UUID 字符串，如果找不到返回 None
    """
    try:
        # 1. 先通过 feishu_open_id 查找
        if old_user_id:
            user = user_manager.get_user_by_feishu_open_id(old_user_id)
            if user:
                return user['user_id']  # 返回 dict，用 ['key'] 访问

        # 2. 再通过 username 查找
        if user_name:
            user = user_manager.get_user(user_name)  # 使用 get_user() 方法
            if user:
                return user['user_id']  # 返回 dict，用 ['key'] 访问

        # 3. 用户不存在，自动创建（不发送飞书通知）
        if old_user_id or user_name:
            import string
            import random

            # 生成随机密码
            def generate_random_password(length=12):
                chars = string.ascii_letters + string.digits
                return ''.join(random.choice(chars) for _ in range(length))

            # 从 user_name 提取 username 和 display_name
            if user_name and ' ' in user_name:
                # "袁荣杰 rjyuan2" -> username="rjyuan2", display_name="袁荣杰"
                parts = user_name.split()
                display_name = parts[0]
                username = parts[-1]
            elif user_name:
                username = user_name
                display_name = user_name
            else:
                # 只有 open_id，没有 user_name
                username = f"user_{old_user_id[:8]}"
                display_name = "未知用户"

            # 创建用户
            user_id = user_manager.create_user(
                username=username,
                password=generate_random_password(),
                feishu_open_id=old_user_id if old_user_id else None,
                display_name=display_name,
                role="user"
            )

            if user_id:
                logger.info(f"自动创建用户: {username} ({display_name}), feishu_open_id={old_user_id}")
                # 重新查询获取 UUID
                user = user_manager.get_user_by_feishu_open_id(old_user_id) if old_user_id else user_manager.get_user(username)
                if user:
                    return user['user_id']

        # 找不到用户
        return None
    except Exception as e:
        logger.error(f"映射 user_id 失败: old_user_id={old_user_id}, user_name={user_name}, error={e}")
        return None


def migrate_chat_history(
    db_manager: DatabaseManager,
    user_manager: UserManager,
    history_dir: Path,
    batch_size: int = 100
) -> Tuple[int, int, int]:
    """
    迁移对话历史数据（映射 user_id 到 UUID）

    Args:
        db_manager: 数据库管理器
        user_manager: 用户管理器
        history_dir: chat_history 目录路径
        batch_size: 批量插入大小

    Returns:
        (成功数量, 跳过数量, 无用户数量)
    """
    if not history_dir.exists():
        logger.warning(f"对话历史目录不存在: {history_dir}")
        return 0, 0, 0

    success_count = 0
    skip_count = 0
    no_user_count = 0

    # 查找所有 .jsonl 文件
    jsonl_files = sorted(history_dir.glob("*.jsonl"))
    if not jsonl_files:
        logger.info(f"未找到对话历史文件: {history_dir}")
        return 0, 0, 0

    logger.info(f"找到 {len(jsonl_files)} 个对话历史文件")

    # 获取已存在的 record_id（用于增量迁移）
    with db_manager.get_session() as session:
        existing_ids = set(
            row[0] for row in session.query(ChatHistoryDB.record_id).all()
        )

    logger.info(f"数据库中已有 {len(existing_ids)} 条对话记录")

    # 逐个文件处理
    for jsonl_file in jsonl_files:
        logger.info(f"处理文件: {jsonl_file.name}")
        batch = []

        # 从文件名提取日期（例如 "2026-11-17.jsonl" -> "2026-11-17"）
        file_date = jsonl_file.stem

        try:
            with open(jsonl_file, "r", encoding="utf-8") as f:
                for line_no, line in enumerate(f, 1):
                    line = line.strip()
                    if not line:
                        continue

                    try:
                        record = json.loads(line)
                        record_id = record.get("record_id")

                        # 跳过已存在的记录
                        if record_id in existing_ids:
                            skip_count += 1
                            continue

                        # 修复时间：如果记录没有 timestamp，使用文件名日期
                        if not record.get("timestamp"):
                            record["timestamp"] = f"{file_date} 00:00:00"

                        # 映射 user_id 到 UUID
                        old_user_id = record.get("user_id")
                        user_name = record.get("user_name", "")

                        uuid = map_user_id_to_uuid(user_manager, old_user_id, user_name)
                        if not uuid:
                            logger.warning(f"找不到用户: user_id={old_user_id}, user_name={user_name}，跳过记录 {record_id}")
                            no_user_count += 1
                            continue

                        # 覆盖为 UUID
                        record["user_id"] = uuid

                        # 创建数据库对象
                        db_record = ChatHistoryDB.from_jsonl_record(record)
                        batch.append(db_record)

                        # 批量插入
                        if len(batch) >= batch_size:
                            with db_manager.get_session() as session:
                                session.bulk_save_objects(batch)
                            success_count += len(batch)
                            logger.debug(f"批量插入 {len(batch)} 条记录")
                            batch = []

                    except json.JSONDecodeError as e:
                        logger.warning(f"解析 JSON 失败 ({jsonl_file.name}:{line_no}): {e}")
                        continue
                    except Exception as e:
                        logger.error(f"处理记录失败 ({jsonl_file.name}:{line_no}): {e}")
                        continue

            # 插入剩余记录
            if batch:
                with db_manager.get_session() as session:
                    session.bulk_save_objects(batch)
                success_count += len(batch)
                logger.debug(f"批量插入剩余 {len(batch)} 条记录")

        except Exception as e:
            logger.error(f"读取文件失败 {jsonl_file}: {e}")
            continue

    return success_count, skip_count, no_user_count


def migrate_feedback(
    db_manager: DatabaseManager,
    user_manager: UserManager,
    feedback_dir: Path,
    batch_size: int = 100
) -> Tuple[int, int, int]:
    """
    迁移反馈数据（映射 user_id 到 UUID）

    Args:
        db_manager: 数据库管理器
        user_manager: 用户管理器
        feedback_dir: feedback 目录路径
        batch_size: 批量插入大小

    Returns:
        (成功数量, 跳过数量, 无用户数量)
    """
    if not feedback_dir.exists():
        logger.warning(f"反馈目录不存在: {feedback_dir}")
        return 0, 0, 0

    success_count = 0
    skip_count = 0
    no_user_count = 0

    # 查找所有 .jsonl 文件
    jsonl_files = sorted(feedback_dir.glob("*.jsonl"))
    if not jsonl_files:
        logger.info(f"未找到反馈文件: {feedback_dir}")
        return 0, 0, 0

    logger.info(f"找到 {len(jsonl_files)} 个反馈文件")

    # 获取已存在的 feedback_id（用于增量迁移）
    with db_manager.get_session() as session:
        existing_ids = set(
            row[0] for row in session.query(FeedbackDB.feedback_id).all()
        )

    logger.info(f"数据库中已有 {len(existing_ids)} 条反馈记录")

    # 逐个文件处理
    for jsonl_file in jsonl_files:
        logger.info(f"处理文件: {jsonl_file.name}")
        batch = []

        try:
            with open(jsonl_file, "r", encoding="utf-8") as f:
                for line_no, line in enumerate(f, 1):
                    line = line.strip()
                    if not line:
                        continue

                    try:
                        record = json.loads(line)
                        feedback_id = record.get("feedback_id")

                        # 跳过已存在的记录
                        if feedback_id in existing_ids:
                            skip_count += 1
                            continue

                        # 映射 user_id 到 UUID
                        old_user_id = record.get("user_id")
                        user_name = record.get("user_name", "")

                        uuid = map_user_id_to_uuid(user_manager, old_user_id, user_name)
                        if not uuid:
                            logger.warning(f"找不到用户: user_id={old_user_id}, user_name={user_name}，跳过记录 {feedback_id}")
                            no_user_count += 1
                            continue

                        # 覆盖为 UUID
                        record["user_id"] = uuid

                        # 创建数据库对象
                        db_record = FeedbackDB.from_jsonl_record(record)
                        batch.append(db_record)

                        # 批量插入
                        if len(batch) >= batch_size:
                            with db_manager.get_session() as session:
                                session.bulk_save_objects(batch)
                            success_count += len(batch)
                            logger.debug(f"批量插入 {len(batch)} 条记录")
                            batch = []

                    except json.JSONDecodeError as e:
                        logger.warning(f"解析 JSON 失败 ({jsonl_file.name}:{line_no}): {e}")
                        continue
                    except Exception as e:
                        logger.error(f"处理记录失败 ({jsonl_file.name}:{line_no}): {e}")
                        continue

            # 插入剩余记录
            if batch:
                with db_manager.get_session() as session:
                    session.bulk_save_objects(batch)
                success_count += len(batch)
                logger.debug(f"批量插入剩余 {len(batch)} 条记录")

        except Exception as e:
            logger.error(f"读取文件失败 {jsonl_file}: {e}")
            continue

    return success_count, skip_count, no_user_count


def main():
    """主函数"""
    logger.info("=" * 60)
    logger.info("开始数据迁移（方案 A：使用 UUID）")
    logger.info("=" * 60)

    # 配置数据库（默认 SQLite）
    db_manager = DatabaseManager(
        db_type="sqlite",
        db_path="./data/ai_assistant.db",
        echo=False,
    )

    # 创建表
    logger.info("创建数据库表...")
    db_manager.create_tables()

    # 初始化用户管理器
    user_manager = UserManager(db_manager)

    # 数据目录
    data_dir = Path("./data")
    history_dir = data_dir / "chat_history"
    feedback_dir = data_dir / "feedback"

    # 1. 迁移对话历史
    logger.info("\n" + "=" * 60)
    logger.info("[1/2] 迁移对话历史数据")
    logger.info("=" * 60)
    history_success, history_skip, history_no_user = migrate_chat_history(
        db_manager, user_manager, history_dir
    )
    logger.info(
        f"对话历史迁移完成: 成功 {history_success} 条, 跳过 {history_skip} 条, 无用户 {history_no_user} 条"
    )

    # 2. 迁移反馈数据
    logger.info("\n" + "=" * 60)
    logger.info("[2/2] 迁移反馈数据")
    logger.info("=" * 60)
    feedback_success, feedback_skip, feedback_no_user = migrate_feedback(
        db_manager, user_manager, feedback_dir
    )
    logger.info(
        f"反馈数据迁移完成: 成功 {feedback_success} 条, 跳过 {feedback_skip} 条, 无用户 {feedback_no_user} 条"
    )

    # 统计信息
    logger.info("\n" + "=" * 60)
    logger.info("迁移统计")
    logger.info("=" * 60)
    with db_manager.get_session() as session:
        from ai_assistant.models import User
        total_users = session.query(User).count()
        total_history = session.query(ChatHistoryDB).count()
        total_feedback = session.query(FeedbackDB).count()

    logger.info(f"数据库中共有:")
    logger.info(f"  - 用户: {total_users} 个")
    logger.info(f"  - 对话记录: {total_history} 条")
    logger.info(f"  - 反馈记录: {total_feedback} 条")
    logger.info("=" * 60)


if __name__ == "__main__":
    main()
