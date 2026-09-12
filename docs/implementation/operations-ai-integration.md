# 运维模式 AI 集成实施文档

## 概述

本文档描述运维模式与 AI Provider 的集成实现。

## 已完成的工作

### 1. AnthropicProvider 扩展

**文件**: `src/ai_assistant/providers/anthropic_provider.py`

#### 新增属性
```python
# 运维工具（由外部注入）
self.operations_tools = None
self.operations_enabled = False
self._operations_sessions: set = set()  # 运维模式会话粘性标记
self._operations_sessions_lock = _threading.Lock()
```

#### 新增方法

**1. set_operations_tools(operations_tools, enabled=True)**
- 注入 OperationsTools 实例
- 启用/禁用运维模式

**2. _should_use_operations_mode(messages, session_id)**
- 检测是否应进入运维模式
- 触发条件：
  - 运维工具已启用（前提）
  - 会话级粘性（该 session 曾进入过运维模式）
  - 显式 `/运维` 或 `/ops` 前缀

**3. _send_with_context_operations(messages, doc_context, session_id, ...)**
- 完整的运维 Agentic 循环
- 支持多轮工具调用
- 超时控制（time/rounds 模式）
- 异常处理和错误恢复

**4. _generate_operations_tools_schema()**
- 从 OperationsTools 实例自动生成 Claude tools schema
- 当前返回基础模板（待 Team C 完成后补全）

**5. _execute_operations_tool(tool_name, tool_input)**
- 执行运维工具调用
- 当前返回模拟数据（待 Team C 完成后替换）

#### System Prompt 设计

```
你是运维助手，当前用户是授权运维人员。

可用工具：
- list_machines - 列出可管理的机器
- get_app_status - 查看应用状态
- get_app_version - 查看应用版本/分支
- get_jar_info - 分析 Java 应用 jar 包
- get_process_info - 查看进程详情
- get_system_metrics - 查看系统资源
- get_logs - 查看应用日志
- restart_app - 重启应用（需审批）
- stop_app - 停止应用（需审批）
- start_app - 启动应用（需审批）

安全规则：
1. 重启/停止等危险操作必须返回 need_approval，等管理员确认后再执行
2. 只能操作配置中定义的机器和应用
3. 所有操作记录审计日志

当前用户：{operator_name} ({operator_id}, {source})
```

### 2. main.py 扩展

**文件**: `src/ai_assistant/main.py`

#### _parse_feishu_event 修改
- 提取发送者详细信息：
  - `sender_id` (open_id)
  - `sender_name` (user_id)
  - `sender_display_name` (待实现)
- 所有返回值增加这3个字段

#### 运维模式消息处理
- 检测 `/运维` 或 `/ops` 前缀
- 在用户消息 metadata 中附加操作者信息：
  ```python
  user_message.metadata = {
      "operator_id": parsed.get("sender_id", ""),
      "operator_name": parsed.get("sender_name", ""),
      "operator_display_name": parsed.get("sender_display_name", ""),
      "source": "feishu"
  }
  ```

## 待完成的工作（等待 Team C）

### 1. OperationsTools 实现

**文件**: `src/ai_assistant/core/operations_tools.py` (待创建)

需要实现的核心方法：
```python
class OperationsTools:
    def list_machines(self) -> Dict[str, Any]:
        """列出可管理的机器"""
        pass
    
    def get_app_status(self, machine_name: str, app_name: str = None) -> Dict[str, Any]:
        """查看应用状态"""
        pass
    
    def get_app_version(self, machine_name: str, app_name: str) -> Dict[str, Any]:
        """查看应用版本/分支"""
        pass
    
    def get_jar_info(self, machine_name: str, app_name: str) -> Dict[str, Any]:
        """分析 Java 应用 jar 包"""
        pass
    
    def get_process_info(self, machine_name: str, app_name: str) -> Dict[str, Any]:
        """查看进程详情"""
        pass
    
    def get_system_metrics(self, machine_name: str) -> Dict[str, Any]:
        """查看系统资源"""
        pass
    
    def get_logs(self, machine_name: str, app_name: str, lines: int = 100) -> Dict[str, Any]:
        """查看应用日志"""
        pass
    
    def restart_app(self, machine_name: str, app_name: str, reason: str) -> Dict[str, Any]:
        """重启应用（需审批）"""
        pass
    
    def stop_app(self, machine_name: str, app_name: str, reason: str) -> Dict[str, Any]:
        """停止应用（需审批）"""
        pass
    
    def start_app(self, machine_name: str, app_name: str, reason: str) -> Dict[str, Any]:
        """启动应用（需审批）"""
        pass
```

### 2. OperationsManager 实现

**文件**: `src/ai_assistant/core/operations_manager.py` (待创建)

需要实现的核心方法：
```python
class OperationsManager:
    def is_authorized(self, operator_id: str, source: str) -> bool:
        """鉴权：检查用户是否有运维权限"""
        pass
    
    def create_pending_operation(self, operation: PendingOperation) -> str:
        """创建待审批操作"""
        pass
    
    def approve_operation(self, operation_id: str, approver: str) -> bool:
        """审批通过操作"""
        pass
    
    def reject_operation(self, operation_id: str, approver: str, reason: str) -> bool:
        """拒绝操作"""
        pass
```

### 3. main.py 集成（Team C完成后）

在 `__init__` 方法中初始化运维工具：

```python
# 初始化运维管理器（如果启用）
if getattr(self.config, 'operations_enabled', False):
    from ai_assistant.core.operations_manager import OperationsManager
    from ai_assistant.core.operations_tools import OperationsTools
    
    # 从配置加载机器和应用定义
    machines = getattr(self.config, 'operations_machines', [])
    applications = getattr(self.config, 'operations_applications', [])
    authorized_users = getattr(self.config, 'operations_authorized_users', [])
    
    self.ops_manager = OperationsManager(
        machines=machines,
        applications=applications,
        authorized_users=authorized_users
    )
    
    self.ops_tools = OperationsTools(ops_manager=self.ops_manager)
    
    # 注入到 AI Provider
    if hasattr(self.ai_provider, 'set_operations_tools'):
        self.ai_provider.set_operations_tools(self.ops_tools, enabled=True)
        logger.info("运维模式已启用")
    else:
        logger.warning("当前 AI Provider 不支持运维模式")
```

### 4. 配置文件示例

**config.yaml** 新增配置项：

```yaml
# 运维模式配置
operations_enabled: true
operations_authorized_users:
  - source: feishu
    user_id: ou_xxx123  # 飞书 open_id
    name: "张三"
  - source: feishu
    user_id: ou_xxx456
    name: "李四"

operations_machines:
  - name: web-server-1
    display_name: "Web服务器1"
    description: "生产环境前端服务器"
    ssh_config:
      host: 192.168.1.10
      port: 22
      username: deploy
      method: key
      key_path: /path/to/key
    tags: [production, web]
    applications: [nginx, web-app]

operations_applications:
  - name: web-app
    display_name: "Web应用"
    description: "前端Web应用"
    machines: [web-server-1]
    tags: [java, springboot]
```

### 5. 工具 Schema 自动生成

完善 `_generate_operations_tools_schema()` 方法，从 OperationsTools 的方法签名和文档自动生成：

```python
def _generate_operations_tools_schema(self) -> List[Dict[str, Any]]:
    """从 OperationsTools 实例自动生成 Claude tools schema"""
    if not self.operations_tools:
        return []
    
    import inspect
    
    schema = []
    for method_name in dir(self.operations_tools):
        if method_name.startswith('_'):
            continue
        
        method = getattr(self.operations_tools, method_name)
        if not callable(method):
            continue
        
        # 解析方法签名
        sig = inspect.signature(method)
        doc = inspect.getdoc(method) or ""
        
        # 构建 tool schema
        tool = {
            "name": method_name,
            "description": doc.split('\n')[0],  # 第一行作为描述
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
            
            param_type = "string"  # 默认类型
            if param.annotation != inspect.Parameter.empty:
                if param.annotation == int:
                    param_type = "integer"
                elif param.annotation == bool:
                    param_type = "boolean"
            
            tool["input_schema"]["properties"][param_name] = {
                "type": param_type,
                "description": f"{param_name} 参数"
            }
            
            if param.default == inspect.Parameter.empty:
                tool["input_schema"]["required"].append(param_name)
        
        schema.append(tool)
    
    return schema
```

### 6. 工具调用实现

完善 `_execute_operations_tool()` 方法：

```python
def _execute_operations_tool(self, tool_name: str, tool_input: Dict[str, Any]) -> Any:
    """执行运维工具调用"""
    if not self.operations_tools:
        return {"error": "运维工具未初始化"}
    
    try:
        method = getattr(self.operations_tools, tool_name, None)
        if not method:
            return {"error": f"未知运维工具: {tool_name}"}
        
        # 调用方法
        result = method(**tool_input)
        return result
    
    except Exception as e:
        logger.error(f"运维工具 {tool_name} 执行异常: {e}", exc_info=True)
        return {"error": str(e)}
```

## 测试计划

### 单元测试
- [ ] `_should_use_operations_mode()` 各种触发条件
- [ ] `_generate_operations_tools_schema()` schema 生成正确性
- [ ] `_execute_operations_tool()` 工具调用正确性
- [ ] 会话级粘性标记机制

### 集成测试
1. 发送 `/运维 列出所有机器` → 触发 list_machines
2. 发送 `/运维 查看web-server-1的状态` → 触发 get_app_status
3. 发送 `/运维 重启web-app` → 返回需要审批提示
4. 追问模式测试（会话粘性）
5. 鉴权测试（授权用户/非授权用户）

### 错误处理测试
- SSH 连接失败
- 工具调用超时
- AI API 调用失败
- 工具返回错误

## 安全注意事项

1. **鉴权验证**：每次运维操作前必须验证用户身份
2. **审批流程**：危险操作必须经过审批
3. **审计日志**：所有运维操作记录到审计日志
4. **最小权限**：只能操作配置中定义的机器和应用
5. **敏感信息**：SSH密码、密钥路径不记录到日志

## 已知限制

1. 当前仅支持飞书渠道获取用户身份
2. 审批流程需要人工确认（未实现自动化）
3. 工具 schema 自动生成依赖 Python 类型注解
4. 多租户隔离需要额外实现

## 参考

- Team Lead 任务分配: 运维模式集成
- operations_models.py: 数据模型定义
- anthropic_provider.py: AI Provider 实现
- main.py: 主程序入口
