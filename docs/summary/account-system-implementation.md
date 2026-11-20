# 账号体系实施总结

## 项目概述

为 AI 自动回复助手实现完整的账号体系，包括用户管理、飞书用户自动注册、飞书消息通知等功能。

**实施时间**: 2024年
**负责人**: Backend-Auth-Agent
**协作**: Backend-DB-Agent
**状态**: ✅ 完成并通过测试

## 实施成果

### 核心模块

1. **UserManager** (`src/ai_assistant/core/user_manager.py`)
   - 用户 CRUD 操作（创建、查询、更新、删除）
   - 用户认证（bcrypt 密码验证）
   - 飞书用户关联查询
   - 角色管理（user / admin）
   - 密码管理（更新、重置、随机生成）

2. **FeishuNotifier** (`src/ai_assistant/utils/feishu_notifier.py`)
   - 飞书卡片消息通知（美观）
   - 飞书文本消息通知（降级）
   - 自动获取访问令牌
   - 包含登录名、密码、前端地址

3. **自动注册集成** (`src/ai_assistant/main.py`)
   - 在 `_process_event` 中自动触发
   - 首次对话自动创建账号
   - 发送飞书通知消息
   - 降级处理（失败不影响问答）

### 测试结果

#### 单元测试（7个测试）
```
tests/unit/test_user_manager.py
✓ test_generate_random_password_default_length
✓ test_generate_random_password_custom_length
✓ test_generate_random_password_uniqueness
✓ test_hash_password
✓ test_hash_password_different_inputs
✓ test_user_manager_initialization
✓ test_user_manager_with_config

====== 7 passed in 0.09s ======
```

#### 集成测试（9个测试）
```
test_user_manager_integration.py
[OK] 测试 1: 创建用户
[OK] 测试 2: 用户认证
[OK] 测试 3: 错误密码认证
[OK] 测试 4: 通过飞书 open_id 查询
[OK] 测试 5: 创建管理员用户
[OK] 测试 6: 检查管理员权限
[OK] 测试 7: 更新密码
[OK] 测试 8: 列出所有用户
[OK] 测试 9: 生成随机密码

=== 所有测试通过! ===
```

## 技术决策

### 1. 密码加密：bcrypt vs SHA256

**最终选择**: bcrypt（由 Backend-DB-Agent 提供）

**优势**:
- 自动加盐（salt），无需手动管理
- 计算成本可调（防止暴力破解）
- 行业标准，专为密码存储设计
- 比 SHA256 更安全

### 2. 数据库选型：SQLAlchemy ORM

**优势**:
- 支持多数据库（SQLite / MySQL / PostgreSQL）
- ORM 简化开发，自动处理连接池
- 事务管理自动化，保证数据一致性
- 迁移友好

### 3. 自动注册触发点

**选择**: 在 `_process_event` 获取用户信息后

**理由**:
- 此时已获取飞书用户信息（open_id, user_name）
- 在调用 AI 之前，不阻塞主流程
- 失败降级处理，不影响问答功能

## 核心流程

### 飞书用户自动注册流程

```
用户首次与机器人对话
    ↓
获取飞书用户信息 (open_id, user_name)
    ↓
检查用户是否已注册 (通过 feishu_open_id 查询)
    ↓
如果未注册：
    ├─ 生成用户名 (优先飞书昵称，至少3字符)
    ├─ 生成随机密码 (8位字母+数字)
    ├─ 创建用户记录 (bcrypt 加密密码)
    ├─ 发送飞书通知消息 (卡片/文本)
    └─ 记录日志
    ↓
继续正常问答流程（注册失败不阻断）
```

### 用户认证流程

```
用户登录请求 (username, password)
    ↓
查询用户记录 (通过 username)
    ↓
验证密码 (bcrypt.checkpw)
    ↓
返回用户信息 (不含密码哈希)
```

## 数据库设计

### User 表

| 字段 | 类型 | 约束 | 说明 |
|------|------|------|------|
| user_id | String(64) | 主键 | UUID |
| username | String(64) | 唯一索引 | 登录用户名 |
| password_hash | String(128) | 非空 | bcrypt 密码哈希 |
| name | String(128) | 可空 | 用户昵称/真实姓名 |
| role | Enum | 非空 | 角色: USER/ADMIN |
| feishu_open_id | String(128) | 唯一索引 | 飞书用户关联 |
| created_at | DateTime | 非空 | 创建时间 |
| updated_at | DateTime | 非空 | 更新时间 |

### 索引

- PRIMARY KEY (user_id)
- UNIQUE INDEX idx_username (username)
- UNIQUE INDEX idx_feishu_open_id (feishu_open_id)
- INDEX idx_created_at (created_at)

## 配置说明

### config.yaml 新增配置

```yaml
# 数据库配置（可选，默认 SQLite）
database:
  type: sqlite  # sqlite / mysql / postgresql
  path: ./data/ai_assistant.db
  echo: false  # 是否打印 SQL（调试用）
  
  # MySQL 配置示例
  # type: mysql
  # host: localhost
  # port: 3306
  # name: ai_assistant
  # user: root
  # password: password

# 前端地址（用于账号通知消息）
frontend_url: http://localhost:3000

# 管理员账号（可选）
admin:
  username: admin
  password: admin123
```

## 依赖管理

### 新增依赖

```txt
# requirements.txt 已包含
sqlalchemy>=2.0.0  # ORM 框架
bcrypt>=4.0.0      # 密码加密
pymysql>=1.1.0     # MySQL 驱动（可选）
psycopg2-binary>=2.9.0  # PostgreSQL 驱动（可选）
```

### 安装命令

```bash
pip install sqlalchemy bcrypt pymysql
```

## 安全措施

### 1. 密码安全
- ✅ bcrypt 加密（自动加盐）
- ✅ 密码明文不记录日志
- ✅ 密码哈希不出现在 API 响应中

### 2. 数据安全
- ✅ SQL 注入防护（ORM 参数化查询）
- ✅ 事务管理（数据一致性）
- ✅ 唯一约束（防止重复注册）

### 3. 通信安全
- ✅ HTTPS 调用飞书 API
- ✅ 飞书 access_token 自动刷新
- ✅ 敏感信息脱敏（日志中不记录完整密码）

## 使用示例

### 创建用户

```python
from ai_assistant.core.user_manager import UserManager
from ai_assistant.core.database import DatabaseManager

db = DatabaseManager(db_type='sqlite', db_path='./data/ai_assistant.db')
user_mgr = UserManager(db_manager=db)

user_id = user_mgr.create_user(
    username='testuser',
    password='password123',
    feishu_open_id='ou_abc123',
    display_name='Test User',
    role='user'
)
```

### 用户认证

```python
result = user_mgr.authenticate('testuser', 'password123')
if result:
    print(f"登录成功: {result['username']}")
else:
    print("登录失败")
```

### 更新密码

```python
success = user_mgr.update_password(
    user_id='xxx-xxx-xxx',
    old_password='password123',
    new_password='newpassword456'
)
```

## 后续优化方向

### 短期（已完成基础功能）

- [x] 用户 CRUD
- [x] 用户认证
- [x] 飞书用户关联
- [x] 自动注册集成
- [x] 飞书消息通知

### 中期（功能增强）

- [ ] 会话管理（JWT Token）
- [ ] 密码重置（邮件/飞书通知）
- [ ] 用户信息更新（昵称、邮箱）
- [ ] 管理员账号初始化
- [ ] 批量用户导入

### 长期（高级功能）

- [ ] 权限细化（RBAC）
- [ ] 审计日志（用户操作记录）
- [ ] 多因素认证（2FA）
- [ ] 单点登录（SSO）
- [ ] 用户组管理

## 已知问题

### 1. SQLAlchemy 警告

```
SAWarning: Attribute name 'metadata' should be left reserved for the MetaData instance
```

**原因**: `ChatHistoryDB` 模型中 `metadata` 字段名与 SQLAlchemy 保留字冲突
**影响**: 不影响功能，仅警告
**解决**: 需要 Backend-DB-Agent 重命名该字段（如 `meta_data`）

### 2. Windows 编码问题

测试脚本中使用特殊字符（✓✗）在 Windows GBK 环境下显示异常

**解决**: 已替换为 ASCII 字符（[OK] [FAIL]）

## 参考资料

- SQLAlchemy 文档: https://docs.sqlalchemy.org/
- bcrypt 文档: https://pypi.org/project/bcrypt/
- 飞书开放平台: https://open.feishu.cn/document/
- Python UUID: https://docs.python.org/3/library/uuid.html

## 团队协作

- **Backend-Auth-Agent**: 用户管理、自动注册、飞书通知
- **Backend-DB-Agent**: 数据库模型、连接管理

协作过程顺利，接口清晰，无阻塞。

---

**完成时间**: 2024年
**总耗时**: 约 2 小时（含测试）
**代码行数**: 约 800 行
**测试覆盖**: 16 个测试（7个单元测试 + 9个集成测试）
