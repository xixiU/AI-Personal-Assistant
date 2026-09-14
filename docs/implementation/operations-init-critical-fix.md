# 运维功能初始化修复 + 群组白名单支持

**实施时间**: 2025-01-XX  
**问题**: 运维功能配置存在但从未初始化 + 缺少群组白名单支持

---

## 根本原因

### 问题1：Config 类缺少 operations 字段 ⭐ 关键问题
- **现象**: 用户发送 `/运维` 指令，AI 未进入运维模式，而是进行文档检索
- **根本原因**: `Config` 类中没有定义 `operations` 字段
- **导致**: `hasattr(self.config, 'operations')` 永远返回 `False`
- **影响**: 运维功能的初始化代码永远不会执行

### 问题2：缺少群组白名单支持
- **需求**: 希望通过飞书群组设置运维白名单，群内所有成员都有权限
- **原因**: `operations_manager.py` 的 `is_authorized()` 方法只支持个人白名单

---

## 解决方案

### 1. 在 Config 类中添加 operations 字段 ⭐ 关键修复
```python
# src/ai_assistant/core/config.py
@dataclass
class Config:
    # ... 其他字段
    
    # 运维操作配置
    operations: Optional[Dict[str, Any]] = None  # 新增
```

### 2. 在 Config.load() 中解析 operations
```python
# 解析运维操作配置
if "operations" in data:
    config.operations = data["operations"]
```

### 3. 添加 OperationsManager.from_config() 工厂方法
- 从配置字典创建 `OperationsManager` 实例
- 解析 `machines`、`applications`、`authorization` 配置

### 4. 在 main.py 中初始化运维功能
```python
# 初始化运维功能（如果启用）
if hasattr(self.config, 'operations') and self.config.operations:
    self.operations_manager = OperationsManager.from_config(self.config.operations)
    self.operations_tools = OperationsTools(operations_manager=self.operations_manager)
    self.ai_provider.set_operations_tools(self.operations_tools, enabled=True)
    logger.info("运维操作功能已启用")  # 关键日志
```

### 5. 增强权限鉴权支持群组白名单
```python
def is_authorized(
    self,
    operator: OperatorIdentity,
    machine: Machine,
    operation_type: str = "read",
    chat_id: Optional[str] = None  # 新增
) -> bool:
    # 1. 检查用户白名单
    # 2. 检查群组白名单（如果提供了 chat_id）
```

---

## 代码变更清单

### 修改的文件
1. **src/ai_assistant/core/config.py** ⭐ 关键修复
   - 添加 `operations: Optional[Dict[str, Any]] = None` 字段
   - 在 `load()` 方法中解析 `operations` 配置

2. **src/ai_assistant/core/operations_manager.py**
   - 新增 `from_config()` 类方法
   - 新增 `authorization_config` 参数
   - 增强 `is_authorized()` 支持群组白名单
   - 修改 `execute_ssh_command()` 接收 `chat_id` 参数

3. **src/ai_assistant/main.py**
   - 添加运维功能初始化逻辑（使用 `from_config()`）
   - 在消息 metadata 中添加 `chat_id`

4. **config.operations.example.yaml**
   - 新增 `groups` 配置段

5. **docs/operations-user-guide.md**
   - 更新配置示例，说明群组白名单

---

## 测试验证

### 验证步骤
1. **检查配置加载**：重启服务，查看日志是否出现 "运维操作功能已启用"
2. **测试运维模式**：在飞书发送 `/运维 查看xxx服务器的状态`
3. **验证鉴权**：
   - 白名单内用户：正常执行运维操作
   - 白名单外用户：返回"权限不足"错误
   - 群组白名单内任何成员：都有运维权限

### 预期日志
```
INFO - 运维操作功能已启用
INFO - 触发 Operations 模式：检测到 /运维 或 /ops 前缀
```

---

## Bug 原因总结

**三层问题**：
1. **Config 类缺少字段** → `hasattr(self.config, 'operations')` 返回 False
2. **初始化代码未执行** → `operations_manager` 和 `operations_tools` 为 None
3. **AI Provider 未注入工具** → `operations_enabled` 为 False

**结果**：`/运维` 指令被当作普通消息处理，进入 RAG 文档检索流程。

---

## 避免类似问题

1. **配置字段必须在 Config 类中声明**
2. **配置字段必须在 Config.load() 中解析**
3. **功能启用必须有明确的日志输出**
4. **参考其他功能的初始化模式**（如 `troubleshoot`、`feishu_docs`）

---

## 配置示例

```yaml
operations:
  authorization:
    # 个人白名单
    operators:
      feishu:
        - open_id: "ou_alice123456"
          name: "张三"
    
    # 群组白名单（新增）
    groups:
      feishu:
        - chat_id: "oc_1a2b3c4d5e6f"
          name: "运维团队群"
        - chat_id: "oc_7g8h9i0j1k2l"
          name: "研发环境运维群"
    
    approvers:
      feishu:
        - open_id: "ou_admin001"
          name: "运维主管"
  
  machines:
    - name: "研发环境"
      host: "192.168.1.100"
      ssh:
        method: "key"
        user: "deploy"
        key_path: "/path/to/key"
```
