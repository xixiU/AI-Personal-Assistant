# 账号体系集成测试计划

## 测试目标

验证账号体系核心功能的端到端流程，确保：
1. 飞书用户首次提问自动注册
2. 用户认证和密码管理
3. 管理员操作流程
4. 数据迁移脚本正确性
5. 飞书通知消息发送

## 测试原则

遵循项目规范"少写测试，只测核心能力"：
- 每项核心能力编写 1-2 条测试
- 重点验证关键路径，不追求覆盖率
- 使用接近现场数据的端到端冒烟测试

## 测试环境准备

### 1. 数据库环境
- SQLite（开发测试）：本地文件数据库，无需额外安装
- PostgreSQL（生产环境）：需要 Docker 或本地安装

### 2. 配置文件
准备测试配置 `config.test.yaml`：
```yaml
database:
  type: "sqlite"
  url: "sqlite:///./data/test.db"
  pool_size: 5

admin:
  username: "admin"
  password: "test_admin_123"

frontend_url: "http://localhost:8080"

# 飞书配置（使用测试 app）
adapters:
  - name: "feishu"
    enabled: true
    bot_api:
      app_id: "test_app_id"
      app_secret: "test_app_secret"
```

### 3. 依赖检查
```bash
# 检查 Python 版本
python --version  # 需要 3.10+

# 安装依赖
pip install -r requirements.txt

# 检查数据库连接（如果使用 PostgreSQL）
# psql -h localhost -U postgres -d ai_assistant
```

## 核心测试用例

### 测试 1: 飞书用户首次提问自动注册

**测试目标**: 验证飞书用户首次发送消息时自动创建账号

**前置条件**:
- 数据库已初始化（运行迁移脚本）
- 飞书 bot 配置正确

**测试步骤**:
1. 清空用户表（测试环境）
2. 模拟飞书用户发送消息（open_id: "ou_test_user_001"）
3. 检查数据库是否自动创建用户记录
4. 验证用户信息：
   - feishu_open_id 正确
   - 自动生成的 username（格式：feishu_ou_xxx）
   - 随机密码已生成
   - 角色为 "user"
   - 通知消息已发送

**预期结果**:
- 数据库中存在该用户
- 用户收到飞书通知（包含用户名和密码）

**测试文件**: `tests/integration/test_feishu_auto_register.py`

---

### 测试 2: 用户登录认证

**测试目标**: 验证用户名密码登录流程

**前置条件**:
- 数据库中存在测试用户

**测试步骤**:
1. 创建测试用户（username: "testuser", password: "test123"）
2. 调用认证接口，使用正确的用户名密码
3. 验证返回的用户信息
4. 调用认证接口，使用错误的密码
5. 验证返回 None

**预期结果**:
- 正确密码返回用户信息字典
- 错误密码返回 None

**测试文件**: `tests/unit/test_user_authentication.py`

---

### 测试 3: 用户修改密码

**测试目标**: 验证用户修改密码流程

**前置条件**:
- 数据库中存在测试用户

**测试步骤**:
1. 创建测试用户（username: "testuser", password: "old_pass"）
2. 调用修改密码接口，提供旧密码和新密码
3. 验证修改成功
4. 使用新密码登录
5. 验证登录成功

**预期结果**:
- 修改密码成功返回 True
- 旧密码无法登录
- 新密码可以登录

**测试文件**: `tests/unit/test_password_change.py`

---

### 测试 4: 管理员创建用户

**测试目标**: 验证管理员创建用户流程

**前置条件**:
- 数据库中存在管理员账号

**测试步骤**:
1. 初始化管理员账号（从 config.yaml 读取）
2. 调用创建用户接口
3. 验证用户创建成功
4. 验证用户信息正确

**预期结果**:
- 用户创建成功，返回 user_id
- 数据库中存在该用户

**测试文件**: `tests/unit/test_admin_create_user.py`

---

### 测试 5: 数据迁移脚本

**测试目标**: 验证数据库迁移脚本正确性

**前置条件**:
- 空数据库

**测试步骤**:
1. 运行迁移脚本创建表结构
2. 检查表是否创建成功（users 表）
3. 检查索引是否创建（username, feishu_open_id）
4. 创建测试数据
5. 验证约束（唯一性、非空）

**预期结果**:
- 表结构正确
- 索引创建成功
- 约束生效

**测试文件**: `tests/integration/test_database_migration.py`

---

### 测试 6: 飞书通知消息发送

**测试目标**: 验证飞书通知消息发送功能

**前置条件**:
- 飞书 bot 配置正确
- 有测试用户的 open_id

**测试步骤**:
1. 调用发送通知接口
2. 构造通知消息（包含用户名���密码）
3. 验证 API 调用成功（mock 飞书 API）
4. 验证消息格式正确

**预期结果**:
- API 调用成功（状态码 200）
- 消息内容包含必要信息（用户名、密码、登录链接）

**测试文件**: `tests/unit/test_feishu_notification.py`

---

## 测试执行顺序

1. **单元测试**（独立模块测试）
   - test_user_authentication.py
   - test_password_change.py
   - test_admin_create_user.py
   - test_feishu_notification.py

2. **集成测试**（端到端流程）
   - test_database_migration.py（先运行，准备数据库）
   - test_feishu_auto_register.py

## 测试数据清理

每个测试用例执行后需要清理测试数据：
- 删除测试用户
- 清空测试数据库（集成测试后）
- 使用 pytest fixture 自动清理

## 测试报告

测试完成后生成测试报告：
```bash
pytest tests/unit tests/integration -v --html=report.html --self-contained-html
```

## 风险和注意事项

1. **飞书 API 依赖**: 部分测试需要 mock 飞书 API，避免真实调用
2. **数据库隔离**: 测试环境和生产环境必须使用不同的数据库
3. **密码安全**: 测试用例中的密码仅用于测试，不应在生产环境使用
4. **并发安全**: 用户注册需要考虑并发场景（同一用户同时首次提问）

## 测试通过标准

所有核心测试用例通过，且满足：
- 飞书用户首次提问能自动注册
- 用户可以使用生成的密码登录 Web 界面
- 用户可以修改密码
- 管理员可以创建新用户
- 数据迁移脚本在空库和已有数据库上都能正确运行
- 飞书通知消息格式正确

## 后续工作

测试通过后：
1. 编写实施文档（`docs/implementation/account-system-implementation.md`）
2. 编写部署文档（`docs/deployment/account-system-deployment.md`）
3. 更新配置文件（`config.example.yaml`）
4. 更新 README.md（新增账号体系说明）
