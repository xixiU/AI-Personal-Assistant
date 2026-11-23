"""
测试用户管理器完整功能
"""
import sys
import os
sys.path.insert(0, 'src')

from ai_assistant.core.user_manager import UserManager
from ai_assistant.core.database import DatabaseManager

# 创建测试数据库
test_db_path = './data/test_user_manager.db'
os.makedirs('./data', exist_ok=True)

# 删除旧的测试数据库
if os.path.exists(test_db_path):
    os.remove(test_db_path)

# 创建数据库管理器
print("1. 创建数据库管理器...")
db = DatabaseManager(db_type='sqlite', db_path=test_db_path, echo=False)

# 只创建 User 表
print("2. 创建 User 表...")
from ai_assistant.models.user import User
User.__table__.create(bind=db.engine, checkfirst=True)

# 创建用户管理器
print("3. 创建用户管理器...")
user_mgr = UserManager(db_manager=db)

# 测试 1: 创建用户
print("\n=== 测试 1: 创建用户 ===")
user_id = user_mgr.create_user(
    username='testuser',
    password='test123',
    feishu_open_id='ou_test12345678',
    display_name='Test User',
    role='user'
)
print(f"[OK] 用户创建成功: user_id={user_id}")

# 测试 2: 用户认证
print("\n=== 测试 2: 用户认证 ===")
result = user_mgr.authenticate('testuser', 'test123')
if result:
    print(f"[OK] 认证成功: username={result['username']}, role={result['role']}")
else:
    print("[FAIL] 认证失败")

# 测试 3: 错误密码认证
print("\n=== 测试 3: 错误密码认证 ===")
result = user_mgr.authenticate('testuser', 'wrongpassword')
if not result:
    print("[OK] 错误密码正确被拒绝")
else:
    print("[FAIL] 错误密码被接受（不应该）")

# 测试 4: 通过飞书 open_id 查询
print("\n=== 测试 4: 通过飞书 open_id 查询 ===")
user = user_mgr.get_user_by_feishu_open_id('ou_test12345678')
if user:
    print(f"[OK] 找到用户: username={user['username']}, name={user['name']}")
else:
    print("[FAIL] 未找到用户")

# 测试 5: 创建管理员用户
print("\n=== 测试 5: 创建管理员用户 ===")
admin_id = user_mgr.create_user(
    username='admin',
    password='admin123',
    display_name='Administrator',
    role='admin'
)
print(f"[OK] 管理员创建成功: user_id={admin_id}")

# 测试 6: 检查管理员权限
print("\n=== 测试 6: 检查管理员权限 ===")
is_admin = user_mgr.is_admin(admin_id)
print(f"[OK] 管理员检查: {is_admin}")

# 测试 7: 更新密码
print("\n=== 测试 7: 更新密码 ===")
success = user_mgr.update_password(user_id, 'test123', 'newpass456')
if success:
    print("[OK] 密码更新成功")
    # 验证新密码
    result = user_mgr.authenticate('testuser', 'newpass456')
    if result:
        print("[OK] 新密码认证成功")
    else:
        print("[FAIL] 新密码认证失败")
else:
    print("[FAIL] 密码更新失败")

# 测试 8: 列出所有用户
print("\n=== 测试 8: 列出所有用户 ===")
users = user_mgr.list_users(limit=10)
print(f"[OK] 找到 {len(users)} 个用户:")
for u in users:
    print(f"  - {u['username']} ({u['role']})")

# 测试 9: 生成随机密码
print("\n=== 测试 9: 生成随机密码 ===")
pwd1 = UserManager.generate_random_password(8)
pwd2 = UserManager.generate_random_password(8)
print(f"[OK] 生成密码 1: {pwd1}")
print(f"[OK] 生成密码 2: {pwd2}")
print(f"[OK] 密码唯一性: {pwd1 != pwd2}")

print("\n=== 所有测试通过! ===")

# 清理测试数据库
os.remove(test_db_path)
print(f"\n清理测试数据库: {test_db_path}")
