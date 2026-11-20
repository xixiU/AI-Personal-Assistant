"""
数据迁移脚本

将现有 JSONL 数据（chat_history 和 feedback）迁移到数据库
支持增量迁移，避免重复导入
"""

import json
import sys
from pathlib import Path
from datetime import datetime
from typing import List, Tuple
from loguru import logger

# 添加项目根目录到 Python 路径
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root / "src"))

from ai_assistant.core.database import DatabaseManager
from ai_assistant.models import ChatHistoryDB, FeedbackDB


def migrate_chat_history(
    db_manager: DatabaseManager,
    history_dir: Path,
    batch_size: int = 100
) -> Tuple[int, int]:
    """
    迁移对话历史数据

    Args:
        db_manager: 数据库管理器
        history_dir: chat_history 目录路径
        batch_size: 批量插入大小

    Returns:
        (成功数量, 跳过数量)
    """
    if not history_dir.exists():
        logger.warning(f"对话历史目录不存在: {history_dir}")
        return 0, 0

    success_count = 0
    skip_count = 0

    # 查找所有 .jsonl 文件
    jsonl_files = sorted(history_dir.glob("*.jsonl"))
    if not jsonl_files:
        logger.info(f"未找到对话历史文件: {history_dir}")
        return 0, 0

    logger.info(f"找到 {len(jsonl_files)} 个对话历史文件")

    # 获取已存在的 record_id（用于增量迁移）
    with db_manager.get_session() as session:
        existing_ids = set(
            row[0] for row in session.query(ChatHistoryDB.record_id).all()
        )
    logger.info(f"数据库中已有 {len(existing_ids)} 条对话记录")

    # 遍历所有文件
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
                        record_id = record.get("record_id")

                        # 增量迁移：跳过已存在的记录
                        if record_id in existing_ids:
                            skip_count += 1
                            continue

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
                logger.debug(f"批量插入 {len(batch)} 条记录")

        except Exception as e:
            logger.error(f"读取文件失败 {jsonl_file}: {e}")
            continue

    return success_count, skip_count


def migrate_feedback(
    db_manager: DatabaseManager,
    feedback_dir: Path,
    batch_size: int = 100
) -> Tuple[int, int]:
    """
    迁移反馈数据

    Args:
        db_manager: 数据库管理器
        feedback_dir: feedback 目录路径
        batch_size: 批量插入大小

    Returns:
        (成功数量, 跳过数量)
    """
    if not feedback_dir.exists():
        logger.warning(f"反馈目录不存在: {feedback_dir}")
        return 0, 0

    success_count = 0
    skip_count = 0

    # 查找所有 .jsonl 文件
    jsonl_files = sorted(feedback_dir.glob("*.jsonl"))
    if not jsonl_files:
        logger.info(f"未找到反馈文件: {feedback_dir}")
        return 0, 0

    logger.info(f"找到 {len(jsonl_files)} 个反馈文件")

    # 获取已存在的 feedback_id（用于增量迁移）
    with db_manager.get_session() as session:
        existing_ids = set(
            row[0] for row in session.query(FeedbackDB.feedback_id).all()
        )
    logger.info(f"数据库中已有 {len(existing_ids)} 条反馈记录")

    # 遍历所有文件
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

                        # 增量迁移：跳过已存在的记录
                        if feedback_id in existing_ids:
                            skip_count += 1
                            continue

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
                logger.debug(f"批量插入 {len(batch)} 条记录")

        except Exception as e:
            logger.error(f"读取文件失败 {jsonl_file}: {e}")
            continue

    return success_count, skip_count


def main():
    """主函数"""
    logger.info("=" * 60)
    logger.info("开始数据迁移")
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

    # 数据目录
    data_dir = Path("./data")
    history_dir = data_dir / "chat_history"
    feedback_dir = data_dir / "feedback"

    # 迁移对话历史
    logger.info("\n" + "=" * 60)
    logger.info("迁移对话历史数据")
    logger.info("=" * 60)
    history_success, history_skip = migrate_chat_history(db_manager, history_dir)
    logger.info(
        f"对话历史迁移完成: 成功 {history_success} 条, 跳过 {history_skip} 条"
    )

    # 迁移反馈数据
    logger.info("\n" + "=" * 60)
    logger.info("迁移反馈数据")
    logger.info("=" * 60)
    feedback_success, feedback_skip = migrate_feedback(db_manager, feedback_dir)
    logger.info(
        f"反馈数据迁移完成: 成功 {feedback_success} 条, 跳过 {feedback_skip} 条"
    )

    # 统计信息
    logger.info("\n" + "=" * 60)
    logger.info("迁移统计")
    logger.info("=" * 60)
    with db_manager.get_session() as session:
        total_history = session.query(ChatHistoryDB).count()
        total_feedback = session.query(FeedbackDB).count()

    logger.info(f"数据库中共有:")
    logger.info(f"  - 对话记录: {total_history} 条")
    logger.info(f"  - 反馈记录: {total_feedback} 条")

    logger.info("\n" + "=" * 60)
    logger.info("数据迁移完成")
    logger.info("=" * 60)

    # 关闭数据库连接
    db_manager.close()


if __name__ == "__main__":
    main()
