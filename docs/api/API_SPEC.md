# AI 助手 RESTful API 规范

## 概述

本文档描述 AI 助手后端管理系统的 RESTful API 接口规范。

**基础信息**
- Base URL: `http://your-domain:8080`
- 认证方式: JWT Bearer Token
- 数据格式: JSON
- 字符编码: UTF-8

## 通用规范

### 请求头

```
Content-Type: application/json
Authorization: Bearer <token>  # 需要认证的接口
```

### 统一响应格式

#### 成功响应

```json
{
  "success": true,
  "message": "Success",
  "data": {
    // 响应数据
  }
}
```

#### 错误响应

```json
{
  "success": false,
  "message": "错误描述",
  "code": "ERROR_CODE",
  "details": {
    // 详细错误信息（可选）
  }
}
```

### HTTP 状态码

- `200 OK`: 请求成功
- `201 Created`: 资源创建成功
- `400 Bad Request`: 请求参数错误
- `401 Unauthorized`: 未认证或 Token 无效
- `403 Forbidden`: 无权限访问
- `404 Not Found`: 资源不存在
- `500 Internal Server Error`: 服务器内部错误
- `503 Service Unavailable`: 服务不可用

### 错误码列表

| 错误码 | 说明 |
|--------|------|
| `INVALID_REQUEST` | 请求格式错误 |
| `MISSING_FIELDS` | 缺少必填字段 |
| `INVALID_PARAMETER` | 参数值无效 |
| `MISSING_TOKEN` | 缺少认证 Token |
| `INVALID_TOKEN` | Token 无效 |
| `TOKEN_EXPIRED` | Token 已过期 |
| `AUTH_FAILED` | 认证失败（用户名或密码错误） |
| `PERMISSION_DENIED` | 权限不足 |
| `SERVICE_UNAVAILABLE` | 服务未初始化 |
| `INTERNAL_ERROR` | 服务器内部错误 |
| `NOT_IMPLEMENTED` | 功能尚未实现 |

### 分页参数

支持分页的接口使用以下参数：

- `page`: 页码（从 1 开始，默认 1）
- `page_size`: 每页数量（默认 20，最大 100）

分页响应格式：

```json
{
  "success": true,
  "message": "Success",
  "data": {
    "records": [...],
    "total": 100,
    "page": 1,
    "page_size": 20
  }
}
```

---

## 认证接口

### 1. 用户登录

**接口**: `POST /api/auth/login`

**说明**: 用户登录获取 JWT Token

**请求体**:
```json
{
  "username": "admin",
  "password": "password123"
}
```

**响应**:
```json
{
  "success": true,
  "message": "登录成功",
  "data": {
    "token": "eyJ0eXAiOiJKV1QiLCJhbGc...",
    "user": {
      "user_id": "550e8400-e29b-41d4-a716-446655440000",
      "username": "admin",
      "display_name": "管理员",
      "role": "admin"
    }
  }
}
```

**错误响应**:
- `400`: 请求参数错误
- `401`: 用户名或密码错误

### 2. 用户登出

**接口**: `POST /api/auth/logout`

**说明**: 用户登出（客户端需删除 Token）

**请求头**:
```
Authorization: Bearer <token>
```

**响应**:
```json
{
  "success": true,
  "message": "登出成功"
}
```

### 3. 修改密码

**接口**: `POST /api/auth/change-password`

**说明**: 用户修改自己的密码

**请求头**:
```
Authorization: Bearer <token>
```

**请求体**:
```json
{
  "old_password": "oldpass123",
  "new_password": "newpass456"
}
```

**响应**:
```json
{
  "success": true,
  "message": "密码修改成功"
}
```

**错误响应**:
- `400`: 参数错误（密码长度不足、新旧密码相同等）
- `401`: 旧密码错误或 Token 无效

---

## 管理员接口

所有管理员接口需要 **管理员权限**（`role: "admin"`）

### 4. 查询用户列表

**接口**: `GET /api/admin/users`

**说明**: 查询用户列表（支持分页和过滤）

**请求头**:
```
Authorization: Bearer <token>
```

**查询参数**:
- `page`: 页码（默认 1）
- `page_size`: 每页数量（默认 20，最大 100）
- `role`: 角色过滤（可选，`admin`/`user`）

**响应**:
```json
{
  "success": true,
  "message": "Success",
  "data": {
    "users": [
      {
        "user_id": "550e8400-e29b-41d4-a716-446655440000",
        "username": "admin",
        "display_name": "管理员",
        "email": "admin@example.com",
        "role": "admin",
        "created_at": "2024-01-01T00:00:00Z"
      }
    ],
    "total": 100,
    "page": 1,
    "page_size": 20
  }
}
```

### 5. 创建用户

**接口**: `POST /api/admin/users`

**说明**: 创建新用户

**请求头**:
```
Authorization: Bearer <token>
```

**请求体**:
```json
{
  "username": "newuser",
  "password": "password123",
  "display_name": "新用户",
  "email": "user@example.com",
  "role": "user"
}
```

**字段说明**:
- `username`: 用户名（必填，3-32 字符，唯一）
- `password`: 密码（可选，不填则自动生成，最少 6 位）
- `display_name`: 显示名称（可选，默认同 username）
- `email`: 邮箱（可选）
- `role`: 角色（可选，默认 `user`，可选 `admin`/`user`）

**响应**:
```json
{
  "success": true,
  "message": "用户创建成功",
  "data": {
    "user_id": "550e8400-e29b-41d4-a716-446655440001",
    "username": "newuser",
    "display_name": "新用户",
    "role": "user",
    "password": "abc12345"
  }
}
```

**说明**: 
- 如果未提供密码，系统会自动生成并在响应中返回（仅此一次）
- HTTP 状态码: `201 Created`

### 6. 更新用户信息

**接口**: `PUT /api/admin/users/{user_id}`

**说明**: 更新用户信息（开发中）

**状态**: `501 Not Implemented`（等待数据库实现）

### 7. 删除用户

**接口**: `DELETE /api/admin/users/{user_id}`

**说明**: 删除用户（软删除）

**请求头**:
```
Authorization: Bearer <token>
```

**响应**:
```json
{
  "success": true,
  "message": "用户删除成功"
}
```

**错误响应**:
- `400`: 不能删除自己
- `404`: 用户不存在

### 8. 重置用户密码

**接口**: `POST /api/admin/users/{user_id}/reset-password`

**说明**: 管理员重置用户密码（不需要旧密码）

**请求头**:
```
Authorization: Bearer <token>
```

**请求体**:
```json
{
  "new_password": "newpass123"
}
```

**字段说明**:
- `new_password`: 新密码（可选，不填则自动生成）

**响应**:
```json
{
  "success": true,
  "message": "密码重置成功",
  "data": {
    "password": "abc12345"
  }
}
```

**说明**: 如果未提供新密码，系统会自动生成并在响应中返回

---

## 数据查询接口

### 9. 查询问答历史

**接口**: `GET /api/chat-history`

**说明**: 查询问答历史记录（支持分页和过滤）

**请求头**:
```
Authorization: Bearer <token>
```

**查询参数**:
- `page`: 页码（默认 1）
- `page_size`: 每页数量（默认 20，最大 100）
- `user_id`: 用户 ID 过滤（可选，管理员可查所有用户）
- `session_id`: 会话 ID 过滤（可选）
- `start_time`: 开始时间（可选，ISO 8601 格式，如 `2024-01-01T00:00:00Z`）
- `end_time`: 结束时间（可选，ISO 8601 格式）
- `source`: 来源过滤（可选，`feishu`/`web`）

**权限说明**:
- 普通用户只能查询自己的数据
- 管理员可以查询所有用户的数据

**响应**:
```json
{
  "success": true,
  "message": "Success",
  "data": {
    "records": [
      {
        "record_id": "550e8400-e29b-41d4-a716-446655440002",
        "session_id": "session-123",
        "user_id": "user-456",
        "user_name": "张三",
        "query": "用户问题",
        "response": "AI 回复内容",
        "mode": "rag",
        "source": "feishu",
        "created_at": "2024-01-01T12:00:00Z",
        "has_feedback": true
      }
    ],
    "total": 100,
    "page": 1,
    "page_size": 20
  }
}
```

**状态**: 接口已实现，等待数据库查询方法

### 10. 查询反馈数据

**接口**: `GET /api/feedback`

**说明**: 查询用户反馈数据（支持分页和过滤）

**请求头**:
```
Authorization: Bearer <token>
```

**查询参数**:
- `page`: 页码（默认 1）
- `page_size`: 每页数量（默认 20，最大 100）
- `user_id`: 用户 ID 过滤（可选，管理员可查所有用户）
- `session_id`: 会话 ID 过滤（可选）
- `feedback_type`: 反馈类型过滤（可选，`like`/`dislike`）
- `start_time`: 开始时间（可选，ISO 8601 格式）
- `end_time`: 结束时间（可选，ISO 8601 格式）
- `source`: 来源过滤（可选，`feishu`/`web`）

**权限说明**:
- 普通用户只能查询自己的反馈
- 管理员可以查询所有反馈

**响应**:
```json
{
  "success": true,
  "message": "Success",
  "data": {
    "feedbacks": [
      {
        "feedback_id": "550e8400-e29b-41d4-a716-446655440003",
        "record_id": "chat-record-uuid",
        "session_id": "session-123",
        "user_id": "user-456",
        "user_name": "张三",
        "feedback_type": "like",
        "feedback_text": "回答很有帮助",
        "source": "feishu",
        "created_at": "2024-01-01T12:00:00Z",
        "query": "原问题",
        "response": "AI 回复"
      }
    ],
    "total": 50,
    "page": 1,
    "page_size": 20,
    "stats": {
      "like_count": 30,
      "dislike_count": 20,
      "like_rate": 0.6
    }
  }
}
```

**状态**: 接口已实现，等待数据库查询方法

---

## JWT Token 说明

### Token 结构

JWT Token 包含以下 Payload：

```json
{
  "user_id": "550e8400-e29b-41d4-a716-446655440000",
  "username": "admin",
  "role": "admin",
  "iat": 1704067200,
  "exp": 1704153600
}
```

### Token 使用

1. 登录成功后，客户端保存返回的 Token
2. 后续请求在 `Authorization` 请求头中携带 Token：
   ```
   Authorization: Bearer <token>
   ```
3. Token 默认有效期 24 小时
4. Token 过期后需要重新登录

### Token 刷新

当前版本不支持 Token 刷新，过期后需要重新登录。

---

## 配置说明

API 相关配置在 `config.yaml` 中添加（待实现）：

```yaml
api:
  jwt_secret: "your-secret-key-change-in-production"  # JWT 密钥（生产环境必须修改）
  jwt_expires_in: 86400  # Token 过期时间（秒，默认 24 小时）
  
  # 管理员账号（首次启动时自动创建）
  admin:
    username: "admin"
    password: "admin123"  # 首次登录后请立即修改
    display_name: "系统管理员"
```

---

## 安全建议

1. **生产环境必须修改 JWT 密钥**：使用强随机字符串
2. **使用 HTTPS**：避免 Token 在传输中被窃取
3. **首次登录后立即修改管理员密码**
4. **Token 不要存储在 localStorage**：容易受 XSS 攻击，建议使用 HttpOnly Cookie
5. **设置合理的 Token 过期时间**：根据业务需求平衡安全性和用户体验

---

## 待实现功能

1. **数据库集成**：等待 Backend-DB-Agent 完成数据库模型
2. **用户更新接口**：`PUT /api/admin/users/{user_id}`
3. **问答历史查询**：数据库查询方法
4. **反馈数据查询**：数据库查询方法和统计
5. **Token 刷新机制**：避免频繁重新登录
6. **API 访问日志**：记录 API 调用历史
7. **速率限制**：防止 API 滥用

---

## 示例代码

### JavaScript (Fetch API)

```javascript
// 登录
const loginResponse = await fetch('http://localhost:8080/api/auth/login', {
  method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify({
    username: 'admin',
    password: 'admin123'
  })
});
const loginData = await loginResponse.json();
const token = loginData.data.token;

// 查询用户列表（需要管理员权限）
const usersResponse = await fetch('http://localhost:8080/api/admin/users?page=1&page_size=20', {
  method: 'GET',
  headers: { 
    'Authorization': `Bearer ${token}`
  }
});
const usersData = await usersResponse.json();

// 查询问答历史
const historyResponse = await fetch('http://localhost:8080/api/chat-history?page=1&page_size=20', {
  method: 'GET',
  headers: {
    'Authorization': `Bearer ${token}`
  }
});
const historyData = await historyResponse.json();
```

### Python (Requests)

```python
import requests

# 登录
login_response = requests.post(
    'http://localhost:8080/api/auth/login',
    json={
        'username': 'admin',
        'password': 'admin123'
    }
)
token = login_response.json()['data']['token']

# 查询用户列表
users_response = requests.get(
    'http://localhost:8080/api/admin/users',
    headers={'Authorization': f'Bearer {token}'},
    params={'page': 1, 'page_size': 20}
)
users_data = users_response.json()

# 创建用户
create_response = requests.post(
    'http://localhost:8080/api/admin/users',
    headers={'Authorization': f'Bearer {token}'},
    json={
        'username': 'newuser',
        'password': 'password123',
        'role': 'user'
    }
)
```

---

## 更新日志

### v1.0.0 (2024-01-01)

- 初始版本
- 实现认证接口（登录、登出、修改密码）
- 实现管理员接口（用户管理 CRUD）
- 实现数据查询接口（问答历史、反馈数据）
- 统一响应格式和错误处理
- JWT Token 认证机制
