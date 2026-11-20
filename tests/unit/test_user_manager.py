"""
UserManager 单元测试

测试用户管理器的核心能力（工具方法）
"""

import pytest
from src.ai_assistant.core.user_manager import UserManager


class TestUserManager:
    """UserManager 测试类"""

    def test_generate_random_password_default_length(self):
        """测试生成随机密码（默认 8 位）"""
        password = UserManager.generate_random_password()
        assert len(password) == 8
        # 验证只包含字母和数字
        assert password.isalnum()

    def test_generate_random_password_custom_length(self):
        """测试生成随机密码（自定义长度）"""
        password = UserManager.generate_random_password(12)
        assert len(password) == 12
        assert password.isalnum()

    def test_generate_random_password_uniqueness(self):
        """测试生成的密码具有唯一性"""
        passwords = [UserManager.generate_random_password() for _ in range(100)]
        # 100 个密码应该都不相同
        assert len(set(passwords)) == 100

    def test_hash_password(self):
        """测试密码加密"""
        password = "test123"
        hashed = UserManager.hash_password(password)

        # SHA256 输出固定为 64 个十六进制字符
        assert len(hashed) == 64
        assert all(c in '0123456789abcdef' for c in hashed)

        # 同样的密码应该产生相同的哈希
        hashed2 = UserManager.hash_password(password)
        assert hashed == hashed2

    def test_hash_password_different_inputs(self):
        """测试不同密码产生不同哈希"""
        hash1 = UserManager.hash_password("password1")
        hash2 = UserManager.hash_password("password2")
        assert hash1 != hash2

    def test_user_manager_initialization(self):
        """测试 UserManager 初始化"""
        manager = UserManager()
        assert manager.db_connection is None
        assert manager.admin_config == {}

    def test_user_manager_with_config(self):
        """测试 UserManager 带配置初始化"""
        admin_config = {"username": "admin", "password": "admin123"}
        manager = UserManager(admin_config=admin_config)
        assert manager.admin_config == admin_config
