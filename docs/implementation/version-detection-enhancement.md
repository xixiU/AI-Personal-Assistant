# 版本检测功能增强

**实施时间**: 2025-01-XX  
**需求**: 支持从 JAR 包内读取自定义文件（如 `BOOT-INF/classes/git.info`）+ 自动按配置尝试多种检测方式

---

## 问题与解决方案

### 原始问题
1. 配置文件中定义了 `version_detection`，但代码完全未实现
2. `jar_manifest` 类型硬编码读取 `META-INF/MANIFEST.MF`，无法支持自定义路径
3. 用户内部使用 `BOOT-INF/classes/git.info` 存储版本信息

### 解决方案
- `jar_manifest` 支持可选的 `file_path` 参数（默认 `META-INF/MANIFEST.MF`）
- 在 `operations_tools.py` 中实现自动从配置读取并按顺序尝试的逻辑
- 保持向后兼容

---

## 代码变更

### 1. `operations_commands.py` - GetAppVersionCommand

**新增参数**:
```python
def execute(
    self,
    machine: Machine,
    source: str = "jar",
    jar_path: Optional[str] = None,
    jar_file_path: Optional[str] = None,  # 新增：支持自定义文件路径
    # ...
)
```

**`_get_version_from_jar()` 方法**:
```python
def _get_version_from_jar(
    self,
    machine: Machine,
    jar_path: str,
    file_path: str = "META-INF/MANIFEST.MF"  # 可配置
) -> CommandResult:
    command = f"unzip -p {shlex.quote(jar_path)} {shlex.quote(file_path)} 2>/dev/null"
    # ...
```

### 2. `operations_tools.py` - OperationsTools

**增强 `get_app_version()` 方法**:
```python
def get_app_version(
    self,
    machine: Machine,
    application_metadata: Optional[Dict[str, Any]] = None,  # 新增：自动模式
    source: Optional[str] = None,  # 手动模式
    jar_path: Optional[str] = None,
    jar_file_path: Optional[str] = None,  # 新增
    # ...
) -> Dict[str, Any]:
    # 自动模式：从 metadata.version_detection 读取并按顺序尝试
    if application_metadata and 'version_detection' in application_metadata:
        return self._auto_detect_version(...)
    
    # 手动模式：直接调用
    if source:
        return command.execute(...)
```

**新增 `_auto_detect_version()` 辅助方法**:
- 遍历 `version_detection` 列表
- 根据 `type` 字段分发到不同检测方式
- 返回第一个成功的结果
- 记录尝试的方法和错误信息

---

## 配置示例

### 用户场景（git.info 文件）
```yaml
applications:
  - name: "订单服务"
    metadata:
      version_detection:
        # 优先：自定义 git.info
        - type: "jar_manifest"
          jar_path: "order-service.jar"
          file_path: "BOOT-INF/classes/git.info"
        
        # 备用：标准 MANIFEST.MF
        - type: "jar_manifest"
          jar_path: "order-service.jar"
        
        # 其他备用方式
        - type: "log_file"
          command: "grep 'Version:' logs/startup.log | tail -1"
```

### 支持的检测类型
1. **jar_manifest**: 从 JAR 读取文件
   - `jar_path`: JAR 包路径（必填）
   - `file_path`: JAR 内文件路径（可选，默认 `META-INF/MANIFEST.MF`）

2. **log_file**: 从日志提取
   - `command`: Shell 命令（必填）

3. **version_file**: 读取版本文件
   - `file_path`: 文件路径（必填）

4. **api_endpoint**: HTTP API 查询
   - `url`: API 地址（必填）

---

## 使用方式

### AI 自动调用
```
用户: /运维 查看订单服务的版本
AI: [自动从配置的 version_detection 中按顺序尝试]
    ✅ 成功使用方法: jar_manifest (method 1)
    版本信息: ...
```

### 返回结果
```json
{
  "success": true,
  "data": {"version": "4.5.0", "commit": "abc123"},
  "method_used": "jar_manifest (method 1)",
  "tried_methods": ["jar_manifest (method 1)"]
}
```

---

## 向后兼容性

✅ 完全兼容：
- 原有手动调用方式不受影响
- `jar_manifest` 默认行为不变（读取 `META-INF/MANIFEST.MF`）
- 不指定 `file_path` 时使用默认值

新增能力：
- 支持 JAR 包内任意文件路径
- 自动按配置顺序尝试多种检测方式

---

## 测试建议

1. 测试默认行为：不指定 `file_path`
2. 测试自定义路径：`file_path="BOOT-INF/classes/git.info"`
3. 测试自动探测：多个 `version_detection` 配置
4. 测试失败回退：第一个失败后继续尝试
5. 测试全失败：返回完整错误列表

---

## 重要修复步骤记录

### 问题复现
1. 配置文件中定义了 `version_detection`，但代码中搜索 `version_detection` 关键词返回 0 结果
2. 用户询问时才发现这是实现漏洞

### 避免类似问题
- 配置文件中定义的功能必须有对应的代码实现
- 配置示例应该与实际支持的功能保持同步
- 定期审查配置文件与代码的一致性
