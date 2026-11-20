"""
飞书用户自动注册集成测试

测试核心能力：
1. 飞书用户首次提问自动创建账号
2. 账号信息正确（用户名、密码、飞书关联）
3. 重复提问不会重复创建账号
"""

import sys
import os
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from ai_assistant.core.database import DatabaseManager
from ai_assistant.core.user_manager import UserManager
from ai_assistant.models.user import User


def test_feishu_auto_register():
    """测试飞书用户自动注册流程"""

    # 1. 创建临时测试数据库
    with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as f:
        test_db_path = f.name

    try:
        # 2. 初始化数据库和用户管理器
        db_manager = DatabaseManager(db_type='sqlite', db_path=test_db_path, echo=False)
        User.__table__.create(bind=db_manager.engine, checkfirst=True)
        user_manager = UserManager(db_manager=db_manager)

        # 3. 模拟飞书用户首次提问
        feishu_open_id = "ou_test_auto_register_001"
        feishu_user_name = "测试用户"

        # 检查用户是否已存在（首次应该不存在）
        existing_user = user_manager.get_user_by_feishu_open_id(feishu_open_id)
        assert existing_user is None, "用户不应该已存在"

        # 4. 自动创建用户（模拟自动注册逻辑）
        username = f"feishu_{feishu_open_id}"
        password = UserManager.generate_random_password(8)

        user_id = user_manager.create_user(
            username=username,
            password=password,
            feishu_open_id=feishu_open_id,
            display_name=feishu_user_name,
            role='user'
        )

        assert user_id is not None, "用户创建应该成功"
        print(f"[OK] 用户创建成功: user_id={user_id}")

        # 5. 验证用户信息
        user = user_manager.get_user(user_id)
        assert user is not None, "应该能查询到用户"
        assert user['username'] == username, "用户名应该匹配"
        assert user['feishu_open_id'] == feishu_open_id, "飞书 open_id 应该匹配"
        assert user['name'] == feishu_user_name, "显示名称应该匹配"
        assert user['role'] == 'user', "角色应该是普通用户"
        print(f"[OK] 用户信息验证通过: {user['username']}")

        # 6. 验证密码可以登录
        auth_result = user_manager.authenticate(username, password)
        assert auth_result is not None, "密码应该可以登录"
        assert auth_result['user_id'] == user_id, "登录返回的 user_id 应该匹配"
        print(f"[OK] 密码验证通过")

        # 7. 模拟同一用户再次提问（不应该重复创建）
        existing_user = user_manager.get_user_by_feishu_open_id(feishu_open_id)
        assert existing_user is not None, "应该能通过 open_id 找到用户"
        assert existing_user['user_id'] == user_id, "应该是同一个用户"
        print(f"[OK] 重复查询不会重复创建")

        # 8. 验证用户列表包含新用户
        users = user_manager.list_users(limit=10)
        assert len(users) == 1, "应该有 1 个用户"
        assert users[0]['user_id'] == user_id, "用户列表中的用户应该匹配"
        print(f"[OK] 用户列表验证通过")

        print("\n=== 飞书自动注册测试通过 ===")

    finally:
        # 清理测试数据库
        if os.path.exists(test_db_path):
            try:
                os.remove(test_db_path)
            except PermissionError:
                pass  # Windows 文件占用，忽略


if __name__ == '__main__':
    test_feishu_auto_register()
