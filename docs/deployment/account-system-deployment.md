# 账号体系部署文档

## 概述

本文档说明如何部署和配置 AI 助手的账号体系，包括数据库初始化、管理员账号创建、飞书用户自动注册功能。

**部署时间**: 预计 15-30 分钟  
**前置条件**: 
- Python 3.10+
- 已安装项目依赖（`pip install -r requirements.txt`）
- 飞书机器人已配置（用于自动注册通知）

## 架构说明

账号体系包含以下组件：

1. **数据库层**：存储用户账号、密码（bcrypt 加密）、飞书关联信息
2. **用户管理器**：提供用户 CRUD、认证、密码管理 API
3. **Web 认证接口**：登录、修改密码、用户管理 REST API（基于 JWT）
4. **飞书自动注册**：用户首次与机器人对话时自动创建账号并通知
5. **前端页面**：登录页、用户信息页、管理后台

## 部署步骤

### 1. 安装依赖

确保安装了数据库相关依赖：

```bash
pip install -r requirements.txt
```

新增依赖包括：
- `sqlalchemy>=2.0.0` - ORM 框架
- `bcrypt>=4.0.0` - 密码加密
- `pyjwt>=2.8.0` - JWT 认证
- `psycopg2-binary>=2.9.0` - PostgreSQL 驱动（可选）
- `pymysql>=1.1.0` - MySQL 驱动（可选）

### 2. 配置数据库

编辑 `config.yaml`，添加数据库配置：

#### SQLite（推荐用于开发和小规模部署）

```yaml
database:
  type: "sqlite"
  sqlite:
    path: "./data/ai_assistant.db"
  echo: false
```

优点：
- 无需额外安装数据库软件
- 配置简单，开箱即用
- 适合单机部署

缺点：
- 不支持多进程并发写入
- 数据库文件需要定期备份

#### PostgreSQL（推荐用于生产环境）

```yaml
database:
  type: "postgresql"
  postgresql:
    host: "localhost"
    port: 5432
    database: "ai_assistant"
    user: "postgres"
    password: "your_password"
  echo: false
```

优点：
- 高性能，支持高并发
- 数据一致性保证
- 支持备份和主从复制

部署 PostgreSQL：

```bash
# Docker 部署（推荐）
docker run -d \
  --name postgres-ai-assistant \
  -e POSTGRES_PASSWORD=your_password \
  -e POSTGRES_DB=ai_assistant \
  -p 5432:5432 \
  -v ./data/postgres:/var/lib/postgresql/data \
  postgres:15

# 验证连接
psql -h localhost -U postgres -d ai_assistant
```

#### MySQL（可选）

```yaml
database:
  type: "mysql"
  mysql:
    host: "localhost"
    port: 3306
    database: "ai_assistant"
    user: "root"
    password: "your_password"
  echo: false
```

### 3. 配置管理员账号

在 `config.yaml` 中配置管理员初始账号：

```yaml
admin:
  username: "admin"
  password: "change_me_123"  # 请务必修改为强密码
```

**安全建议**：
- 使用强密码（至少 12 位，包含大小写字母、数字、特殊字符）
- 生产环境部署后立即通过 Web 界面修改密码
- 不要在配置文件中保存明文密码（首次启动后可删除此配置）

### 4. 配置前端地址

```yaml
frontend_url: "http://localhost:8080"  # 开发环境
# frontend_url: "https://ai-assistant.example.com"  # 生产环境
```

此地址用于：
- 飞书通知消息中的登录链接
- 邮件通知中的链接（未来功能）

### 5. 配置 JWT 密钥

```yaml
jwt:
  secret: "change_this_secret_key_in_production"  # 生产环境请使用随机生成的长密钥
  expires_in: 86400  # Token 过期时间（秒），24 小时
```

生成安全的 JWT 密钥：

```python
import secrets
print(secrets.token_urlsafe(64))
# 输出示例: "q3K8vN2Lm9Pz..."
```

### 6. 初始化数据库

运行数据库初始化脚本，创建表结构：

```bash
# 方式一：通过测试脚本初始化（推荐）
python scripts/test_database.py

# 方式二：启动主程序（会自动创建表）
python run.py
```

初始化会创建以下数据表：
- `users` - 用户账号表
- `chat_history` - 对话历史表（可选，用于迁移现有 JSONL 数据）
- `feedback` - 用户反馈表（可选）

验证表创建成功：

```bash
# SQLite
sqlite3 ./data/ai_assistant.db ".tables"

# PostgreSQL
psql -h localhost -U postgres -d ai_assistant -c "\dt"
```

### 7. 创建管理员账号

首次启动时，系统会自动检查并创建管理员账号（如果不存在）。

手动创建管理员（可选）：

```python
# 进入 Python 交互环境
import sys
sys.path.insert(0, 'src')

from ai_assistant.core.database import DatabaseManager
from ai_assistant.core.user_manager import UserManager

# 创建数据库管理器
db = DatabaseManager(db_type='sqlite', db_path='./data/ai_assistant.db')

# 创建用户管理器
user_mgr = UserManager(db_manager=db)

# 创建管理员
admin_id = user_mgr.create_user(
    username='admin',
    password='your_strong_password',
    display_name='系统管理员',
    role='admin'
)

print(f"管理员创建成功: {admin_id}")
```

### 8. 配置飞书通知（用于自动注册）

确保飞书机器人配置正确（在 `config.yaml` 的 `adapters` 段）：

```yaml
adapters:
  - name: "feishu"
    enabled: true
    bot_api:
      base_url: "https://open.feishu.cn"
      app_id: "cli_xxx"
      app_secret: "xxx"
```

飞书自动注册通知需要机器人有以下权限：
- **im:message** - 发送私聊消息
- **contact:user.base** - 获取用户基本信息（昵称）

### 9. 启动服务

```bash
python run.py
```

启动日志应显示：

```
[INFO] 数据库管理器初始化完成: sqlite
[INFO] 用户管理器初始化完成
[INFO] 认证模块初始化完成，Token 过期时间: 24.0 小时
[INFO] Webhook 服务器启动: 0.0.0.0:8080
```

### 10. 验证部署

#### 10.1 验证 Web 登录

访问 `http://localhost:8080/login.html`，使用管理员账号登录：
- 用户名：`admin`
- 密码：`config.yaml` 中配置的密码

登录成功后会跳转到用户信息页面。

#### 10.2 验证飞书自动注册

1. 使用一个从未与机器人对话的飞书账号
2. 向机器人发送消息（如 "@AI助手 你好"）
3. 检查是否收到账号注册通知（包含用户名和密码）
4. 使用通知中的用户名和密码登录 Web 界面

#### 10.3 验证 API 接口

```bash
# 测试登录接口
curl -X POST http://localhost:8080/api/auth/login \
  -H "Content-Type: application/json" \
  -d '{"username": "admin", "password": "your_password"}'

# 预期响应
{
  "success": true,
  "message": "登录成功",
  "data": {
    "token": "eyJ0eXAiOiJKV1QiLCJhbGc...",
    "user": {
      "user_id": "...",
      "username": "admin",
      "role": "admin"
    }
  }
}
```

## 数据迁移（可选）

如果你有现有的 JSONL 格式对话历史和反馈数据，可以迁移到数据库：

```bash
python scripts/migrate_data.py
```

迁移脚本会：
1. 扫描 `data/chat_history/*.jsonl` 和 `data/feedback/*.jsonl`
2. 将数据导入数据库（增量迁移，不会重复导入）
3. 保留原始 JSONL 文件（不会删除）

迁移完成后，系统会同时支持 JSONL 文件和数据库两种存储方式。

## 安全加固

### 生产环境检查清单

- [ ] 修改管理员默认密码
- [ ] 使用强随机 JWT 密钥
- [ ] 启用 HTTPS（配置反向代理）
- [ ] 限制数据库访问（防火墙规则）
- [ ] 定期备份数据库
- [ ] 配置日志审计
- [ ] 启用飞书 Webhook 加密（encrypt_key）

### 备份策略

#### SQLite 备份

```bash
# 每日备份脚本
#!/bin/bash
DATE=$(date +%Y%m%d)
sqlite3 ./data/ai_assistant.db ".backup ./data/backups/ai_assistant_$DATE.db"
find ./data/backups -name "*.db" -mtime +7 -delete  # 保留 7 天
```

#### PostgreSQL 备份

```bash
# 每日备份脚本
#!/bin/bash
DATE=$(date +%Y%m%d)
pg_dump -h localhost -U postgres ai_assistant > ./data/backups/ai_assistant_$DATE.sql
find ./data/backups -name "*.sql" -mtime +7 -delete
```

## 常见问题

### Q1: 启动时提示 "No module named 'bcrypt'"

**解决方案**：
```bash
pip install bcrypt
```

### Q2: PostgreSQL 连接失败

**检查项**：
1. PostgreSQL 服务是否运行：`systemctl status postgresql`
2. 防火墙是否开放端口：`telnet localhost 5432`
3. 用户名密码是否正确
4. 数据库是否存在：`psql -l`

### Q3: 管理员登录失败

**可能原因**：
1. 密码错误：检查 `config.yaml` 中的密码
2. 管理员未创建：查看启动日志，手动创建（见步骤 7）
3. JWT 配置错误：检查 `jwt.secret` 配置

### Q4: 飞书用户没有收到注册通知

**检查项**：
1. 飞书机器人配置是否正确（app_id、app_secret）
2. 机器人是否有发送私聊消息权限
3. 查看日志中的错误信息
4. 使用 `curl` 测试飞书 API：

```bash
# 获取 access_token
curl -X POST "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal" \
  -H "Content-Type: application/json" \
  -d '{"app_id": "xxx", "app_secret": "xxx"}'
```

### Q5: 数据库迁移失败

**常见错误**：
- JSONL 文件格式错误：检查是否有非法 JSON 行
- 数据库连接超时：检查网络和数据库状态
- 权限不足：确保数据库用户有写入权限

**解决方案**：
```bash
# 查看迁移日志
python scripts/migrate_data.py 2>&1 | tee migration.log

# 重置数据库（慎用，会删除所有数据）
python scripts/test_database.py --reset
```

## 监控和维护

### 日志位置

- 应用日志：`logs/ai-assistant.log`
- 数据库日志：`echo: true` 开启 SQL 日志

### 性能监控

```bash
# SQLite 数据库大小
ls -lh ./data/ai_assistant.db

# PostgreSQL 数据库大小
psql -h localhost -U postgres -d ai_assistant -c "SELECT pg_size_pretty(pg_database_size('ai_assistant'));"

# 用户数量
sqlite3 ./data/ai_assistant.db "SELECT COUNT(*) FROM users;"
```

### 定期维护

- **每周**：检查日志错误、备份数据库
- **每月**：清理过期数据、更新依赖版本
- **每季度**：安全审计、密码策略检查

## 回滚方案

如果部署出现问题，可以回滚到 JSONL 文件模式：

1. 停止服务
2. 在 `config.yaml` 中注释掉 `database` 段
3. 重启服务（系统会回退到 JSONL 文件存储）
4. 数据库数据不会丢失，修复问题后可以重新启用

## 联系支持

遇到问题可以：
- 查看项目文档：`docs/` 目录
- 提交 Issue
- 查看日志文件定位错误

---

**部署检查清单**：

- [ ] 依赖安装完成
- [ ] 数据库配置正确
- [ ] 数据库表创建成功
- [ ] 管理员账号创建成功
- [ ] JWT 配置完成
- [ ] 飞书机器人配置正确
- [ ] Web 登录测试通过
- [ ] 飞书自动注册测试通过
- [ ] 备份脚本配置完成
- [ ] 安全加固检查完成
