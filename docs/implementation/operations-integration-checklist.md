# 运维模式集成清单

## Team C 完成后的集成步骤

### 步骤 1: 验证 Team C 交付物

检查以下文件是否存在且实现完整：

- [ ] `src/ai_assistant/core/operations_tools.py`
  - [ ] OperationsTools 类定义
  - [ ] 所有核心方法实现（list_machines, get_app_status, get_logs, restart_app 等）
  - [ ] 完整的类型注解和文档字符串

- [ ] `src/ai_assistant/core/operations_manager.py`
  - [ ] OperationsManager 类定义
  - [ ] 鉴权方法 is_authorized()
  - [ ] 审批流程方法（create_pending_operation, approve_operation, reject_operation）

### 步骤 2: 更新 anthropic_provider.py

#### 2.1 完善 _generate_operations_tools_schema()

替换当前的硬编码 schema 为自动生成：

```python
def _generate_operations_tools_schema(self) -> List[Dict[str, Any]]:
    """从 OperationsTools 实例自动生成 Claude tools schema"""
    if not self.operations_tools:
        return []
    
    import inspect
    
    schema = []
    # 遍历 OperationsTools 的所有公开方法
    for method_name in dir(self.operations_tools):
        if method_name.startswith('_'):
            continue
        
        method = getattr(self.operations_tools, method_name)
        if not callable(method):
            continue
        
        # 解析方法签名和文档
        sig = inspect.signature(method)
        doc = inspect.getdoc(method) or f"{method_name} 方法"
        
        tool = {
            "name": method_name,
            "description": doc.split('\n')[0],
            "input_schema": {
                "type": "object",
                "properties": {},
                "required": []
            }
        }
        
        # 解析参数
        for param_name, param in sig.parameters.items():
            if param_name == 'self':
                continue
            
            # 根据类型注解确定参数类型
            param_type = "string"
            if param.annotation != inspect.Parameter.empty:
                if param.annotation == int:
                    param_type = "integer"
                elif param.annotation == bool:
                    param_type = "boolean"
                elif param.annotation == list or param.annotation == List:
                    param_type = "array"
            
            tool["input_schema"]["properties"][param_name] = {
                "type": param_type,
                "description": f"{param_name} 参数"
            }
            
            # 必需参数（没有默认值）
            if param.default == inspect.Parameter.empty:
                tool["input_schema"]["required"].append(param_name)
        
        schema.append(tool)
    
    logger.info(f"自动生成 {len(schema)} 个运维工具 schema")
    return schema
```

#### 2.2 完善 _execute_operations_tool()

替换模拟数据为真实调用：

```python
def _execute_operations_tool(self, tool_name: str, tool_input: Dict[str, Any]) -> Any:
    """执行运维工具调用"""
    if not self.operations_tools:
        return {"error": "运维工具未初始化"}
    
    try:
        # 动态获取方法
        method = getattr(self.operations_tools, tool_name, None)
        if not method or not callable(method):
            return {"error": f"未知运维工具: {tool_name}"}
        
        # 调用方法
        logger.info(f"调用运维工具: {tool_name}({tool_input})")
        result = method(**tool_input)
        return result
    
    except Exception as e:
        logger.error(f"运维工具 {tool_name} 执行异常: {e}", exc_info=True)
        return {"error": str(e), "tool": tool_name}
```

### 步骤 3: 更新 main.py

在 `__init__` 方法中添加运维工具初始化（在 AI Provider 初始化之后）：

```python
# 初始化运维工具（如果启用）
if getattr(self.config, 'operations_enabled', False):
    from ai_assistant.core.operations_manager import OperationsManager
    from ai_assistant.core.operations_tools import OperationsTools
    
    try:
        # 从配置加载机器和应用定义
        machines_config = getattr(self.config, 'operations_machines', [])
        applications_config = getattr(self.config, 'operations_applications', [])
        authorized_users = getattr(self.config, 'operations_authorized_users', [])
        
        # 初始化管理器
        self.ops_manager = OperationsManager(
            machines_config=machines_config,
            applications_config=applications_config,
            authorized_users=authorized_users
        )
        
        # 初始化工具
        self.ops_tools = OperationsTools(ops_manager=self.ops_manager)
        
        # 注入到 AI Provider
        if hasattr(self.ai_provider, 'set_operations_tools'):
            self.ai_provider.set_operations_tools(self.ops_tools, enabled=True)
            logger.info(f"运维工具已启用: {len(machines_config)} 台机器, {len(applications_config)} 个应用")
        else:
            logger.warning("当前 AI Provider 不支持运维模式")
    
    except Exception as e:
        logger.error(f"初始化运维工具失败: {e}", exc_info=True)
        logger.warning("运维功能将不可用")
else:
    logger.info("运维模式未启用")
```

### 步骤 4: 添加配置项

在 `config.example.yaml` 和 `config.yaml` 中添加：

```yaml
# ========== 运维模式配置 ==========
operations_enabled: false  # 是否启用运维模式

# 授权用户列表（只有这些用户可以使用 /运维 命令）
operations_authorized_users:
  - source: feishu
    user_id: ou_xxx123  # 飞书 open_id
    name: "张三"
    roles: [operator, admin]

# 可管理的机器列表
operations_machines:
  - name: web-server-1
    display_name: "Web服务器1"
    description: "生产环境前端服务器"
    ssh_config:
      host: 192.168.1.10
      port: 22
      username: deploy
      method: key  # password 或 key
      key_path: /path/to/deploy_key
      timeout: 30
    tags: [production, web]
    applications: [nginx, web-app]

# 应用定义
operations_applications:
  - name: web-app
    display_name: "Web应用"
    description: "前端Web应用（Spring Boot）"
    machines: [web-server-1]
    tags: [java, springboot, frontend]
    metadata:
      port: 8080
      log_path: /opt/app/logs
      pid_file: /opt/app/app.pid
```

### 步骤 5: 在 _process_event 中添加鉴权检查

在调用 AI 之前添加运维模式鉴权：

```python
# 运维模式鉴权检查
if text and (text.strip().startswith("/运维") or text.strip().startswith("/ops")):
    if not hasattr(self, 'ops_manager'):
        reply = "❌ 运维模式未启用，请联系管理员"
        self._send_reply(adapter, session_id, message_id, reply)
        return
    
    # 检查用户权限
    operator_id = parsed.get("sender_id", "")
    if not self.ops_manager.is_authorized(operator_id, source="feishu"):
        reply = "❌ 您没有运维权限，如需开通请联系管理员"
        logger.warning(f"未授权用户尝试使用运维模式: {operator_id}")
        self._send_reply(adapter, session_id, message_id, reply)
        return
    
    logger.info(f"运维模式鉴权通过: user={operator_id}")
```

找到合适位置插入（在 `ai_start = time_mod.time()` 之前）。

### 步骤 6: 实现 _send_reply 辅助方法

如果 main.py 中没有 `_send_reply` 方法，添加：

```python
def _send_reply(self, adapter, session_id: str, message_id: str, reply: str):
    """发送回复消息的辅助方法"""
    try:
        adapter.send_message(session_id, reply, reply_to_message_id=message_id)
    except Exception as e:
        logger.error(f"发送回复失败: {e}")
```

### 步骤 7: 测试

#### 7.1 单元测试

创建 `tests/unit/test_operations_integration.py`：

```python
import pytest
from ai_assistant.providers.anthropic_provider import AnthropicProvider
from ai_assistant.core.operations_tools import OperationsTools
from ai_assistant.core.operations_manager import OperationsManager

def test_operations_tools_injection():
    """测试运维工具注入"""
    provider = AnthropicProvider(api_key="test-key")
    ops_manager = OperationsManager(machines_config=[], applications_config=[], authorized_users=[])
    ops_tools = OperationsTools(ops_manager=ops_manager)
    
    provider.set_operations_tools(ops_tools, enabled=True)
    
    assert provider.operations_tools == ops_tools
    assert provider.operations_enabled == True

def test_operations_mode_detection():
    """测试运维模式检测"""
    provider = AnthropicProvider(api_key="test-key")
    ops_manager = OperationsManager(machines_config=[], applications_config=[], authorized_users=[])
    ops_tools = OperationsTools(ops_manager=ops_manager)
    provider.set_operations_tools(ops_tools, enabled=True)
    
    from ai_assistant.core.models import Message, Content
    from datetime import datetime
    
    # 测试 /运维 前缀
    messages = [
        Message(
            role="user",
            content=[Content(type="text", data="/运维 列出所有机器")],
            timestamp=datetime.now()
        )
    ]
    
    assert provider._should_use_operations_mode(messages, session_id="test-session")

def test_schema_generation():
    """测试工具 schema 自动生成"""
    provider = AnthropicProvider(api_key="test-key")
    ops_manager = OperationsManager(machines_config=[], applications_config=[], authorized_users=[])
    ops_tools = OperationsTools(ops_manager=ops_manager)
    provider.set_operations_tools(ops_tools, enabled=True)
    
    schema = provider._generate_operations_tools_schema()
    
    assert isinstance(schema, list)
    assert len(schema) > 0
    # 验证 schema 结构
    for tool in schema:
        assert "name" in tool
        assert "description" in tool
        assert "input_schema" in tool
```

运行测试：

```bash
PYTHONPATH=src pytest tests/unit/test_operations_integration.py -v
```

#### 7.2 集成测试

在飞书中发送消息测试：

1. **基础测试**
   - 发送: `/运维 列出所有机器`
   - 预期: 返回机器列表

2. **状态查询**
   - 发送: `/运维 查看web-server-1的应用状态`
   - 预期: 返回应用运行状态

3. **日志查看**
   - 发送: `/运维 查看web-app的最近100行日志`
   - 预期: 返回日志内容

4. **危险操作**
   - 发送: `/运维 重启web-app`
   - 预期: 返回需要审批提示

5. **追问测试**（会话粘性）
   - 第一条: `/运维 列出所有机器`
   - 第二条: `查看第一台的状态` （不带 /运维）
   - 预期: 第二条也进入运维模式

6. **鉴权测试**
   - 用未授权账号发送: `/运维 列出所有机器`
   - 预期: 返回权限不足提示

### 步骤 8: 文档更新

- [ ] 更新 README.md，添加运维模式使用说明
- [ ] 更新 config.example.yaml，添加配置注释
- [ ] 编写运维模式用户手册

## 验收标准

- [ ] 代码编译通过，无语法错误
- [ ] 所有单元测试通过
- [ ] 集成测试所有用例通过
- [ ] 鉴权机制生效，未授权用户被拒绝
- [ ] 危险操作正确返回审批提示
- [ ] 会话粘性机制工作正常
- [ ] 日志记录完整，包含审计信息
- [ ] 错误处理健壮，SSH 连接失败不会导致程序崩溃
- [ ] 文档完整，配置清晰

## 注意事项

1. **安全优先**: 所有运维操作必须经过鉴权
2. **审计日志**: 记录所有运维操作，包括操作者、时间、目标、结果
3. **错误恢复**: SSH 连接失败、命令执行超时等场景要有友好提示
4. **权限最小化**: 只能操作配置中定义的机器和应用
5. **敏感信息**: SSH 密码、密钥内容不记录到日志

## 联系人

- Team D (AI 集成): 当前文档作者
- Team C (运维工具): operations_tools.py 和 operations_manager.py 负责人
- Team Lead: 整体协调
