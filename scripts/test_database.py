"""
数据库功能测试脚本

用于验证数据库连接、ORM 模型、CRUD 操作是否正常
运行前需要先安装依赖: pip install -r requirements.txt
"""

import sys
from pathlib import Path
from datetime import datetime

# 添加项目根目录到 Python 路径
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root / "src"))

from ai_assistant.core.database import DatabaseManager
from ai_assistant.models import User, ChatHistoryDB, FeedbackDB
from ai_assistant.models.user import UserRole
from ai_assistant.models.feedback_db import FeedbackType


def test_database_connection():
    """测试数据库连接"""
    print("\n" + "=" * 60)
    print("测试 1: 数据库连接")
    print("=" * 60)

    db_manager = DatabaseManager(
        db_type="sqlite",
        db_path="./data/test_db.db",
        echo=True,  # 打印 SQL 语句
    )

    print("✓ 数据库连接成功")
    return db_manager


def test_create_tables(db_manager: DatabaseManager):
    """测试创建表"""
    print("\n" + "=" * 60)
    print("测试 2: 创建数据库表")
    print("=" * 60)

    db_manager.create_tables()
    print("✓ 数据库表创建成功")


def test_user_model(db_manager: DatabaseManager):
    """测试 User 模型"""
    print("\n" + "=" * 60)
    print("测试 3: User 模型 CRUD")
    print("=" * 60)

    # 创建用户
    with db_manager.get_session() as session:
        user = User(
            user_id="test_user_001",
            username="testuser",
            name="测试用户",
            role=UserRole.USER,
            feishu_open_id="ou_abc123",
        )
        user.set_password("password123")
        session.add(user)

    print("✓ 创建用户成功")

    # 查询用户
    with db_manager.get_session() as session:
        user = session.query(User).filter_by(username="testuser").first()
        print(f"  查询到用户: {user}")
        print(f"  密码验证: {user.check_password('password123')}")
        assert user.check_password("password123"), "密码验证失败"
        assert not user.check_password("wrong_password"), "密码验证应该失败"

    print("✓ 用户查询和密码验证成功")


def test_chat_history_model(db_manager: DatabaseManager):
    """测试 ChatHistory 模型"""
    print("\n" + "=" * 60)
    print("测试 4: ChatHistory 模型 CRUD")
    print("=" * 60)

    # 创建对话记录
    with db_manager.get_session() as session:
        record = ChatHistoryDB(
            record_id="record_001",
            timestamp=datetime.now(),
            session_id="chat_123",
            source="feishu",
            query="这是一个测试问题",
            answer="这是一个测试回答",
            latency_ms=1500,
            user_id="test_user_001",
            user_name="测试用户",
            context_key="root_123",
            message_id="msg_001",
            metadata={"mode": "agentic", "tool_rounds": 2},
        )
        session.add(record)

    print("✓ 创建对话记录成功")

    # 查询对话记录
    with db_manager.get_session() as session:
        records = session.query(ChatHistoryDB).filter_by(session_id="chat_123").all()
        print(f"  查询到 {len(records)} 条记录")
        for r in records:
            print(f"    - {r.record_id}: {r.query[:20]}...")

    print("✓ 对话记录查询成功")


def test_feedback_model(db_manager: DatabaseManager):
    """测试 Feedback 模型"""
    print("\n" + "=" * 60)
    print("测试 5: Feedback 模型 CRUD")
    print("=" * 60)

    # 创建反馈记录
    with db_manager.get_session() as session:
        feedback = FeedbackDB(
            feedback_id="feedback_001",
            record_id="record_001",
            timestamp=datetime.now(),
            session_id="chat_123",
            source="feishu",
            feedback_type=FeedbackType.LIKE,
            feedback_text="",
            user_id="test_user_001",
            user_name="测试用户",
        )
        session.add(feedback)

    print("✓ 创建反馈记录成功")

    # 查询反馈记录
    with db_manager.get_session() as session:
        feedbacks = session.query(FeedbackDB).filter_by(record_id="record_001").all()
        print(f"  查询到 {len(feedbacks)} 条反馈")
        for f in feedbacks:
            print(f"    - {f.feedback_id}: {f.feedback_type.value}")

    print("✓ 反馈记录查询成功")


def test_jsonl_conversion():
    """测试 JSONL 转换"""
    print("\n" + "=" * 60)
    print("测试 6: JSONL 记录转换")
    print("=" * 60)

    # 测试 ChatHistory 转换
    jsonl_record = {
        "record_id": "record_002",
        "timestamp": "2024-01-15 10:30:00",
        "session_id": "chat_456",
        "source": "feishu",
        "query": "测试问题",
        "answer": "测试回答",
        "latency_ms": 2000,
        "user_id": "user_002",
        "user_name": "用户2",
        "context_key": "root_456",
        "message_id": "msg_002",
        "metadata": {"mode": "normal"},
    }

    db_record = ChatHistoryDB.from_jsonl_record(jsonl_record)
    print(f"✓ ChatHistory 转换成功: {db_record.record_id}")

    # 测试 Feedback 转换
    feedback_jsonl = {
        "feedback_id": "feedback_002",
        "record_id": "record_002",
        "timestamp": "2024-01-15 10:35:00",
        "session_id": "chat_456",
        "source": "feishu",
        "feedback_type": "dislike",
        "feedback_text": "回答不够准确",
        "user_id": "user_002",
        "user_name": "用户2",
    }

    db_feedback = FeedbackDB.from_jsonl_record(feedback_jsonl)
    print(f"✓ Feedback 转换成功: {db_feedback.feedback_id}")


def main():
    """主测试流程"""
    print("=" * 60)
    print("数据库功能测试")
    print("=" * 60)

    try:
        # 1. 测试数据库连接
        db_manager = test_database_connection()

        # 2. 测试创建表
        test_create_tables(db_manager)

        # 3. 测试 User 模型
        test_user_model(db_manager)

        # 4. 测试 ChatHistory 模型
        test_chat_history_model(db_manager)

        # 5. 测试 Feedback 模型
        test_feedback_model(db_manager)

        # 6. 测试 JSONL 转换
        test_jsonl_conversion()

        print("\n" + "=" * 60)
        print("所有测试通过！")
        print("=" * 60)

        # 清理
        db_manager.close()

        # 删除测试数据库
        import os
        test_db_path = "./data/test_db.db"
        if os.path.exists(test_db_path):
            os.remove(test_db_path)
            print(f"\n已清理测试数据库: {test_db_path}")

    except Exception as e:
        print(f"\n测试失败: {e}")
        import traceback
        traceback.print_exc()
        return 1

    return 0


if __name__ == "__main__":
    exit(main())
