# API 接口集成指南

本文档说明如何将 API 接口集成到主程序中。

## 快速开始

### 1. 安装依赖

```bash
pip install pyjwt>=2.8.0
```

### 2. 配置文件

在 `config.yaml` 中添加 API 配置：

```yaml
api:
  enabled: true  # 是否启用 API 接口
  jwt_secret: "your-secret-key-change-in-production"  # JWT 密钥（生产环境必须修改）
  jwt_expires_in: 86400  # Token 过期时间（秒，默认 24 小时）
  
  # 管理员账号（首次启动时自动创建）
  admin:
    username: "admin"
    password: "admin123"  # 首次登录后请立即修改
    display_name: "系统管理员"
```

### 3. 主程序集成

在 `main.py` 或 `run.py` 中添加以下代码：

```python
from ai_assistant.webhook_server import WebhookServer
from ai_assistant.core.user_manager import UserManager

# 读取配置
config = load_config()
api_config = config.get('api', {})

# 初始化 WebhookServer
webhook_server = WebhookServer(
    host=config['system']['host'],
    port=config['system']['webhook_port'],
    web_frontend_enabled=config['system']['web_frontend_enabled']
)

# 如果启用了 API
if api_config.get('enabled', True):
    # 初始化 UserManager 和 ChatHistoryDB（等待 Backend-DB-Agent 完成）
    user_manager = UserManager(db_connection)
    chat_history_db = ChatHistoryDB(db_connection)
    
    # 注入到 WebhookServer
    webhook_server.set_api_components(
        user_manager=user_manager,
        chat_history_db=chat_history_db,
        jwt_secret=api_config['jwt_secret'],
        jwt_expires_in=api_config.get('jwt_expires_in', 86400)
    )
    
    # 创建管理员账号（如果不存在）
    admin_config = api_config.get('admin', {})
    if admin_config:
        user_manager.create_admin_if_not_exists(
            username=admin_config['username'],
            password=admin_config['password'],
            display_name=admin_config.get('display_name', '系统管理员')
        )

# 启动服务器
webhook_server.run_production()
```

## API 路由

所有 API 路由已自动注册到 Flask 应用：

- `/api/auth/*` - 认证接口
- `/api/admin/*` - 管理员接口
- `/api/chat-history` - 问答历史查询
- `/api/feedback` - 反馈数据查询

## 测试 API

### 使用 curl

```bash
# 登录
curl -X POST http://localhost:8080/api/auth/login \
  -H "Content-Type: application/json" \
  -d '{"username":"admin","password":"admin123"}'

# 查询用户列表（需要 Token）
curl -X GET "http://localhost:8080/api/admin/users?page=1&page_size=20" \
  -H "Authorization: Bearer <token>"
```

### 使用 Postman

1. 导入 API 文档（`docs/api/API_SPEC.md`）
2. 配置环境变量：
   - `base_url`: `http://localhost:8080`
   - `token`: 登录后获取的 Token
3. 测试各个接口

## 注意事项

### 1. JWT 密钥

**生产环境必须修改 JWT 密钥**，使用强随机字符串：

```bash
# 生成随机密钥
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

### 2. 管理员密码

首次登录后立即修改管理员密码：

```bash
curl -X POST http://localhost:8080/api/auth/change-password \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer <token>" \
  -d '{"old_password":"admin123","new_password":"new-strong-password"}'
```

### 3. HTTPS

生产环境建议使用 HTTPS，避免 Token 在传输中被窃取。

可以使用 Nginx 作为反向代理：

```nginx
server {
    listen 443 ssl;
    server_name your-domain.com;
    
    ssl_certificate /path/to/cert.pem;
    ssl_certificate_key /path/to/key.pem;
    
    location / {
        proxy_pass http://localhost:8080;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
    }
}
```

## 依赖状态

### 当前可用功能
- ✅ 用户登录/登出
- ✅ 修改密码
- ✅ 创建用户
- ✅ 删除用户
- ✅ 重置密码

### 等待实现功能（依赖数据库）
- ⏳ 更新用户信息
- ⏳ 问答历史查询
- ⏳ 反馈数据查询

## 故障排查

### API 返回 503 Service Unavailable

**原因**: 未调用 `set_api_components()` 注入依赖

**解决**: 确保在启动服务器前注入 UserManager 和 ChatHistoryDB

### Token 验证失败

**原因**: JWT 密钥不一致

**解决**: 检查配置文件中的 `jwt_secret` 是否正确

### 403 Permission Denied

**原因**: 普通用户访问管理员接口

**解决**: 使用管理员账号登录获取 Token

## 更多信息

详细的 API 接口文档请参考：`docs/api/API_SPEC.md`
