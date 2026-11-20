# 账号体系使用指南

## 快速开始

### 1. 安装依赖

```bash
pip install sqlalchemy bcrypt pymysql
```

### 2. 配置数据库（可选）

编辑 `config.yaml`，添加数据库配置：

```yaml
# 数据库配置（默认使用 SQLite）
database:
  type: sqlite
  path: ./data/ai_assistant.db
  echo: false  # 调试时设为 true 查看 SQL

# 前端地址（用于账号通知）
frontend_url: http://localhost:3000
```

### 3. 启动系统

```bash
python src/ai_assistant/main.py
```

首次启动会自动创建数据库表。

### 4. 飞书用户自动注册

用户首次与飞书机器人对话时：
- 系统自动创建账号
- 发送飞书通知消息（包含登录名和密码）
- 用户可用该账号登录前端

## 核心功能

### 用户管理

```python
from ai_assistant.core.user_manager import UserManager
from ai_assistant.core.database import DatabaseManager

# 初始化
db = DatabaseManager(db_type='sqlite', db_path='./data/ai_assistant.db')
db.create_tables()
user_mgr = UserManager(db_manager=db)

# 创建用户
user_id = user_mgr.create_user(
    username='alice',
    password='password123',
    display_name='Alice Wang',
    role='user'
)

# 用户认证
result = user_mgr.authenticate('alice', 'password123')
if result:
    print(f"登录成功: {result}")

# 更新密码
user_mgr.update_password(user_id, 'password123', 'newpass456')

# 查询用户
user = user_mgr.get_user(user_id)
users = user_mgr.list_users(limit=10)
```

### 飞书通知

```python
from ai_assistant.utils.feishu_notifier import FeishuNotifier

# 初始化（需要飞书 Bot 配置）
notifier = FeishuNotifier({
    'app_id': 'your_app_id',
    'app_secret': 'your_app_secret',
    'base_url': 'https://open.feishu.cn'
})

# 发送账号通知
notifier.send_card_message(
    open_id='ou_xxx',
    username='alice',
    password='password123',
    frontend_url='http://localhost:3000'
)
```

## 运行测试

### 单元测试

```bash
cd D:\source\AI-Personal-Assistant
set PYTHONPATH=src
pytest tests/unit/test_user_manager.py -v
```

### 集成测试

```bash
python test_user_manager_integration.py
```

预期输出：
```
=== 所有测试通过! ===
```

## 配置项说明

### 数据库类型

**SQLite（默认）**
```yaml
database:
  type: sqlite
  path: ./data/ai_assistant.db
```

**MySQL**
```yaml
database:
  type: mysql
  host: localhost
  port: 3306
  name: ai_assistant
  user: root
  password: your_password
```

**PostgreSQL**
```yaml
database:
  type: postgresql
  host: localhost
  port: 5432
  name: ai_assistant
  user: postgres
  password: your_password
```

### 管理员账号

```yaml
admin:
  username: admin
  password: admin123
```

## 常见问题

### Q1: 如何重置用户密码？

```python
# 管理员操作（不需要旧密码）
user_mgr.reset_password(user_id, 'newpassword')
```

### Q2: 如何查看某个飞书用户的账号？

```python
user = user_mgr.get_user_by_feishu_open_id('ou_xxx')
if user:
    print(f"用户名: {user['username']}")
```

### Q3: 自动注册失败怎么办？

查看日志：
```
logs/ai-assistant.log
```

自动注册失败不影响问答功能，用户仍可正常使用 AI 问答。

### Q4: 如何手动创建管理员账号？

```python
admin_id = user_mgr.create_user(
    username='admin',
    password='admin123',
    display_name='Administrator',
    role='admin'
)
```

### Q5: 如何检查用户是否为管理员？

```python
is_admin = user_mgr.is_admin(user_id)
```

## 安全建议

1. **生产环境修改默认密码**
   - 不要使用 `admin123` 等弱密码
   - 使用强密码（至少 12 位，包含大小写字母、数字、特殊字符）

2. **数据库备份**
   ```bash
   # SQLite
   cp ./data/ai_assistant.db ./data/ai_assistant.db.backup
   
   # MySQL
   mysqldump -u root -p ai_assistant > backup.sql
   ```

3. **HTTPS 部署**
   - 前端使用 HTTPS
   - 飞书 Webhook 使用 HTTPS

4. **日志脱敏**
   - 系统已自动脱敏密码
   - 不要在日志中记录完整的用户密码

## 故障排查

### 数据库连接失败

检查配置：
```bash
# 测试 MySQL 连接
mysql -h localhost -u root -p

# 测试 PostgreSQL 连接
psql -h localhost -U postgres
```

### 飞书通知失败

检查：
1. app_id 和 app_secret 是否正确
2. 网络是否可访问 open.feishu.cn
3. 飞书应用权限是否开通

### 自动注册不生效

检查：
1. `config.yaml` 中数据库配置是否正确
2. `main.py` 中自动注册代码是否启用
3. 日志中是否有错误信息

## 更多信息

- 实施文档: `docs/implementation/account-system.md`
- 总结文档: `docs/summary/account-system-implementation.md`
- API 文档: 查看代码注释
- 测试用例: `tests/unit/test_user_manager.py`

## 联系支持

如有问题，请：
1. 查看日志文件 `logs/ai-assistant.log`
2. 运行测试确认功能正常
3. 查阅相关文档
