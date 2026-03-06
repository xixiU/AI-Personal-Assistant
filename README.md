# AI 自动回复助手

一个基于大模型的 IM 工具自动回复系统，支持微信和飞书等即时通讯工具。

## 项目简介

本项目通过 Windows UI 自动化技术监听 IM 客户端窗口，当检测到特定触发词时，自动调用 AI 模型生成回复并复制到剪贴板，实现智能自动回复功能。

### 核心特性

- 🤖 **智能回复**：集成 CherryStudio 本地 AI 模型，支持上下文理解
- 💬 **多平台支持**：支持飞书、微信等主流 IM 工具（优先实现飞书）
- 🎯 **灵活触发**：通过关键词【ai】触发，支持 @提及和私聊场景
- 📝 **上下文管理**：自动维护短期消息历史，提供更准确的回复
- 🔒 **非侵入式**：通过 UI 自动化实现，不修改 IM 客户端，用户可正常使用
- 🎨 **多模态支持**：计划支持文本、图片、视频等多种消息类型

## 开发状态

⚠️ **当前处于 MVP 开发阶段**

已完成：
- ✅ 项目基础架构
- ✅ 数据模型定义（Content, Message, Session）
- ✅ 配置管理模块

进行中：
- 🚧 上下文管理器
- 🚧 AI 集成模块
- 🚧 飞书适配器
- 🚧 回复执行模块

## 系统要求

- **操作系统**：Windows 10/11
- **Python**：3.10 或更高版本
- **AI 服务**：CherryStudio 本地服务或 OpenAI 兼容 API

## 安装步骤

### 1. 克隆项目

```bash
git clone <repository-url>
cd AI-Personal-Assistant
```

### 2. 安装依赖

```bash
pip install -r requirements.txt
```

### 3. 配置文件

复制 `config.yaml` 并根据需要修改配置：

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
    provider: "cherrystudio"
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

## 使用方法

### 启动 AI 服务

在使用本工具前，需要先启动 CherryStudio 或其他 OpenAI 兼容的 AI 服务。

**CherryStudio 启动示例：**
```bash
# 确保 CherryStudio 在 http://localhost:8000 运行
# 具体启动方式请参考 CherryStudio 文档
```

### 运行助手（开发中）

⚠️ **注意：主程序尚未完成，以下为计划中的使用方式**

```bash
# 启动 AI 自动回复助手
python -m ai_assistant.main
```

### 使用场景

1. **工作协作**：在飞书群聊中，同事 @你 并发送 "【ai】帮我总结一下这个文档"
2. **客户服务**：客户私聊发送 "【ai】这个功能怎么使用？"
3. **个人助理**：朋友发送 "【ai】推荐几个周末去的地方"

助手会自动：
1. 检测到触发关键词【ai】
2. 提取最近 10 条消息作为上下文
3. 调用 AI 模型生成回复
4. 将回复复制到剪贴板
5. 显示系统通知
6. 您按 Ctrl+V 粘贴并发送

## 配置说明

### 触发规则

- `keyword`：触发关键词，默认为 "【ai】"
- `check_mention`：是否检查 @提及消息
- `check_private`：是否检查私聊消息

### 上下文策略

- `mode`：
  - `short`：保留最近 N 条消息
  - `smart`：智能判断相关消息（计划中）
- `max_messages`：最大消息历史数量
- `session_timeout`：会话超时时间（秒）

### AI 配置

- `provider`：AI 服务提供商（cherrystudio | openai）
- `base_url`：API 基础 URL
- `api_key`：API 密钥（如需要）
- `model`：使用的模型名称
- `timeout`：API 调用超时时间
- `multimodal`：是否启用多模态（图片、视频）

### 回复模式

- `clipboard`：复制到剪贴板，用户手动粘贴（推荐）
- `auto_input`：自动输入并发送（计划中，需谨慎使用）

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
```

### 开发计划

**第一阶段（MVP）**：
- [x] 项目初始化
- [x] 数据模型定义
- [x] 配置管理模块
- [ ] 上下文管理器
- [ ] AI 集成模块（CherryStudio）
- [ ] 飞书适配器
- [ ] 回复执行模块
- [ ] 主程序循环

**第二阶段**：
- [ ] 图片多模态支持
- [ ] OpenAI 兼容接口
- [ ] 智能上下文判断

**第三阶段**：
- [ ] 微信适配器
- [ ] 视频处理支持
- [ ] 自动输入模式

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

## 常见问题

### Q: 为什么需要无障碍权限？

A: 本工具通过 Windows UI 自动化 API 读取 IM 窗口内容，需要无障碍权限才能访问窗口元素。

### Q: 会不会影响正常使用 IM 工具？

A: 不会。本工具采用非侵入式设计，仅读取窗口内容，不修改 IM 客户端，您可以正常使用。

### Q: 支持哪些 AI 模型？

A: 目前支持 CherryStudio 本地服务和任何 OpenAI 兼容的 API，包括 GPT-4、Claude、本地部署的开源模型等。

### Q: 消息会被上传到云端吗？

A: 不会。所有消息仅在本地处理，AI API 调用使用 HTTPS 加密，不会上传到第三方服务器（除了您配置的 AI 服务）。

### Q: 如何关闭自动回复？

A: 直接关闭本程序即可，或在配置文件中禁用相应的适配器。

## 许可证

待定

## 贡献

欢迎提交 Issue 和 Pull Request！

## 联系方式

如有问题或建议，请通过 Issue 反馈。

---

**注意**：本项目仅供学习和个人使用，请遵守相关 IM 工具的使用条款。
