# AI 自动回复助手

一个基于大模型的 IM 工具自动回复系统，支持微信和飞书等即时通讯工具。

## 项目简介

本项目通过飞书机器人 API 和微信 UI 自动化，监听 IM 消息，当检测到特定触发词时，自动调用 AI 模型生成回复并发送，实现智能自动回复功能。

### 核心特性

- 🤖 **智能回复**：集成 Dify、OpenAI 等 AI 服务，支持上下文理解
- 💬 **多平台支持**：支持飞书、微信等主流 IM 工具
- 🎯 **灵活触发**：通过关键词【ai】触发，支持 @提及和私聊场景
- 📝 **上下文管理**：自动维护短期消息历史，提供更准确的回复
- 🔄 **飞书机器人**：基于飞书开放平台 API，事件驱动，实时响应
- ⚡ **高并发处理**：事件队列 + 线程池架构，支持多用户同时使用
- 🔒 **非侵入式**：通过官方 API 或 UI 自动化实现，不修改 IM 客户端
- 🎨 **多模态支持**：计划支持文本、图片、视频等多种消息类型

## 最近更新

### v0.2.0 (2024-01-XX) - 性能优化与并发支持

**重大改进：**
- ✅ **并发架构重构**：实现事件队列 + 线程池模式，支持多用户同时使用
- ✅ **线程安全优化**：
  - 使用 `RLock` 替代 `Lock`，避免死锁
  - 重构事件处理流程，消除共享状态竞争
  - 为 Dify Provider 添加线程安全的 conversation_id 管理
- ✅ **性能监控**：添加详细的性能日志，追踪 AI 响应时间和总处理时间
- ✅ **配置增强**：
  - 新增 `system.event_queue_size` 配置（默认 100）
  - 新增 `system.max_concurrent_workers` 配置（默认 5）
- ✅ **AI Provider 改进**：
  - 所有 Provider 的 `send_message` 方法支持 `session_id` 参数
  - 支持多用户并发对话，每个用户独立维护上下文

**性能提升：**
- 解决了线程池饱和导致的延迟问题
- 增加默认并发数，提升多用户场景下的响应速度
- 添加队列大小监控，避免事件丢失

**配置示例：**
```yaml
system:
  event_queue_size: 200  # 事件队列大小
  max_concurrent_workers: 10  # 并发工作线程数
```

**日志示例：**
```
⏱️  Event processing started (queue_size=3)
⏱️  AI reply received in 2.35s: ...
⏱️  Total processing time: 2.58s (AI: 2.35s, Send: 0.23s)
✅ Event enqueued, queue size: 3/200
```

## 开发状态

✅ **MVP 阶段已完成！程序现在可以运行了。**

已完成：
- ✅ 项目基础架构
- ✅ 数据模型定义（Content, Message, Session）
- ✅ 配置管理模块
- ✅ 上下文管理器
- ✅ AI 集成模块（CherryStudio）
- ✅ 飞书适配器 - 机器人 API 模式（官方 API，事件驱动）
- ✅ 微信适配器（基于 pywechat）
- ✅ 回复执行模块
- ✅ 主程序循环
- ✅ Webhook 服务器（用于飞书机器人模式）

待改进：
- 🚧 图片多模态支持
- 🚧 智能上下文判断

## 快速开始

### 前提条件

1. **Python 3.10+** 已安装
2. **AI 服务**：Dify 或其他 OpenAI 兼容的 AI 服务正在运行

### 安装步骤

**1. 克隆项目**

```bash
git clone <repository-url>
cd AI-Personal-Assistant
```

**2. 安装依赖**

```bash
# 使用 uv（推荐）
uv sync

# 或使用 pip
pip install -r requirements.txt

# 如果需要使用微信适配器，根据微信版本选择安装：
# 微信 3.9+:
pip install pywechat127==1.9.7

# 微信 4.1+:
pip install git+https://github.com/Hello-Mr-Crab/pywechat.git
```

**GPU 加速（可选）**

如果你有 NVIDIA GPU 并希望加速文档检索的 Embedding 生成：

```bash
# 1. 确保已安装 CUDA 11.8+ 和 cuDNN
nvcc --version
nvidia-smi

# 2. 卸载 CPU 版本，安装 GPU 版本
pip uninstall onnxruntime
pip install onnxruntime-gpu

# 或使用 uv
uv pip uninstall onnxruntime
uv pip install onnxruntime-gpu

# 3. 验证 CUDA 可用
python -c "import onnxruntime as ort; print(ort.get_available_providers())"
# 应该看到 'CUDAExecutionProvider'

# 4. 在 config.yaml 中启用 GPU
vector_db:
  use_gpu: true
  gpu_id: 0  # 使用第一张 GPU（0 或 1）
```

**注意**：
- `onnxruntime` (CPU) 和 `onnxruntime-gpu` (GPU) 是互斥的，只能安装一个
- GPU 加速主要提升文档索引和检索速度（5-10x），对于文档数量 > 500 篇时效果明显
- 如果 GPU 不可用，会自动 fallback 到 CPU 模式

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
# 开发环境：前台运行（推荐用于测试）
python run.py

# 生产环境：后台运行（推荐用于服务器部署）
./service.sh start

# 调试模式：前台运行，输出详细信息（用于排查问题）
./service.sh debug

# 其他服务管理命令
./service.sh status   # 查看服务状态
./service.sh stop     # 停止服务
./service.sh restart  # 重启服务
./service.sh monitor  # 监控并自动重启（前台运行）
```

看到以下输出表示启动成功：
```
AI Auto-Reply Assistant Starting...
Assistant is running. Press Ctrl+C to stop.
```

**5. 测试使用（飞书机器人模式）**

1. 在飞书中向机器人发送私聊消息，或在已添加机器人的群聊中发送消息
2. 消息中包含触发关键词，例如：
   ```
   【ai】你好，请介绍一下自己
   ```
3. 机器人自动回复 AI 生成的内容

**停止程序：** 按 `Ctrl+C`

---

## 系统要求

- **操作系统**：Windows 10/11 / Linux
- **Python**：3.10 或更高版本
- **AI 服务**：Dify 平台或 OpenAI 兼容 API
- **GPU（可选）**：NVIDIA GPU + CUDA 11.8+ （用于加速文档检索）

## 性能优化

### CPU 模式（默认）
- 适合文档数量 < 500 篇的场景
- 已优化线程数，避免 CPU 占用过高
- 无需额外配置

### GPU 模式（推荐用于大规模文档）
- 适合文档数量 > 500 篇或高频查询场景
- Embedding 生成速度提升 5-10x
- 需要安装 `onnxruntime-gpu` 和 CUDA

**性能对比**：

| 场景 | CPU (2 线程) | GPU (单卡) | 加速比 |
|------|-------------|-----------|--------|
| 索引 1000 篇文档 | ~120s | ~15s | 8x |
| 单次查询 | ~0.5s | ~0.1s | 5x |

**GPU 配置**：
```yaml
# config.yaml
vector_db:
  use_gpu: true   # 启用 GPU 加速
  gpu_id: 0       # 使用第一张 GPU（0 或 1）
```

如果有多张 GPU，可以通过环境变量指定：
```bash
export CUDA_VISIBLE_DEVICES=1  # 只使用 GPU 1
./service.sh start
```

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
  - `dify`：Dify 平台（LLM 应用开发平台）
- `base_url`：API 基础 URL
  - OpenAI 官方：`https://api.openai.com`
  - Dify：`https://api.dify.ai/v1` 或自部署地址
  - Ollama：`http://localhost:11434`
  - LM Studio：`http://localhost:1234`
  - 其他兼容服务：根据实际情况填写
- `api_key`：API 密钥
  - OpenAI 官方：必需，从 OpenAI 官网获取
  - Dify：必需，从 Dify 应用设置中获取
  - 本地服务（Ollama、LM Studio）：留空即可
- `model`：使用的模型名称（OpenAI 兼容接口需要）
  - OpenAI：`gpt-4`, `gpt-3.5-turbo` 等
  - Ollama：`llama2`, `mistral` 等
  - LM Studio：根据加载的模型填写
  - Dify：不需要，由 Dify 应用配置决定
- `timeout`：API 调用超时时间（秒）
- `multimodal`：是否启用多模态（图片、视频，计划中）

**Dify 特定配置：**

- `app_type`：应用类型
  - `chat`：对话型应用（推荐，支持多轮对话）
  - `completion`：完成型应用（单次完成）
- `user`：用户标识，用于区分不同用户（默认：`default-user`）

**支持的 AI 服务：**

| 服务 | Provider | Base URL | API Key | Model |
|------|----------|----------|---------|-------|
| OpenAI 官方 | openai | https://api.openai.com | 必需 | 必需 |
| Dify 平台 | dify | https://api.dify.ai/v1 | 必需 | 不需要 |
| Ollama | openai | http://localhost:11434 | 留空 | 必需 |
| LM Studio | openai | http://localhost:1234 | 留空 | 必需 |
| Azure OpenAI | openai | 自定义 | 必需 | 必需 |
| 其他兼容服务 | openai | 自定义 | 根据服务要求 | 必需 |

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

1. **工作协作**：在飞书群聊中，同事发送 "【ai】帮我总结一下这个文档"
   - 机器人检测到触发词，调用 AI 生成回复
   - 自动回复到原消息线程

2. **客户服务**：客户私聊发送 "【ai】这个功能怎么使用？"
   - 机器人自动回复

3. **个人助理**：朋友发送 "【ai】推荐几个周末去的地方"
   - 机器人自动回复

**工作流程：**
```
用户发送消息 → Webhook 接收事件 → 检测触发词 → 调用 AI → 回复原消息
```

**注意事项：**
- 飞书机器人需要在开放平台创建应用并配置 Webhook
- 群聊中需要将机器人添加到群组

---

## 适配器使用指南

### 飞书适配器

#### 机器人 API 模式

**优点：** 全自动，无需手动操作
**缺点：** 需要创建飞书应用并获得审批

**配置：**
```yaml
adapters:
  - name: "feishu"
    enabled: true
    bot_api:
      app_id: "your_app_id"
      app_secret: "your_app_secret"
      verification_token: "your_token"
      allowed_chats: []  # 白名单，为空则允许所有
```

**设置步骤：**
1. 访问飞书开放平台创建应用
2. 配置事件订阅和权限（见下方权限清单）
3. 填写配置信息
4. 启动程序（会自动启动 webhook 服务器）

**飞书机器人所需权限清单：**

在 [飞书开放平台](https://open.feishu.cn/app) 创建应用后，需申请以下权限：

| 权限标识 | 权限名称 | 用途 |
|---------|---------|------|
| `im:message` | 获取与发送单聊、群组消息 | **核心权限**：向用户/群聊发送消息、回复消息 |
| `im:message.p2p_msg:readonly` | 读取用户发给机器人的单聊消息 | 接收私聊触发消息 |
| `im:message.group_at_msg:readonly` | 接收群聊中@机器人消息事件 | 接收群聊触发消息 |

**事件订阅（在"事件订阅"页面配置）：**

| 事件 | 说明 |
|------|------|
| `im.message.receive_v1` | 接收消息 |

**机器人能力：**
- 在"应用功能 → 机器人"中开启机器人能力

**Webhook 配置：**
- 程序启动后会在本地 `8080` 端口监听飞书事件
- 需要在**飞书开放平台 → 事件订阅**中填写回调地址：`https://your-domain.com/webhook/feishu`
- **飞书要求 webhook 必须使用 HTTPS**，配置方式：

**开发测试环境（使用 ngrok）：**
```bash
# 安装 ngrok: https://ngrok.com/download
ngrok http 8080
```
ngrok 会生成一个 HTTPS 地址（如 `https://abc123.ngrok.io`），将 `https://abc123.ngrok.io/webhook/feishu` 填入飞书事件订阅。

**生产环境（使用 Nginx + SSL 证书）：**

1. **获取 SSL 证书**（推荐 Let's Encrypt 免费证书）：
```bash
# 安装 certbot
sudo apt install certbot python3-certbot-nginx

# 申请证书（自动配置 Nginx）
sudo certbot --nginx -d your-domain.com
```

2. **配置 Nginx 反向代理**（`/etc/nginx/sites-available/feishu-webhook`）：
```nginx
server {
    listen 443 ssl http2;
    server_name your-domain.com;

    # SSL 证书（certbot 会自动配置）
    ssl_certificate /etc/letsencrypt/live/your-domain.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/your-domain.com/privkey.pem;

    # 飞书 webhook 路由
    location /webhook/feishu {
        proxy_pass http://127.0.0.1:8080;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}

# HTTP 自动跳转 HTTPS
server {
    listen 80;
    server_name your-domain.com;
    return 301 https://$server_name$request_uri;
}
```

3. **启用配置并重启 Nginx**：
```bash
sudo ln -s /etc/nginx/sites-available/feishu-webhook /etc/nginx/sites-enabled/
sudo nginx -t  # 测试配置
sudo systemctl reload nginx
```

4. **在飞书开放平台填写**：`https://your-domain.com/webhook/feishu`

**证书自动续期**：
```bash
# certbot 会自动添加 cron 任务，也可以手动测试续期
sudo certbot renew --dry-run
```

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
- **智能轮询**：程序只在微信窗口可见时进行轮询，窗口最小化或隐藏时自动停止，不影响正常使用电脑

---

## 常见问题

### Q: 提示 "AI service health check failed"

**A:** 检查 AI 服务（Dify/OpenAI 等）是否正在运行，配置文件中的 `base_url` 和 `api_key` 是否正确。

### Q: 没有检测到触发

**A:** 确保：
- 飞书机器人已正确配置 App ID、App Secret 和 Webhook URL
- 消息中包含【ai】关键词
- Webhook 服务器可被飞书服务器访问（需公网地址）

### Q: 如何修改触发关键词？

**A:** 编辑 `config.yaml`：
```yaml
trigger:
  keyword: "【ai】"  # 改成你想要的关键词
```

### Q: 如何停止程序？

**A:** 在终端按 `Ctrl+C`

### Q: 为什么需要无障碍权限？

**A:** 微信适配器通过 Windows UI 自动化 API 操作微信客户端，需要无障碍权限。飞书机器人模式不需要此权限。

### Q: 会不会影响正常使用 IM 工具？

**A:** 不会。本工具采用非侵入式设计，仅读取窗口内容，不修改 IM 客户端，您可以正常使用。

### Q: 支持哪些 AI 模型？

**A:** 支持 Dify 平台和任何 OpenAI 兼容的 API，包括 GPT-4、Claude、Ollama、LM Studio 等本地部署的开源模型。

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

1. **消息监听模块**：飞书通过 Webhook 接收事件，微信通过 pywechat 轮询
2. **上下文管理模块**：维护会话历史和消息缓存
3. **AI 集成模块**：调用 AI 模型生成回复
4. **回复执行模块**：将回复发送到 IM

### 技术栈

- **飞书集成**：飞书开放平台 API + Flask Webhook
- **微信自动化**：pywechat, pywinauto
- **AI 模型**：Anthropic Claude, OpenAI, Dify
- **文档检索**：ChromaDB (向量数据库) + BM25 (关键词匹配)
- **Embedding 模型**：text2vec-base-chinese (ONNX Runtime)
- **图像处理**：Pillow
- **HTTP 客户端**：requests
- **配置解析**：pyyaml
- **日志管理**：loguru
- **测试框架**：pytest
- **生产服务器**：waitress (WSGI)

---

## 故障排查

### 服务管理

**查看服务状态：**
```bash
./service.sh status
```

**查看日志：**
```bash
# 实时查看日志
tail -f logs/ai-assistant.log

# 查看 nohup 输出（后台运行时）
tail -f logs/nohup.out

# Windows
type logs\ai-assistant.log
```

**服务异常退出排查：**

如果服务经常自动退出，可以通过以下方式排查：

1. **使用调试模式（前台运行，看完整输出）**
   ```bash
   ./service.sh debug
   ```
   让它运行几分钟，观察是否有错误信息或异常退出

2. **查看进程状态和资源占用**
   ```bash
   ./service.sh status
   ```

3. **查看最近的日志**
   ```bash
   tail -100 logs/ai-assistant.log
   tail -100 logs/nohup.out
   ```

4. **检查系统日志（Linux）**
   ```bash
   # 检查是否被 OOM killer 杀掉
   dmesg | grep -i "killed process"
   
   # 检查内存使用
   free -h
   ```

5. **使用监控模式自动重启**
   ```bash
   # 前台运行监控（会自动重启退出的服务）
   ./service.sh monitor
   
   # 后台运行监控
   nohup ./service.sh monitor > logs/monitor.log 2>&1 &
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
4. 服务静默退出 - 查看日志，可能是内存不足或未捕获的异常
5. CPU 占用过高 - 如果启用了文档检索，考虑使用 GPU 加速或减少文档数量
6. `CUDA not available` - 检查 CUDA 安装和 onnxruntime-gpu 版本

---

## 许可证

待定

## 贡献

欢迎提交 Issue 和 Pull Request！

## 联系方式

如有问题或建议，请通过 Issue 反馈。

---

**注意**：本项目仅供学习和个人使用，请遵守相关 IM 工具的使用条款。

## 飞书机器人说明

**优势：**
- ✅ 官方 API，稳定可靠
- ✅ 事件驱动，实时响应
- ✅ 支持白名单控制（限制特定群聊/用户）
- ✅ 无需手动操作

**配置示例：**
```yaml
adapters:
  - name: "feishu"
    enabled: true
    bot_api:
      app_id: "cli_xxx"
      app_secret: "xxx"
      verification_token: "xxx"
      allowed_chats: ["oc_xxx"]  # 可选：白名单
```

**使用步骤：**
1. 在飞书开放平台创建应用
2. 配置事件订阅 webhook URL
3. 获取 App ID 和 App Secret
4. 填入配置文件
5. 运行程序（自动启动 webhook 服务器）

---
