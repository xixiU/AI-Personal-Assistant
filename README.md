# AI 自动回复助手

一个基于大模型的 IM 工具自动回复系统，支持微信和飞书等即时通讯工具。

## 项目简介

本项目通过 Windows UI 自动化技术监听 IM 客户端窗口，当检测到特定触发词时，自动调用 AI 模型生成回复并复制到剪贴板，实现智能自动回复功能。

### 核心特性

- 🤖 **智能回复**：集成 CherryStudio 本地 AI 模型，支持上下文理解
- 💬 **多平台支持**：支持飞书、微信等主流 IM 工具
- 🎯 **灵活触发**：通过关键词【ai】触发，支持 @提及和私聊场景
- 📝 **上下文管理**：自动维护短期消息历史，提供更准确的回复
- 🔄 **双模式支持**：飞书支持机器人 API 和 UI 自动化两种模式
- 🔒 **非侵入式**：通过官方 API 或 UI 自动化实现，不修改 IM 客户端
- 🎨 **多模态支持**：计划支持文本、图片、视频等多种消息类型

## 开发状态

✅ **MVP 阶段已完成！程序现在可以运行了。**

已完成：
- ✅ 项目基础架构
- ✅ 数据模型定义（Content, Message, Session）
- ✅ 配置管理模块
- ✅ 上下文管理器
- ✅ AI 集成模块（CherryStudio）
- ✅ 飞书适配器 - 双模式支持
  - ✅ 机器人 API 模式（官方 API，适用于个人飞书账号）
  - ✅ UI 自动化模式（无需审批，企业环境备选）
- ✅ 微信适配器（基于 pywechat）
- ✅ 回复执行模块（剪贴板模式 + API 模式）
- ✅ 主程序循环
- ✅ Webhook 服务器（用于机器人模式）

待改进：
- 🚧 UI 自动化的完整实现（自动读取消息而非手动复制）
- 🚧 图片多模态支持
- 🚧 智能上下文判断

## 快速开始

### 前提条件

1. **Python 3.10+** 已安装
2. **CherryStudio** 或其他 OpenAI 兼容的 AI 服务正在运行

### 安装步骤

**1. 克隆项目**

```bash
git clone <repository-url>
cd AI-Personal-Assistant
```

**2. 安装依赖**

```bash
pip install -r requirements.txt

# 如果需要使用微信适配器，根据微信版本选择安装：
# 微信 3.9+:
pip install pywechat127==1.9.7

# 微信 4.1+:
pip install git+https://github.com/Hello-Mr-Crab/pywechat.git
```

**3. 配置文件**

```bash
# 复制示例配置文件
cp config.example.yaml config.yaml

# 编辑配置文件，修改 AI 服务地址
# Windows: notepad config.yaml
```

**最小配置示例：**
```yaml
ai:
  primary:
    base_url: "http://localhost:8000"  # 修改为你的 AI 服务地址
    model: "gpt-4"                      # 修改为你的模型名称
```

**4. 启动程序**

```bash
# 方式 1: 使用 run.py 脚本（推荐）
python run.py

# 方式 2: 直接运行模块
python -m ai_assistant.main
```

看到以下输出表示启动成功：
```
AI Auto-Reply Assistant Starting...
Assistant is running. Press Ctrl+C to stop.
```

**5. 测试使用**

1. **打开飞书**
2. **在聊天窗口中选中一条包含【ai】的消息**，例如：
   ```
   【ai】你好，请介绍一下自己
   ```
3. **按 Ctrl+C 复制消息**
4. **等待几秒**，程序会调用 AI 生成回复
5. **看到通知**："🔔 AI 回复已复制到剪贴板"
6. **在飞书输入框按 Ctrl+V 粘贴**，然后发送

**停止程序：** 按 `Ctrl+C`

---

## 系统要求

- **操作系统**：Windows 10/11
- **Python**：3.10 或更高版本
- **AI 服务**：CherryStudio 本地服务或 OpenAI 兼容 API

## 详细配置说明

```yaml
# 触发规则
trigger:
  keyword: "【ai】"          # 触发关键词
  check_mention: true       # 检查 @提及
  check_private: true       # 检查私聊消息

# 上下文策略
context:
  mode: "short"             # 上下文模式：short | smart
  max_messages: 10          # 最大消息历史数量
  session_timeout: 3600     # 会话超时时间（秒）

# AI 配置
ai:
  primary:
    provider: "openai"  # AI 提供商: "openai" (推荐) 或 "cherrystudio"
    base_url: "http://localhost:8000"
    api_key: ""
    model: "gpt-4-vision-preview"
  timeout: 30
  multimodal: false         # 是否启用多模态

# 回复执行
reply:
  mode: "clipboard"         # 回复模式：clipboard | auto_input
  notification: true        # 是否显示通知

# IM 适配器
adapters:
  - name: "feishu"
    enabled: true
    priority: 1
```

## 详细配置说明

⚠️ **重要**：首次使用前，请先复制 `config.example.yaml` 为 `config.yaml`，然后修改配置。`config.yaml` 已被 Git 忽略，不会提交到仓库，保护您的 API 密钥安全。

**完整配置示例：**

```yaml
# 触发规则
trigger:
  keyword: "【ai】"          # 触发关键词
  check_mention: true       # 检查 @提及
  check_private: true       # 检查私聊消息

# 上下文策略
context:
  mode: "short"             # 上下文模式：short | smart
  max_messages: 10          # 最大消息历史数量
  session_timeout: 3600     # 会话超时时间（秒）

# AI 配置
ai:
  primary:
    provider: "openai"  # AI 提供商: "openai" (推荐) 或 "cherrystudio"
    base_url: "http://localhost:8000"
    api_key: ""
    model: "gpt-4-vision-preview"
  timeout: 30
  multimodal: false         # 是否启用多模态

# 回复执行
reply:
  mode: "clipboard"         # 回复模式：clipboard | auto_input
  notification: true        # 是否显示通知

# IM 适配器
adapters:
  - name: "feishu"
    enabled: true
    priority: 1

# 日志
logging:
  level: "INFO"
  file: "logs/ai-assistant.log"
  rotation: "daily"
  retention: 7
```

### 配置项说明

### 配置项说明

**触发规则：**

- `keyword`：触发关键词，默认为 "【ai】"
- `check_mention`：是否检查 @提及消息
- `check_private`：是否检查私聊消息

**上下文策略：**

- `mode`：
  - `short`：保留最近 N 条消息
  - `smart`：智能判断相关消息（计划中）
- `max_messages`：最大消息历史数量
- `session_timeout`：会话超时时间（秒）

**AI 配置：**

- `provider`：AI 服务提供商
  - `openai`：OpenAI 兼容接口（推荐，支持所有兼容服务）
  - `cherrystudio`：CherryStudio 专用（已废弃，建议使用 openai）
- `base_url`：API 基础 URL
  - OpenAI 官方：`https://api.openai.com`
  - CherryStudio：`http://localhost:23333`
  - Ollama：`http://localhost:11434`
  - LM Studio：`http://localhost:1234`
  - 其他兼容服务：根据实际情况填写
- `api_key`：API 密钥
  - OpenAI 官方：必需，从 OpenAI 官网获取
  - 本地服务（CherryStudio、Ollama、LM Studio）：留空即可
- `model`：使用的模型名称
  - OpenAI：`gpt-4`, `gpt-3.5-turbo` 等
  - CherryStudio：根据配置的模型填写
  - Ollama：`llama2`, `mistral` 等
  - LM Studio：根据加载的模型填写
- `timeout`：API 调用超时时间（秒）
- `multimodal`：是否启用多模态（图片、视频，计划中）

**支持的 AI 服务：**

| 服务 | Provider | Base URL | API Key |
|------|----------|----------|---------|
| OpenAI 官方 | openai | https://api.openai.com | 必需 |
| CherryStudio | openai | http://localhost:23333 | 留空 |
| Ollama | openai | http://localhost:11434 | 留空 |
| LM Studio | openai | http://localhost:1234 | 留空 |
| Azure OpenAI | openai | 自定义 | 必需 |
| 其他兼容服务 | openai | 自定义 | 根据服务要求 |

**回复模式：**

- `clipboard`：复制到剪贴板，用户手动粘贴（推荐）
- `auto_input`：自动输入并发送（计划中，需谨慎使用）

**系统配置：**

- `poll_interval`：UI 轮询间隔（秒），默认 5.0
  - 控制程序检查新消息的频率
  - 设置过小会频繁轮询，可能影响用户操作
  - 设置过大会导致响应延迟
  - 推荐值：3.0 - 10.0 秒

---

## 使用场景

**示例场景：**

1. **工作协作**：在飞书群聊中，同事 @你 并发送 "【ai】帮我总结一下这个文档"
   - 复制这条消息（Ctrl+C）
   - 助手检测到触发词，调用 AI 生成回复
   - 回复自动复制到剪贴板
   - 在输入框粘贴（Ctrl+V）并发送

2. **客户服务**：客户私聊发送 "【ai】这个功能怎么使用？"
   - 同样的流程：复制 → 等待 → 粘贴

3. **个人助理**：朋友发送 "【ai】推荐几个周末去的地方"
   - 复制消息触发 AI 回复

**工作流程：**
```
用户复制消息 → 助手检测触发词 → 提取上下文 → 调用 AI →
回复复制到剪贴板 → 用户粘贴发送
```

**注意事项：**
- 当前飞书适配器是简化实现，需要手动复制消息来触发
- 完整的自动化实现需要更复杂的 UI 元素定位逻辑
- 确保 CherryStudio 服务正常运行，否则会报错

---

## 适配器使用指南

### 飞书适配器

飞书支持两种模式：

#### 1. UI 自动化模式（默认）

**优点：** 无需审批，本地运行
**缺点：** 需要手动复制消息

**配置：**
```yaml
adapters:
  - name: "feishu"
    enabled: true
    mode: "ui_automation"
    ui_automation:
      window_titles: ["飞书", "Lark", "Feishu"]
      poll_interval: 1.0
```

**使用步骤：**
1. 打开飞书
2. 选中包含【ai】的消息
3. 按 Ctrl+C 复制
4. 等待 AI 生成回复
5. 按 Ctrl+V 粘贴并发送

#### 2. 机器人 API 模式

**优点：** 全自动，无需手动操作
**缺点：** 需要创建飞书应用并获得审批

**配置：**
```yaml
adapters:
  - name: "feishu"
    enabled: true
    mode: "bot_api"
    bot_api:
      app_id: "your_app_id"
      app_secret: "your_app_secret"
      verification_token: "your_token"
      allowed_chats: []  # 白名单，为空则允许所有
```

**设置步骤：**
1. 访问飞书开放平台创建应用
2. 配置事件订阅和权限
3. 填写配置信息
4. 启动程序（会自动启动 webhook 服务器）

---

### 微信适配器

基于 [pywechat](https://github.com/Hello-Mr-Crab/pywechat) 实现，使用 UI 自动化操作微信客户端。

**支持版本：**
- ✅ 微信 3.9+ 系列（推荐）
- ✅ 微信 4.1+ 系列

**特点：**
- ✅ 安全：操作真实客户端，不破解协议，封号风险低
- ✅ 功能完善：支持消息收发、聊天监控、自动回复
- ✅ 自动监听：无需手动复制消息（3.9 版本）

**前提条件：**
1. Windows 10/11 系统
2. 已安装微信客户端
3. Python 3.9+

**安装（根据微信版本选择）：**

**微信 3.9+ 版本（推荐）：**
```bash
pip install pywechat127==1.9.7
```

**微信 4.1+ 版本：**
```bash
pip install git+https://github.com/Hello-Mr-Crab/pywechat.git
```

**配置：**
```yaml
adapters:
  - name: "wechat"
    enabled: true
    priority: 2
    wechat_version: "3.9"  # 微信版本: "3.9" 或 "4.1"
    poll_interval: 1.0
    monitored_chats: ["好友名", "群名"]  # 监控的聊天列表
```

**版本差异：**

| 特性 | 微信 3.9 | 微信 4.1 |
|------|---------|---------|
| 自动监听 | ✅ 支持 | ⚠️ 有兼容性问题 |
| 手动复制模式 | ✅ 支持 | ✅ 支持 |
| 系统要求 | 32位 Windows 7/10 | Windows 10/11 |
| 稳定性 | ⭐⭐⭐⭐⭐ | ⭐⭐⭐ |

**使用模式：**

**模式 1：自动监听指定聊天（推荐 - 仅 3.9 版本）**
```yaml
wechat_version: "3.9"
monitored_chats: ["张三", "工作群", "项目讨论组"]
```
- 自动监听指定聊天的新消息
- 检测到触发词后自动回复
- 无需手动操作
- 适合长期使用

**模式 2：监听所有聊天（3.9 版本）**
```yaml
wechat_version: "3.9"
monitored_chats: []  # 留空
```
- 自动获取所有聊天的最新消息
- 检测到触发词后自动回复
- 适合临时使用

**模式 3：手动复制模式（4.1 版本或备用）**
```yaml
wechat_version: "4.1"
monitored_chats: []  # 留空
```
- 需要手动复制包含触发词的消息
- 适合 4.1 版本或作为备用方案

**使用步骤：**
1. 启动微信客户端并登录
2. 启动 AI 助手程序
3. 在微信中接收包含触发关键词的消息（如 "ai，你好"）
4. 程序自动检测并回复（3.9 版本）或手动复制触发（4.1 版本）

**注意事项：**
- 确保微信客户端保持运行
- 3.9 版本推荐用于生产环境（更稳定）
- 监听指定聊天时，聊天名称需要完全匹配
- 首次使用建议先测试单个聊天

---

## 常见问题

### Q: 提示 "AI service health check failed"

**A:** 检查 CherryStudio 是否正在运行，配置文件中的 `base_url` 是否正确。

### Q: 没有检测到触发

**A:** 确保：
- 飞书窗口是当前活动窗口
- 消息中包含【ai】关键词
- 已经用 Ctrl+C 复制了消息

### Q: 如何修改触发关键词？

**A:** 编辑 `config.yaml`：
```yaml
trigger:
  keyword: "【ai】"  # 改成你想要的关键词
```

### Q: 如何停止程序？

**A:** 在终端按 `Ctrl+C`

### Q: 为什么需要无障碍权限？

**A:** 本工具通过 Windows UI 自动化 API 读取 IM 窗口内容，需要无障碍权限才能访问窗口元素。

### Q: 会不会影响正常使用 IM 工具？

**A:** 不会。本工具采用非侵入式设计，仅读取窗口内容，不修改 IM 客户端，您可以正常使用。

### Q: 支持哪些 AI 模型？

**A:** 目前支持 CherryStudio 本地服务和任何 OpenAI 兼容的 API，包括 GPT-4、Claude、本地部署的开源模型等。

### Q: 消息会被上传到云端吗？

**A:** 不会。所有消息仅在本地处理，AI API 调用使用 HTTPS 加密，不会上传到第三方服务器（除了您配置的 AI 服务）。

### Q: 如何关闭自动回复？

**A:** 直接关闭本程序即可，或在配置文件中禁用相应的适配器。

### Q: 微信适配器提示 "pyweixin not installed" 或 "pywechat 未安装"

**A:** 根据你的微信版本安装对应的库：

**微信 3.9+：**
```bash
pip install pywechat127==1.9.7
```

**微信 4.1+：**
```bash
pip install git+https://github.com/Hello-Mr-Crab/pywechat.git
```

然后在配置文件中设置对应的版本：
```yaml
wechat_version: "3.9"  # 或 "4.1"
```

### Q: 如何查看我的微信版本？

**A:** 打开微信 → 左下角三条横线 → 设置 → 关于微信，查看版本号。

### Q: 微信适配器无法检测到消息

**A:** 检查：
- 微信客户端是否正在运行并已登录
- 配置文件中 `wechat_version` 是否与实际微信版本匹配
- 配置文件中 `monitored_chats` 聊天名称是否完全匹配（区分大小写）
- 触发关键词是否正确（默认为 "ai"）
- 如果使用 4.1 版本，尝试切换到 3.9 版本

### Q: 微信 3.9 和 4.1 版本应该选哪个？

**A:**
- **推荐使用 3.9 版本**：更稳定，支持自动监听，无需手动复制
- 如果你的微信是 4.1 版本且不想降级，可以使用 4.1 适配器，但可能需要手动复制消息
- 3.9 版本仅支持 32 位 Windows 7/10

### Q: 微信会不会封号？

**A:** pywechat 使用 UI 自动化方式操作真实微信客户端，不涉及协议破解，封号风险极低。但建议：
- 不要频繁发送大量消息
- 不要用于营销或骚扰行为
- 遵守微信使用规范

### Q: 监听指定聊天时，聊天名称怎么填？

**A:**
- 好友：填写好友的微信昵称（不是备注名）
- 群聊：填写群聊名称
- 名称必须完全匹配，区分大小写
- 示例：`monitored_chats: ["张三", "工作群", "项目讨论组"]`

---

## 开发指南

### 项目结构

```
AI-Personal-Assistant/
├── src/
│   └── ai_assistant/
│       ├── core/           # 核心模块
│       │   ├── models.py   # 数据模型
│       │   ├── config.py   # 配置管理
│       │   └── context_manager.py  # 上下文管理
│       ├── adapters/       # IM 适配器
│       │   └── feishu.py   # 飞书适配器（开发中）
│       └── utils/          # 工具函数
├── tests/
│   ├── unit/              # 单元测试
│   └── integration/       # 集成测试
├── docs/
│   └── plans/             # 设计文档和实施计划
├── config.yaml            # 配置文件
└── requirements.txt       # 依赖列表
```

### 运行测试

```bash
# 运行所有测试
PYTHONPATH=src pytest tests/ -v

# 运行单元测试
PYTHONPATH=src pytest tests/unit/ -v

# 运行特定测试文件
PYTHONPATH=src pytest tests/unit/test_models.py -v

# Windows PowerShell 用户
$env:PYTHONPATH="src"; pytest tests/unit/ -v
```

**测试覆盖：**
- ✅ 数据模型测试（4 个测试）
- ✅ 配置管理测试（2 个测试）
- ✅ 上下文管理测试（4 个测试）
- ✅ AI Provider 测试（2 个测试）
- ✅ 回复执行器测试（2 个测试）
- ✅ 飞书适配器测试（3 个测试）

**总计：17 个单元测试全部通过 ✅**

### 开发计划

**第一阶段（MVP）** ✅ 已完成：
- [x] 项目初始化
- [x] 数据模型定义
- [x] 配置管理模块
- [x] 上下文管理器
- [x] AI 集成模块（CherryStudio）
- [x] 飞书适配器（简化版）
- [x] 回复执行模块
- [x] 主程序循环

**第二阶段**（改进）：
- [ ] 完整的飞书 UI 自动化（自动提取消息，无需手动复制）
- [ ] 图片多模态支持
- [ ] OpenAI 兼容接口
- [ ] 智能上下文判断

**第三阶段**（扩展）：
- [ ] 微信适配器
- [ ] 视频处理支持
- [ ] 自动输入模式
- [ ] GUI 配置界面

## 技术架构

### 核心组件

1. **消息监听模块**：通过 pywinauto 监听 IM 窗口
2. **上下文管理模块**：维护会话历史和消息缓存
3. **AI 集成模块**：调用 AI 模型生成回复
4. **回复执行模块**：将回复发送到 IM 窗口

### 技术栈

- **UI 自动化**：pywinauto, pyautogui
- **图像处理**：Pillow
- **HTTP 客户端**：requests
- **配置解析**：pyyaml
- **日志管理**：loguru
- **测试框架**：pytest

---

## 故障排查

**查看日志：**
```bash
# 日志文件位置
tail -f logs/ai-assistant.log

# Windows
type logs\ai-assistant.log
```

**运行测试：**
```bash
# 运行所有测试
PYTHONPATH=src pytest tests/unit/ -v

# Windows PowerShell
$env:PYTHONPATH="src"; pytest tests/unit/ -v
```

**常见错误：**
1. `ModuleNotFoundError` - 检查是否安装了所有依赖
2. `FileNotFoundError: config.yaml` - 需要先复制配置文件
3. `Connection refused` - 检查 AI 服务是否运行

---

## 许可证

待定

## 贡献

欢迎提交 Issue 和 Pull Request！

## 联系方式

如有问题或建议，请通过 Issue 反馈。

---

**注意**：本项目仅供学习和个人使用，请遵守相关 IM 工具的使用条款。

## 飞书双模式说明

项目支持两种飞书适配器模式，可通过配置文件切换：

### 模式 1：机器人 API（推荐）

**适用场景：**
- 个人飞书账号（容易创建应用）
- 能获得企业管理员审批的情况

**优势：**
- ✅ 官方 API，稳定可靠
- ✅ 事件驱动，实时响应
- ✅ 支持白名单控制（限制特定群聊/用户）
- ✅ 无需手动操作

**配置示例：**
\`\`\`yaml
adapters:
  - name: "feishu"
    enabled: true
    mode: "bot_api"
    bot_api:
      app_id: "cli_xxx"
      app_secret: "xxx"
      verification_token: "xxx"
      allowed_chats: ["oc_xxx"]  # 可选：白名单
\`\`\`

**使用步骤：**
1. 在飞书开放平台创建应用
2. 配置事件订阅 webhook URL
3. 获取 App ID 和 App Secret
4. 填入配置文件
5. 运行程序（自动启动 webhook 服务器）

---

### 模式 2：UI 自动化（备选）

**适用场景：**
- 企业环境无法获得审批
- 不想创建飞书应用
- 快速测试验证

**优势：**
- ✅ 无需任何审批
- ✅ 本地运行，完全可控
- ✅ 立即可用

**限制：**
- ⚠️ 当前需要手动复制消息触发
- ⚠️ 飞书更新可能需要适配

**配置示例：**
\`\`\`yaml
adapters:
  - name: "feishu"
    enabled: true
    mode: "ui_automation"
    ui_automation:
      window_titles: ["飞书", "Lark"]
      poll_interval: 1.0
\`\`\`

**使用步骤：**
1. 配置为 ui_automation 模式
2. 运行程序
3. 在飞书中复制包含【ai】的消息
4. 程序自动检测并回复

---

**模式切换：** 只需修改配置文件中的 `mode` 参数即可。
