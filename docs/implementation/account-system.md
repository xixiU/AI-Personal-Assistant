# 账号体系实施文档

## 概述

本文档记录账号体系核心业务逻辑的实施细节，包括用户管理模块、飞书用户自动注册、飞书消息通知功能。

**实施时间**: 2024年（第一阶段）
**负责人**: Backend-Auth-Agent
**状态**: 接口定义完成，等待数据库模型

## 架构设计

### 核心模块

1. **UserManager** (`src/ai_assistant/core/user_manager.py`)
   - 用户 CRUD 操作
   - 密码管理（加密、验证、重置）
   - 飞书用户关联查询
   - 角色管理（user / admin）

2. **FeishuNotifier** (`src/ai_assistant/utils/feishu_notifier.py`)
   - 飞书私聊消息通知
   - 账号注册成功通知
   - 支持卡片消息和文本消息

3. **自动注册逻辑** (`src/ai_assistant/main.py`)
   - 在 `_process_event` 中集成
   - 首次使用时自动创建账号
   - 降级处理：注册失败不影响问答

## 技术实现

### 1. 用户管理接口

```python
class UserManager:
    def create_user(username, password, feishu_open_id=None, ...) -> str
    def authenticate(username, password) -> Dict
    def get_user(user_id) -> Dict
    def get_user_by_feishu_open_id(feishu_open_id) -> Dict
    def list_users(limit, offset, role=None) -> List[Dict]
    def update_password(user_id, old_password, new_password) -> bool
    def reset_password(user_id, new_password) -> bool
    def delete_user(user_id) -> bool
```

### 2. 密码策略

- **生成方式**: 随机 8 位字母+数字组合
- **加密算法**: SHA256
- **字符集**: `a-zA-Z0-9`（避免特殊字符，便于用户输入）

```python
# 生成随机密码
password = UserManager.generate_random_password(8)  # 例如: "W33gilas"

# 密码加密
password_hash = UserManager.hash_password(password)  # SHA256
```

### 3. 飞书用户自动注册流程

```
用户首次与机器人对话
    ↓
获取飞书用户信息 (open_id, user_name)
    ↓
检查用户是否已注册 (通过 feishu_open_id 查询)
    ↓
如果未注册：
    ├─ 生成用户名 (优先使用飞书昵称，回退到 feishu_{open_id})
    ├─ 生成随机密码 (8位)
    ├─ 创建用户记录 (关联 feishu_open_id)
    ├─ 发送飞书通知消息 (包含登录名、密码、前端地址)
    └─ 记录日志
    ↓
继续正常问答流程（注册失败不阻断）
```

### 4. 飞书消息通知

**卡片消息**（优先）:
```json
{
  "header": {
    "template": "green",
    "title": "账号注册成功"
  },
  "elements": [
    {
      "tag": "div",
      "text": "🎉 **您的账号已自动注册成功！**"
    },
    {
      "tag": "div",
      "fields": [
        {"text": "**📧 登录名**\nusername"},
        {"text": "**🔑 初始密码**\npassword"}
      ]
    },
    {
      "tag": "action",
      "actions": [
        {
          "tag": "button",
          "text": "🌐 前往登录",
          "url": "frontend_url"
        }
      ]
    }
  ]
}
```

**文本消息**（降级）:
```
🎉 您的账号已自动注册成功！

📧 登录名: username
🔑 初始密码: password

⚠️ 请妥善保管您的密码，建议首次登录后修改。

🌐 前端地址: frontend_url
```

## 数据库设计建议

### 用户表 (users)

| 字段 | 类型 | 约束 | 说明 |
|------|------|------|------|
| user_id | UUID | 主键 | 用户唯一标识 |
| username | VARCHAR(50) | 唯一索引 | 登录用户名 |
| password_hash | VARCHAR(64) | 非空 | SHA256 密码哈希 |
| feishu_open_id | VARCHAR(50) | 唯一索引，可空 | 飞书用户关联 |
| feishu_user_id | VARCHAR(50) | 可空 | 飞书 user_id |
| display_name | VARCHAR(100) | 可空 | 显示名称 |
| email | VARCHAR(100) | 可空 | 邮箱地址 |
| role | ENUM | 非空 | 角色: user/admin |
| created_at | TIMESTAMP | 非空 | 创建时间 |
| updated_at | TIMESTAMP | 非空 | 更新时间 |
| deleted_at | TIMESTAMP | 可空 | 软删除标记 |

### 索引设计

```sql
-- 主键索引
PRIMARY KEY (user_id)

-- 唯一索引
UNIQUE INDEX idx_username (username)
UNIQUE INDEX idx_feishu_open_id (feishu_open_id)

-- 查询索引
INDEX idx_role (role)
INDEX idx_created_at (created_at)
```

## 配置项

在 `config.yaml` 中新增 `admin` 段：

```yaml
# 管理员账号配置（从配置文件读取）
admin:
  username: "admin"
  password: "admin_password_hash"  # SHA256 哈希值
  email: "admin@example.com"

# 前端地址（用于账号通知消息）
frontend_url: "http://localhost:3000"
```

## 测试覆盖

### 单元测试 (`tests/unit/test_user_manager.py`)

- ✅ 密码生成（默认长度、自定义长度、唯一性）
- ✅ 密码加密（SHA256、幂等性、差异性）
- ✅ UserManager 初始化（无配置、带配置）

### 集成测试（等待数据库模型）

- [ ] 用户创建和查询
- [ ] 用户认证
- [ ] 密码更新和重置
- [ ] 飞书用户关联查询
- [ ] 软删除

## 实施步骤

### 第一阶段：接口定义 ✅

1. ✅ 创建 UserManager 类（接口定义）
2. ✅ 实现密码生成和加密工具方法
3. ✅ 创建 FeishuNotifier 类
4. ✅ 在 main.py 中预留自动注册逻辑
5. ✅ 编写单元测试

### 第二阶段：数据库对接（等待 Backend-DB-Agent）

1. [ ] 完成数据库模型设计
2. [ ] 实现 UserManager 数据库操作
3. [ ] 实现管理员账号初始化
4. [ ] 启用自动注册逻辑（取消 main.py 中的注释）
5. [ ] 编写集成测试

### 第三阶段：功能验证

1. [ ] 测试飞书用户首次对话自动注册
2. [ ] 测试飞书通知消息发送
3. [ ] 测试管理员账号功能
4. [ ] 压力测试（并发注册）

## 关键决策记录

### 1. 为什么使用 SHA256 而不是 bcrypt？

- 项目现有代码已使用 SHA256（见 `operations_manager.py`）
- 保持一致性，减少依赖
- 对于内部系统足够安全
- 如需更高安全性，后续可升级为 bcrypt

### 2. 为什么自动注册失败不阻断问答？

- 核心功能是 AI 问答，账号体系是辅助功能
- 降级处理保证系统稳定性
- 用户可以继续使用 AI 问答，后续手动注册

### 3. 为什么密码不包含特殊字符？

- 随机生成的特殊字符密码用户输入不便（尤其是手机端）
- 8 位字母+数字组合已有 62^8 ≈ 2.18×10^14 种可能，足够安全
- 用户可以首次登录后修改为包含特殊字符的密码

## 后续优化方向

1. **密码强度提升**: 支持 bcrypt/PBKDF2
2. **会话管理**: JWT token 认证
3. **权限细化**: 基于角色的访问控制 (RBAC)
4. **审计日志**: 记录用户操作历史
5. **批量注册**: 支持 Excel 导入批量创建用户

## 参考资料

- 现有模块: `FeedbackManager`, `ChatHistoryManager`
- 飞书 API 文档: https://open.feishu.cn/document/
- Python hashlib: https://docs.python.org/3/library/hashlib.html
