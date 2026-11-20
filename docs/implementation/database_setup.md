# 数据库配置说明

## 安装依赖

```bash
pip install -r requirements.txt
```

新增的数据库相关依赖：
- `sqlalchemy>=2.0.0` - ORM 框架
- `bcrypt>=4.0.0` - 密码加密
- `psycopg2-binary>=2.9.0` - PostgreSQL 驱动
- `pymysql>=1.1.0` - MySQL 驱动

## 数据库配置

在 `config.yaml` 中添加数据库配置（可选，暂未集成到主程序）：

```yaml
# 数据库配置（可选，用于账号体系）
database:
  # 数据库类型: sqlite / mysql / postgresql
  type: "sqlite"
  
  # SQLite 配置
  sqlite:
    path: "./data/ai_assistant.db"
  
  # MySQL 配置（type 为 mysql 时使用）
  mysql:
    host: "localhost"
    port: 3306
    database: "ai_assistant"
    user: "root"
    password: "your_password"
  
  # PostgreSQL 配置（type 为 postgresql 时使用）
  postgresql:
    host: "localhost"
    port: 5432
    database: "ai_assistant"
    user: "postgres"
    password: "your_password"
  
  # 调试选项
  echo: false  # 是否打印 SQL 语句
```

## 数据迁移

将现有 JSONL 数据迁移到数据库：

```bash
# 运行数据迁移脚本
python scripts/migrate_data.py
```

迁移脚本会：
1. 创建数据库表（如果不存在）
2. 读取 `data/chat_history/*.jsonl` 和 `data/feedback/*.jsonl`
3. 将数据导入数据库
4. 支持增量迁移（避免重复导入）

## 数据库测试

验证数据库功能是否正常：

```bash
# 运行测试脚本
python scripts/test_database.py
```

测试内容：
- 数据库连接
- 表创建
- User 模型 CRUD（创建用户、密码验证）
- ChatHistory 模型 CRUD
- Feedback 模型 CRUD
- JSONL 记录转换

## 数据库表结构

### users 表
- `user_id` (主键): 用户唯一标识
- `username`: 用户名（登录用，唯一）
- `password_hash`: 密码哈希（bcrypt）
- `name`: 用户昵称/真实姓名
- `role`: 用户角色（user / admin）
- `feishu_open_id`: 飞书用户 open_id（关联飞书账号）
- `created_at`: 创建时间
- `updated_at`: 更新时间

索引：
- `idx_username` (username)
- `idx_feishu_open_id` (feishu_open_id)
- `idx_created_at` (created_at)

### chat_history 表
映射现有 JSONL 字段：
- `record_id` (主键): 记录唯一标识（UUID）
- `timestamp`: 对话时间
- `session_id`: 会话 ID（飞书 chat_id / 微信窗口名）
- `source`: 来源（feishu / wechat / web）
- `query`: 用户提问
- `answer`: AI 回复
- `latency_ms`: 响应耗时（毫秒）
- `user_id`: 用户标识（飞书 open_id 等）
- `user_name`: 用户昵称
- `context_key`: 引用链标识（飞书 root_id 或首条 message_id）
- `chain_info`: 引用链排查字段（JSON）
- `message_id`: AI 回复的消息 ID（飞书场景）
- `metadata`: 附加元数据（JSON，包含 mode / tool_rounds / doc_count 等）
- `created_at`: 记录创建时间

索引：
- `idx_session_source` (session_id, source)
- `idx_context_key` (context_key)
- `idx_message_id` (message_id)
- `idx_user_id` (user_id)
- `idx_timestamp` (timestamp)
- `idx_session_timestamp` (session_id, timestamp)

### feedback 表
映射现有 JSONL 字段：
- `feedback_id` (主键): 反馈记录唯一标识（UUID）
- `record_id`: 关联的 chat_history 记录主键
- `timestamp`: 反馈时间
- `session_id`: 会话 ID
- `source`: 反馈来源（feishu / wechat / web）
- `feedback_type`: 反馈类型（like / dislike）
- `feedback_text`: 用户填写的反馈内容（点踩时可选填）
- `user_id`: 反馈人 ID（飞书 open_id 等）
- `user_name`: 反馈人昵称
- `created_at`: 记录创建时间

索引：
- `idx_record_id` (record_id)
- `idx_session_source` (session_id, source)
- `idx_feedback_type` (feedback_type)
- `idx_timestamp` (timestamp)
- `idx_user_id` (user_id)
- `idx_session_timestamp` (session_id, timestamp)

## 使用示例

### 创建数据库管理器

```python
from ai_assistant.core.database import DatabaseManager

# SQLite（默认）
db_manager = DatabaseManager(
    db_type="sqlite",
    db_path="./data/ai_assistant.db"
)

# MySQL
db_manager = DatabaseManager(
    db_type="mysql",
    db_host="localhost",
    db_port=3306,
    db_name="ai_assistant",
    db_user="root",
    db_password="your_password"
)

# PostgreSQL
db_manager = DatabaseManager(
    db_type="postgresql",
    db_host="localhost",
    db_port=5432,
    db_name="ai_assistant",
    db_user="postgres",
    db_password="your_password"
)

# 创建表
db_manager.create_tables()
```

### 用户管理

```python
from ai_assistant.models import User
from ai_assistant.models.user import UserRole

# 创建用户
with db_manager.get_session() as session:
    user = User(
        user_id="user_001",
        username="testuser",
        name="测试用户",
        role=UserRole.USER,
        feishu_open_id="ou_abc123"
    )
    user.set_password("password123")
    session.add(user)

# 验证密码
with db_manager.get_session() as session:
    user = session.query(User).filter_by(username="testuser").first()
    if user and user.check_password("password123"):
        print("密码正确")
```

### 查询对话历史

```python
from ai_assistant.models import ChatHistoryDB

# 按 session_id 查询
with db_manager.get_session() as session:
    records = session.query(ChatHistoryDB)\
        .filter_by(session_id="chat_123")\
        .order_by(ChatHistoryDB.timestamp.desc())\
        .limit(10)\
        .all()

# 按 context_key 查询（引用链）
with db_manager.get_session() as session:
    records = session.query(ChatHistoryDB)\
        .filter_by(context_key="root_123")\
        .order_by(ChatHistoryDB.timestamp.asc())\
        .all()

# 按 message_id 查询（飞书反馈场景）
with db_manager.get_session() as session:
    record = session.query(ChatHistoryDB)\
        .filter_by(message_id="msg_123")\
        .first()
```

### 查询反馈记录

```python
from ai_assistant.models import FeedbackDB
from ai_assistant.models.feedback_db import FeedbackType

# 按 record_id 查询
with db_manager.get_session() as session:
    feedbacks = session.query(FeedbackDB)\
        .filter_by(record_id="record_123")\
        .all()

# 查询负反馈
with db_manager.get_session() as session:
    negative_feedbacks = session.query(FeedbackDB)\
        .filter_by(
            session_id="chat_123",
            feedback_type=FeedbackType.DISLIKE
        )\
        .order_by(FeedbackDB.timestamp.desc())\
        .limit(3)\
        .all()
```

## 注意事项

1. **密码安全**：User 模型使用 bcrypt 加密密码，自动加盐，安全性高
2. **事务管理**：使用 `db_manager.get_session()` 上下文管理器，自动提交/回滚
3. **连接池**：自动管理连接池，支持 `pool_pre_ping`（检测连接有效性）和 `pool_recycle`（定期回收连接）
4. **外键约束**：SQLite 默认启用外键约束
5. **索引优化**：所有常用查询字段都添加了索引，提升查询性能
6. **增量迁移**：迁移脚本支持增量导入，多次运行不会重复导入数据
7. **数据兼容**：完全兼容现有 JSONL 字段，支持无缝迁移

## 未来扩展

当前实现为账号体系的数据库层基础，后续可扩展：
1. Web 前端集成（用户登录、权限管理）
2. 用户级别的配置存储
3. 更细粒度的权限控制（群组权限、功能权限等）
4. 数据统计和分析接口
5. 数据备份和恢复功能
