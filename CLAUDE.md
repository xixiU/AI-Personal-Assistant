# AI 自动回复助手 - 开发规范

本文档为 AI 助手和开发者提供项目开发的规范、约束和上下文信息。

## 项目概述

**项目名称**: AI 自动回复助手
**目标**: 为 IM 工具（飞书、微信）提供基于大模型的智能自动回复功能
**技术栈**: Python 3.10+, pywinauto, requests, loguru, pytest
**当前状态**: MVP 阶段已完成，核心功能可运行

## 核心架构原则

### 1. 插件化设计

**IM 适配器必须独立**
- 每个 IM 工具（飞书、微信等）作为独立的适配器实现
- 所有适配器继承 `IMAdapter` 基类
- 适配器之间不应有依赖关系
- 新增 IM 支持只需实现新的适配器类

**AI Provider 可扩展**
- 支持多种 AI 服务（CherryStudio, OpenAI, 自定义）
- 所有 Provider 继承 `AIProvider` 基类
- Provider 之间可以作为主备切换

### 2. 非侵入式原则

**严格禁止**
- ❌ 修改 IM 客户端文件
- ❌ 注入代码到 IM 进程
- ❌ Hook 系统 API
- ❌ 拦截网络请求

**允许的方式**
- ✅ 使用 Windows UI 自动化 API
- ✅ 读取窗口内容
- ✅ 模拟键盘输入（需用户授权）
- ✅ 剪贴板操作

### 3. 安全优先

**配置文件安全**
- `config.yaml` 必须在 `.gitignore` 中
- 敏感信息（API Key）不得硬编码
- 提供 `config.example.yaml` 作为模板

**数据隐私**
- 消息内容仅在本地处理
- 不上传到第三方服务器（除用户配置的 AI 服务）
- 日志中不记录完整消息内容（仅记录长度和摘要）

**权限最小化**
- 仅请求必要的系统权限
- 用户可随时禁用功能

## 代码规范

### 文件组织

```
src/ai_assistant/
├── core/              # 核心模块
│   ├── models.py      # 数据模型
│   ├── config.py      # 配置管理
│   ├── context_manager.py  # 上下文管理
│   ├── ai_provider.py      # AI Provider 接口
│   └── reply_executor.py   # 回复执行
├── adapters/          # IM 适配器
│   ├── base.py        # 适配器基类
│   ├── feishu.py      # 飞书适配器
│   └── wechat.py      # 微信适配器（待实现）
├── providers/         # AI Provider 实现
│   ├── cherrystudio.py
│   └── openai.py      # OpenAI 兼容（待实现）
├── utils/             # 工具函数
└── main.py            # 主程序入口
```

### Python 代码风格

**遵循 PEP 8**
- 使用 4 空格缩进
- 行长度限制 100 字符
- 类名使用 PascalCase
- 函数名使用 snake_case
- 常量使用 UPPER_CASE

**类型注解**
```python
# 必须：函数参数和返回值
def send_message(self, messages: List[Message]) -> str:
    pass

# 推荐：类属性
class Config:
    trigger_keyword: str = "【ai】"
```

**文档字符串**
```python
def function_name(param1: str, param2: int) -> bool:
    """
    简短描述（一行）

    Args:
        param1: 参数1说明
        param2: 参数2说明

    Returns:
        返回值说明

    Raises:
        Exception: 异常说明
    """
    pass
```

### 日志规范

**使用 loguru**
```python
from loguru import logger

# 日志级别使用
logger.debug("调试信息")      # 详细的调试信息
logger.info("正常信息")       # 关键操作记录
logger.warning("警告信息")    # 可恢复的问题
logger.error("错误信息")      # 需要关注的错误
```

**日志内容要求**
- 不记录完整的消息内容（隐私保护）
- 记录消息长度、时间戳、会话 ID
- 记录 AI API 调用的耗时和状态
- 错误日志必须包含异常堆栈

## 测试要求

### 测试覆盖

**必须测试**
- ✅ 所有核心模块（models, config, context_manager）
- ✅ AI Provider 的接口调用
- ✅ 配置文件加载和解析
- ✅ 数据模型的边界条件

**可选测试**
- 🔶 IM 适配器（依赖真实窗口，难以自动化）
- 🔶 UI 自动化逻辑（需要 mock）

### 测试命名

```python
# 格式：test_<功能>_<场景>
def test_session_add_message():
    pass

def test_session_max_messages_limit():
    pass

def test_config_load_from_file():
    pass
```

### 运行测试

```bash
# 运行所有测试
PYTHONPATH=src pytest tests/unit/ -v

# 运行特定测试
PYTHONPATH=src pytest tests/unit/test_models.py -v

# 测试覆盖率
PYTHONPATH=src pytest tests/unit/ --cov=ai_assistant --cov-report=html
```

## Git 工作流

### 提交规范

**Commit Message 格式**
```
<type>: <subject>

<body>
```

**Type 类型**
- `feat`: 新功能
- `fix`: Bug 修复
- `docs`: 文档更新
- `refactor`: 代码重构
- `test`: 测试相关
- `chore`: 构建/工具链相关
- `security`: 安全相关

**示例**
```
feat: add WeChat adapter with message extraction

- Implement WeChatAdapter class
- Add window detection logic
- Support private chat and group chat
```

### 分支策略

- `main`: 稳定版本，可运行
- `develop`: 开发分支（如需要）
- `feature/*`: 功能分支
- `fix/*`: 修复分支

### 提交前检查

```bash
# 1. 运行测试
PYTHONPATH=src pytest tests/unit/ -v

# 2. 检查代码风格（如果配置了 flake8）
flake8 src/

# 3. 查看改动
git diff

# 4. 提交
git add <files>
git commit -m "type: message"
```

## 开发约束

### 禁止事项

**代码层面**
- ❌ 不得在代码中硬编码 API Key 或密码
- ❌ 不得使用 `eval()` 或 `exec()` 执行动态代码
- ❌ 不得在日志中记录完整的用户消息
- ❌ 不得绕过配置文件直接修改行为

**依赖管理**
- ❌ 不得添加未经审查的第三方库
- ❌ 不得使用已知有安全漏洞的库版本
- ❌ 不得添加体积过大的依赖（如深度学习框架）

**UI 自动化**
- ❌ 不得使用破坏性操作（如自动删除消息）
- ❌ 不得在未授权情况下自动发送消息
- ❌ 不得读取 IM 工具的配置文件或数据库

### 必须遵守

**新增功能**
- ✅ 必须编写单元测试
- ✅ 必须更新 README.md
- ✅ 必须更新 config.example.yaml（如涉及配置）
- ✅ 必须考虑向后兼容性

**修改现有功能**
- ✅ 必须确保现有测试通过
- ✅ 必须更新相关文档
- ✅ 必须考虑对用户的影响

**安全相关**
- ✅ 必须使用 HTTPS 调用 AI API
- ✅ 必须验证用户输入
- ✅ 必须处理异常情况

## 已知限制和注意事项

### 技术限制

1. **飞书适配器是简化实现**
   - 当前需要手动复制消息触发
   - 完整的 UI 自动化需要更复杂的元素定位
   - 不同版本的飞书客户端可能需要适配

2. **仅支持 Windows 平台**
   - pywinauto 依赖 Windows API
   - macOS/Linux 需要使用不同的自动化库

3. **多模态支持未完成**
   - 当前仅支持文本消息
   - 图片、视频处理需要额外实现

### 性能考虑

1. **轮询间隔**: 默认 500ms，不建议低于 100ms
2. **上下文长度**: 默认 10 条消息，过多会增加 token 消耗
3. **AI 调用超时**: 默认 30 秒，根据模型调整

### 兼容性

1. **Python 版本**: 3.10+（使用了新的类型注解语法）
2. **IM 客户端版本**: 需要定期测试和适配
3. **AI 模型**: 必须支持 OpenAI 兼容的 API 格式

## 未来开发方向

### 优先级 P0（必须）

- [ ] 完善飞书适配器的 UI 自动化
- [ ] 添加错误重试机制
- [ ] 优化日志输出格式

### 优先级 P1（重要）

- [ ] 实现微信适配器
- [ ] 支持 OpenAI 兼容 API
- [ ] 添加图片多模态支持
- [ ] 智能上下文判断

### 优先级 P2（可选）

- [ ] GUI 配置界面
- [ ] 自动输入模式
- [ ] 视频处理支持
- [ ] 更多 IM 工具支持（钉钉、Slack）

## 常见问题（开发者）

### Q: 如何添加新的 IM 适配器？

1. 在 `src/ai_assistant/adapters/` 创建新文件
2. 继承 `IMAdapter` 基类
3. 实现所有抽象方法
4. 在 `config.example.yaml` 添加配置
5. 编写单元测试
6. 更新 README.md

### Q: 如何添加新的 AI Provider？

1. 在 `src/ai_assistant/providers/` 创建新文件
2. 继承 `AIProvider` 基类
3. 实现 `send_message()` 和 `check_health()`
4. 在配置中添加支持
5. 编写测试

### Q: 如何调试 UI 自动化问题？

1. 设置日志级别为 DEBUG
2. 使用 `pywinauto` 的 `print_control_identifiers()` 查看窗口结构
3. 使用 `pyautogui.displayMousePosition()` 查看坐标
4. 逐步测试每个 UI 操作

### Q: 如何处理 IM 客户端更新？

1. 记录当前版本号
2. 测试新版本的窗口结构变化
3. 更新适配器代码
4. 添加版本检测逻辑（如需要）

## 参考资源

- [pywinauto 文档](https://pywinauto.readthedocs.io/)
- [loguru 文档](https://loguru.readthedocs.io/)
- [pytest 文档](https://docs.pytest.org/)
- [OpenAI API 文档](https://platform.openai.com/docs/api-reference)

---

**最后更新**: 2026-03-06
**维护者**: 项目团队

**重要提醒**: 本项目涉及 UI 自动化和用户隐私，开发时必须严格遵守本文档的约束和规范。
