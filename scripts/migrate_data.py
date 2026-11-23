"""
数据迁移脚本

将现有 JSONL 数据（chat_history 和 feedback）迁移到数据库
支持增量迁移，避免重复导入
自动创建历史用户记录
"""

import json
import sys
from pathlib import Path
from datetime import datetime
from typing import List, Tuple, Set, Dict
from loguru import logger

# 添加项目根目录到 Python 路径
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root / "src"))

from ai_assistant.core.database import DatabaseManager
from ai_assistant.core.user_manager import UserManager
from ai_assistant.models import ChatHistoryDB, FeedbackDB, User


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


def extract_users_from_jsonl(
    history_dir: Path,
    feedback_dir: Path
) -> Set[Tuple[str, str]]:
    """
    从 JSONL 文件提取所有唯一用户（user_id, user_name）

    Args:
        history_dir: chat_history 目录路径
        feedback_dir: feedback 目录路径

    Returns:
        Set of (user_id, user_name) tuples
    """
    users = set()

    # 从 chat_history 提取
    if history_dir.exists():
        for jsonl_file in history_dir.glob("*.jsonl"):
            try:
                with open(jsonl_file, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            record = json.loads(line)
                            user_id = record.get("user_id")
                            user_name = record.get("user_name", "")

                            # 提取域账号
                            if user_name and " " in user_name:
                                user_name = user_name.split()[-1]

                            if user_id and user_name:
                                users.add((user_id, user_name))
                        except json.JSONDecodeError:
                            continue
            except Exception as e:
                logger.warning(f"读取文件失败 {jsonl_file}: {e}")
                continue

    # 从 feedback 提取
    if feedback_dir.exists():
        for jsonl_file in feedback_dir.glob("*.jsonl"):
            try:
                with open(jsonl_file, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            record = json.loads(line)
                            user_id = record.get("user_id")
                            user_name = record.get("user_name", "")

                            # 提取域账号
                            if user_name and " " in user_name:
                                user_name = user_name.split()[-1]

                            if user_id and user_name:
                                users.add((user_id, user_name))
                        except json.JSONDecodeError:
                            continue
            except Exception as e:
                logger.warning(f"读取文件失败 {jsonl_file}: {e}")
                continue

    return users


def send_feishu_password_notification(open_id: str, username: str, password: str) -> bool:
    """
    发送飞书消息通知用户密码

    Args:
        open_id: 用户的 open_id
        username: 用户名
        password: 临时密码

    Returns:
        True if successful, False otherwise
    """
    import requests
    import yaml
    from pathlib import Path

    try:
        # 读取配置
        config_file = Path("config.yaml")
        if not config_file.exists():
            logger.warning("配置文件不存在，跳过飞书通知")
            return False

        with open(config_file, 'r', encoding='utf-8') as f:
            config = yaml.safe_load(f)

        feishu_config = config.get('feishu', {})
        app_id = feishu_config.get('app_id')
        app_secret = feishu_config.get('app_secret')
        base_url = feishu_config.get('base_url', 'https://open.feishu.cn')

        if not app_id or not app_secret:
            logger.warning("飞书配置不完整，跳过密码通知")
            return False

        # 获取 tenant_access_token
        token_url = f"{base_url}/open-apis/auth/v3/tenant_access_token/internal"
        token_response = requests.post(
            token_url,
            json={"app_id": app_id, "app_secret": app_secret},
            timeout=10
        )
        token_result = token_response.json()

        if token_result.get('code') != 0:
            logger.warning(f"获取飞书 token 失败: {token_result.get('msg')}")
            return False

        tenant_access_token = token_result.get('tenant_access_token')

        # 发送消息
        message_url = f"{base_url}/open-apis/im/v1/messages"

        # 消息内容
        message_text = f"""🔐 您的管理后台账号已创建

用户名：{username}
临时密码：{password}

管理后台地址：http://your-server:8080/admin/

⚠️ 请首次登录后立即修改密码

如有问题，请联系管理员。"""

        message_body = {
            "receive_id": open_id,
            "msg_type": "text",
            "content": json.dumps({"text": message_text})
        }

        headers = {
            "Authorization": f"Bearer {tenant_access_token}",
            "Content-Type": "application/json"
        }

        params = {"receive_id_type": "open_id"}

        message_response = requests.post(
            message_url,
            headers=headers,
            params=params,
            json=message_body,
            timeout=10
        )
        message_result = message_response.json()

        if message_result.get('code') == 0:
            logger.info(f"飞书密码通知发送成功: username={username}, open_id={open_id[:8]}...")
            return True
        else:
            logger.warning(f"飞书密码通知发送失败: {message_result.get('msg')}")
            return False

    except Exception as e:
        logger.warning(f"发送飞书密码通知异常: {e}")
        return False


def migrate_users(
    user_manager: UserManager,
    users: Set[Tuple[str, str]]
) -> Tuple[int, int]:
    """
    批量创建历史用户

    Args:
        user_manager: 用户管理器
        users: Set of (user_id/feishu_open_id, username) tuples

    Returns:
        (成功数量, 跳过数量)
    """
    success_count = 0
    skip_count = 0

    logger.info(f"开始迁移 {len(users)} 个唯一用户")

    for user_id, username in users:
        try:
            # 检查用户是否已存在（通过 feishu_open_id 或 username）
            existing_user = user_manager.get_user_by_feishu_open_id(user_id)
            if not existing_user:
                existing_user = user_manager.get_user(user_id)  # 也检查 user_id

            if existing_user:
                skip_count += 1
                continue

            # 创建用户（使用随机密码）
            password = user_manager.generate_random_password(8)
            new_user_id = user_manager.create_user(
                username=username,
                password=password,
                feishu_open_id=user_id,  # 将 user_id 作为 feishu_open_id
                display_name=username,
                role="user"
                # 注意：create_user 不支持 is_active 参数，新用户默认 is_active=True
            )

            if new_user_id:
                success_count += 1
                logger.debug(f"创建用户: username={username}, feishu_open_id={user_id}")

                # 发送飞书消息通知密码
                send_feishu_password_notification(user_id, username, password)
            else:
                logger.warning(f"创建用户失败: username={username}")

        except Exception as e:
            logger.error(f"创建用户异常 ({username}, {user_id}): {e}")
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

    # 初始化用户管理器
    user_manager = UserManager(db_manager)

    # 数据目录
    history_dir = Path("./data/chat_history")
    feedback_dir = Path("./data/feedback")

    # 1. 提取所有用户
    logger.info("\n[1/4] 提取历史用户...")
    users = extract_users_from_jsonl(history_dir, feedback_dir)
    logger.info(f"提取到 {len(users)} 个唯一用户")

    # 2. 迁移用户
    logger.info("\n[2/4] 迁移用户...")
    user_success, user_skip = migrate_users(user_manager, users)
    logger.info(f"用户迁移完成: 成功 {user_success}, 跳过 {user_skip}")

    # 3. 迁移对话历史
    logger.info("\n[3/4] 迁移对话历史...")
    history_success, history_skip = migrate_chat_history(
        db_manager, history_dir, batch_size=100
    )
    logger.info(f"对话历史迁移完成: 成功 {history_success}, 跳过 {history_skip}")

    # 4. 迁移反馈
    logger.info("\n[4/4] 迁移反馈...")
    feedback_success, feedback_skip = migrate_feedback(
        db_manager, feedback_dir, batch_size=100
    )
    logger.info(f"反馈迁移完成: 成功 {feedback_success}, 跳过 {feedback_skip}")

    # 总结
    logger.info("\n" + "=" * 60)
    logger.info("数据迁移完成")
    logger.info("=" * 60)
    logger.info(f"用户:     成功 {user_success}, 跳过 {user_skip}")
    logger.info(f"对话历史: 成功 {history_success}, 跳过 {history_skip}")
    logger.info(f"反馈:     成功 {feedback_success}, 跳过 {feedback_skip}")
    logger.info("=" * 60)

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
