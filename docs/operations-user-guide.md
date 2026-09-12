# 运维操作功能使用指南

## 快速开始

### 1. 配置文件

将 `config.operations.example.yaml` 的内容复制到你的 `config.yaml` 中，并根据实际情况修改：

```yaml
operations:
  authorization:
    operators:
      feishu:
        - open_id: "your_open_id_here"
          name: "张三"
    
    approvers:
      feishu:
        - open_id: "admin_open_id"
          name: "运维主管"
    
    approval_timeout: 180

  machines:
    - name: "生产服务器1"
      alias: ["生产1号", "prod1"]
      host: "192.168.1.10"
      description: "主要部署订单服务"
      ssh:
        method: "key"
        user: "deploy"
        key_path: "/path/to/private_key"
        port: 22
      
      applications:
        - name: "订单服务"
          alias: ["订单", "order"]
          path: "/opt/apps/order-service"
          scripts:
            status: "./status.sh"
            restart: "./restart.sh"
          logs:
            app: "logs/application.log"
```

### 2. 生成加密密钥（如使用密码认证）

```bash
# 生成密钥
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

# 设置环境变量
export OPERATIONS_ENCRYPT_KEY="your_key_here"

# 加密密码
python -c "from cryptography.fernet import Fernet; import sys; cipher=Fernet(sys.argv[1].encode()); print(cipher.encrypt(sys.argv[2].encode()).decode())" "$OPERATIONS_ENCRYPT_KEY" "your_password"
```

### 3. 初始化（在 main.py 中）

```python
# 在 AIAssistant.__init__ 中添加
if getattr(self.config, 'operations_enabled', False):
    from ai_assistant.core.operations_manager import OperationsManager
    from ai_assistant.tools.operations_tools import OperationsTools
    
    # 加载配置并初始化
    self.ops_manager = OperationsManager(config=self.config.operations)
    self.ops_tools = OperationsTools(operations_manager=self.ops_manager)
    
    # 注入到 AI Provider
    self.ai_provider.set_operations_tools(self.ops_tools, enabled=True)
```

---

## 使用方式

### 飞书场景

**触发运维模式**：
- 发送 `/运维 <你的指令>`
- 或 `/ops <你的指令>`

**示例**：

```
/运维 查看生产服务器1的订单服务状态
/运维 查下测试服的内存使用情况
/运维 重启生产1号的订单服务
/运维 看下用户服务的最近100行日志
```

**危险操作审批流程**：
1. 用户发送重启/停止指令
2. AI 检测到危险操作，向管理员发送审批卡片（私聊）
3. 管理员点击"批准"或"拒绝"
4. 审批通过后自动执行，并通知用户
5. 超时（默认3分钟）自动取消

---

## 可用指令

### 安全操作（无需审批）

1. **查看应用状态** - `get_app_status`
   ```
   /运维 查看生产1号的订单服务状态
   ```

2. **查看版本信息** - `get_app_version`
   ```
   /运维 查下订单服务的版本
   ```

3. **分析 JAR 包** - `get_jar_info`
   ```
   /运维 分析生产2号订单服务的jar包
   ```

4. **查看进程详情** - `get_process_info`
   ```
   /运维 看下用户服务的进程信息
   ```

5. **查看系统资源** - `get_system_metrics`
   ```
   /运维 查看测试服务器的内存使用
   /运维 生产1号的磁盘空间还有多少
   ```

6. **查看日志** - `get_logs`
   ```
   /运维 看下订单服务的最近100行日志
   /运维 查看用户服务的错误日志，grep "Exception"
   ```

### 危险操作（需要审批）

7. **重启应用** - `restart_app`
   ```
   /运维 重启生产1号的订单服务
   ```

8. **停止应用** - `stop_app`
   ```
   /运维 停止测试服的用户服务
   ```

9. **启动应用** - `start_app`
   ```
   /运维 启动生产2号的订单服务
   ```

---

## 自然语言支持

AI 可以理解以下表达：

**机器名称**：
- 配置的正式名称："生产服务器1"
- 别名："生产1号"、"prod1"
- 描述中的关键词："订单相关"

**应用名称**：
- 配置的正式名称："订单服务 v4.3.6"
- 别名："订单"、"order"
- 简短描述："处理用户下单"

**意图理解**：
- "查看状态" = `get_app_status`
- "看下版本" = `get_app_version`
- "内存使用" = `get_system_metrics`
- "重启" = `restart_app`

---

## 扩展指令

### 添加自定义指令

```python
# 在 tools/operations_commands.py 中

class CustomHealthCheckCommand(OperationCommand):
    name = "custom_health_check"
    description = "自定义健康检查（调用应用 API）"
    risk_level = "LOW"
    
    def execute(self, ops_manager, machine, application, params):
        api_url = application.metadata.get('health_check_url')
        command = f"curl -s {api_url}"
        result = ops_manager.execute_ssh_command(machine, command)
        
        return {
            "success": result['success'],
            "health_status": result['stdout']
        }

# 在配置中添加
applications:
  - name: "订单服务"
    metadata:
      health_check_url: "http://localhost:8080/health"
```

注册指令后，AI 自动识别：
```
/运维 对订单服务做自定义健康检查
```

---

## 安全注意事项

### 1. 权限配置
- ✅ 只添加可信人员到 `operators` 白名单
- ✅ 至少配置 2 个 `approvers`（互相制约）
- ✅ 定期审查权限列表

### 2. SSH 安全
- ✅ 优先使用密钥认证（不用密码）
- ✅ 密码必须加密存储（使用 Fernet）
- ✅ 密钥文件权限设置为 600
- ✅ SSH 用户权限最小化（避免 root）

### 3. 脚本安全
- ✅ 所有脚本路径在配置中预定义
- ✅ 脚本内容经过审查（避免恶意命令）
- ✅ 脚本执行用户权限受限

### 4. 审批流程
- ✅ 高危操作强制审批
- ✅ 审批超时自动取消
- ✅ 审批记录混入 chat_history（可追溯）

### 5. 日志审计
- ✅ 所有操作记录到 chat_history
- ✅ 包含操作人、目标、命令、结果
- ✅ 带时间戳和会话 ID

---

## 故障排查

### 问题1：提示"没有运维权限"

**原因**：用户不在 `operators` 白名单中

**解决**：
1. 获取用户的 `open_id`（查看飞书日志）
2. 添加到 `config.yaml` 的 `operators.feishu` 列表
3. 重启服务

### 问题2：SSH 连接失败

**可能原因**：
- 密钥路径错误
- 密钥权限不正确（需要 600）
- 目标机器防火墙
- SSH 用户名/端口错误

**排查**：
```bash
# 手动测试 SSH 连接
ssh -i /path/to/key user@host -p 22

# 检查密钥权限
ls -l /path/to/key
chmod 600 /path/to/key
```

### 问题3：审批超时

**原因**：管理员未及时响应

**解决**：
- 增加 `approval_timeout`（默认 180 秒）
- 添加更多审批人
- 检查管理员是否收到私聊卡片

### 问题4：找不到机器/应用

**原因**：AI 理解的名称与配置不匹配

**解决**：
- 增加 `alias`（别名）
- 丰富 `description`（描述）
- 用户用更明确的表达

---

## 配置示例

### 完整配置（生产环境）

```yaml
operations:
  authorization:
    operators:
      feishu:
        - open_id: "ou_alice123"
          name: "张三（运维工程师）"
        - open_id: "ou_bob456"
          name: "李四（开发负责人）"
    
    approvers:
      feishu:
        - open_id: "ou_admin001"
          name: "运维主管王五"
        - open_id: "ou_admin002"
          name: "技术总监赵六"
    
    approval_timeout: 300  # 5分钟

  machines:
    - name: "生产服务器1"
      alias: ["生产1号", "prod1", "主服务器"]
      host: "192.168.1.10"
      description: "订单和支付服务"
      ssh:
        method: "key"
        user: "deploy"
        key_path: "/home/bot/.ssh/id_rsa_prod1"
        port: 22
      
      applications:
        - name: "订单服务 v4.3.6"
          alias: ["订单", "order"]
          description: "处理用户下单和支付"
          path: "/opt/apps/order-service-4.3.6"
          type: "java"
          
          scripts:
            status: "./status.sh"
            start: "./start.sh"
            stop: "./stop.sh"
            restart: "./restart.sh"
            health: "curl -s localhost:8080/actuator/health"
          
          logs:
            app: "logs/application.log"
            error: "logs/error.log"
          
          metadata:
            version_detection:
              - type: "jar_manifest"
                jar_path: "order-service.jar"
              - type: "log_file"
                command: "grep 'Application Version:' logs/startup.log | tail -1"
            
            pid_file: "app.pid"
            health_check_url: "http://localhost:8080/health"

  system_commands:
    memory: "free -h"
    disk: "df -h"
    cpu: "top -bn1 | head -20"
    uptime: "uptime"
    processes: "ps aux --sort=-%mem | head -20"
```

---

## 常见场景

### 场景1：快速排查线上问题

```
用户: /运维 生产1号订单服务怎么了，CPU 很高
AI: [自动执行]
    1. get_system_metrics（查看 CPU）
    2. get_process_info（查看进程）
    3. get_logs（查看最近日志，grep "ERROR"）
    
    📊 诊断结果：
    CPU 使用率 85%，订单服务进程占用 70%
    最近日志中出现大量数据库连接超时
    
    建议：检查数据库连接池配置或重启服务
```

### 场景2：版本确认

```
用户: /运维 确认下生产环境订单服务的版本
AI: [调用 get_app_version]
    
    📦 订单服务版本
    - JAR Manifest: 4.3.6
    - Git Branch: release/4.3.6
    - Build Time: 2026-09-01 10:30:00
    
    当前运行版本：4.3.6
```

### 场景3：应急重启

```
用户: /运维 紧急重启生产1号订单服务
AI: ⚠️ 重启操作需要审批
    已向运维主管王五和技术总监赵六发送审批请求
    
（王五点击批准）

AI: ✅ 审批通过，正在执行...
    1. 停止服务 ✅
    2. 启动服务 ✅
    3. 健康检查 ✅
    
    ✅ 重启完成，服务已恢复
```

---

## 最佳实践

1. **权限最小化**：只给必要人员运维权限
2. **审批人冗余**：至少 2 个审批人，避免单点
3. **脚本标准化**：所有应用使用统一的脚本规范（status.sh/restart.sh）
4. **版本检测多源**：配置多种版本检测方式，提高准确率
5. **日志定期清理**：chat_history 混入运维记录，注意存储空间
6. **定期演练**：测试环境演练审批流程和应急操作

---

## 性能优化

1. **SSH 连接池**：复用连接，减少握手开销
2. **并发执行**：多台机器查询可并发（AI 自动决策）
3. **缓存**：审批状态缓存在内存（3分钟过期）

---

## 路线图

### 已完成 ✅
- 权限控制（白名单 + 审批）
- 9 个内置指令
- 飞书审批集成
- AI 工具注册
- 会话粘性

### 计划中 📋
- Web 端审批（弹窗）
- 微信渠道支持
- 定时任务（cron）
- 批量操作（多机器并发）
- 操作回滚
- Grafana 集成（嵌入监控图表）

---

如有问题，请查看 `docs/implementation/operations-feature-summary.md` 或联系开发团队。
