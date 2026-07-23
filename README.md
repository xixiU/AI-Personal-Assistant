# AI 智能助手

基于大模型的智能 IM 自动回复助手，支持飞书、微信等 IM 工具，具备 RAG 知识库检索、Agentic 代码排查和 Web 聊天界面。

## 功能特性

### 核心能力
- **多 IM 平台支持**：飞书（Bot API + Webhook）、微信（UI 自动化）
- **智能回复**：基于 Claude/GPT/Dify 等大模型的上下文感知回复
- **RAG 知识库检索**：混合检索（向量语义 + BM25 关键词）+ AI 标题过滤
- **Agentic 代码排查**（新）：Claude 自主调用 Git 工具，分析日志截图并定位代码问题
- **飞书文档同步**：通过 MCP 协议自动同步飞书知识库和云空间文档
- **Web 聊天界面**：独立的 Web UI，支持对话历史、图片上传、Markdown 渲染
- **多模态支持**：支持图片输入（日志截图、流程图等）
- **对话历史持久化**：用户提问和回复自动保存到本地文件

### Agentic 代码排查（新功能）

**触发方式**（二选一，必须显式发起，避免误触发普通文档查询）：
- **发送日志截图**：带图片的消息自动进入排查模式
- **斜杠指令**：纯文字排查需以 `/排查`、`/查代码`、`/code` 开头


进入排查模式后，AI 自动：
1. **视觉识别**：从截图中提取异常信息（类名、错误信息、堆栈）
2. **版本定位**：查找对应的 Git 分支/tag
3. **代码搜索**：在指定版本中搜索异常类和错误关键词
4. **上下文读取**：读取抛错位置前后代码
5. **根因分析**：综合日志和代码给出【根因 + 文件:行号 + 修复建议】

**技术实现**：
- **Git 只读工具**：基于 `git grep`/`git show` 实现，不修改工作目录，支持并发查询不同分支
- **Agentic 循环**：Claude 自主决策工具调用顺序，最多 6 轮调用
- **安全隔离**：路径限定在仓库内，ref 白名单验证，防注入/穿越

**使用场景**：
```
用户: [发送日志截图] + "v4.3.6 版本报错"

AI: 🤔 (思考中表情)
    [自动分析]
    → 从截图识别: NullPointerException at xxx.Xxx.method(Xxx.java:125)
    → 查找分支: release/4.3.6
    → 搜索代码: XxxException
    → 读取文件: src/xxx/Xxx.java 第 100-150 行
    
AI 回复:
    根据日志和代码分析，问题原因是...
    代码位置：src/xxx/Xxx.java:125
    修复建议：...
```

### RAG 检索流程
1. **混合检索**：
   - 向量语义搜索（ChromaDB + text2vec-base-chinese ONNX 模型）
   - BM25 关键词匹配（jieba 分词）
   - RRF（Reciprocal Rank Fusion）等权融合
2. **AI 标题过滤**：Claude 判断候选文档标题与查询的相关性，过滤无关文档
3. **上下文注入**：将相关文档内容注入到大模型 prompt 中

### 技术亮点
- **GPU 加速**：ONNX Runtime GPU 向量化，支持单卡/多卡轮询
- **非侵入式**：不修改 IM 客户端，仅通过 API 和 UI 自动化实现
- **高并发**：异步事件处理，支持多会话并发
- **模块化设计**：插件化 IM 适配器和 AI Provider

## 快速开始

### 系统要求
- Python 3.10+
- Linux / Windows 10/11 均支持
- （微信适配器仅限 Windows，依赖 pywinauto）
- （可选）NVIDIA GPU + CUDA 11.x/12.x（用于向量化加速）

### 1. 安装依赖

本项目使用 [uv](https://github.com/astral-sh/uv) 进行依赖管理。

```bash
# 安装 uv（如果尚未安装）
pip install uv

# CPU 版本（仅用 CPU 做向量化）
uv sync --extra cpu

# GPU 版本（NVIDIA GPU + CUDA）
uv sync --extra gpu
```

**依赖说明**：
- `--extra cpu`：安装 `onnxruntime>=1.15.0,<1.18.0`（纯 CPU 推理）
- `--extra gpu`：安装 `onnxruntime-gpu>=1.15.0,<1.18.0`（需要 CUDA 11.x 或 12.x）

**GPU 显存估算**（text2vec-base-chinese 模型）：
- batch_size=32：1-1.5GB
- batch_size=64：2-3GB
- batch_size=128：4-6GB
- batch_size=256：8-12GB

### 2. 配置文件

复制配置模板并修改：

```bash
cp config.example.yaml config.yaml
```

**核心配置项**：

#### 触发条件
```yaml
trigger:
  keyword: "【ai】"           # 触发关键词
  check_mention: true         # 是否检查 @机器人
  check_private: true         # 是否响应私聊
```

#### AI Provider
```yaml
ai:
  primary:
    provider: "anthropic"     # openai / anthropic / dify
    base_url: "https://api.anthropic.com"
    api_key: "sk-ant-xxx"     # 必填，从环境变量或直接填写
    model: "claude-3-5-sonnet-20241022"
  
  dify:                       # Dify 专用配置（如使用 dify provider）
    app_type: "chatbot"       # chatbot / agent / workflow
    user: "default-user"
  
  timeout: 90                 # API 超时（秒）
  multimodal: false           # 是否启用多模态（图片输入）
```

#### 飞书 Bot
```yaml
adapters:
  feishu:
    mode: "bot_api"           # bot_api（推荐）或 ui_automation
    app_id: "cli_xxx"         # 飞书应用 ID
    app_secret: "xxx"         # 飞书应用 Secret
    verification_token: "xxx" # Webhook 验证 Token
    encrypt_key: "xxx"        # Webhook 加密 Key
    allowed_chats: []         # 白名单群组 ID（空=全部）
    allowed_users: []         # 白名单用户 ID（空=全部）
    welcome_message: "你好，我是 AI 助手..."
```

#### 飞书文档同步（RAG 知识库）
```yaml
feishu_docs:
  enabled: true
  mcp_url: "http://localhost:3000/sse"  # MCP 服务端点
  doc_base_url: "https://xxx.feishu.cn"
  cache_dir: "./data/feishu_docs_cache"
  cache_ttl: 3600               # 文档缓存有效期（秒）
  sources:
    - type: "wiki"
      token: "xxx"              # 知识库 wiki_token
      description: "产品文档"
    - type: "drive"
      token: "xxx"              # 云空间 folder_token
      description: "技术文档"
```

#### 向量数据库
```yaml
vector_db:
  use_gpu: true                 # 是否使用 GPU
  gpu_id: [0]                   # 单卡：[0]，多卡：[0, 1]
  batch_size: 64                # 批处理大小（见显存估算）
```

#### 本地文档（可选）
```yaml
local_docs:
  - path: "./docs/manual.txt"
    description: "用户手册"
  - path: "./docs/faq.md"
    description: "常见问题"
```

#### 系统参数
```yaml
system:
  poll_interval: 10.0           # 轮询间隔（秒，UI 自动化模式）
  webhook_port: 8080            # Webhook 服务端口
  disable_proxy: true           # 禁用系统代理（建议开启）
  max_concurrent_workers: 5     # 最大并发处理数
```

#### 对话历史持久化
```yaml
chat_history:
  enabled: true
  dir: "./data/chat_history"    # 保存路径，按天分文件（JSONL 格式）
```

### 3. 飞书 Bot 配置

#### 创建飞书应用
1. 访问 [飞书开放平台](https://open.feishu.cn/app) 创建企业自建应用
2. 在「应用功能 → 机器人」中开启机器人能力
3. 获取 `app_id` 和 `app_secret`

#### 申请权限

在「权限管理」中申请以下权限：

| 权限标识 | 权限名称 | 用途 |
|---------|---------|------|
| `im:message` | 获取与发送单聊、群组消息 | **核心权限**：发送/回复消息 |
| `im:message.p2p_msg:readonly` | 读取用户发给机器人的单聊消息 | 接收私聊消息 |
| `im:message.group_at_msg:readonly` | 接收群聊中@机器人消息事件 | 接收群聊@消息 |

#### 配置事件订阅

在「事件订阅」页面：

1. 配置请求地址（Webhook URL）：`https://your-domain.com/webhook/feishu`
2. 获取 `verification_token` 和 `encrypt_key`
3. 订阅事件：`im.message.receive_v1`

> **注意**：飞书要求 Webhook 必须使用 HTTPS。

#### Webhook HTTPS 配置

**开发环境（ngrok 内网穿透）**：
```bash
ngrok http 8080
# 将生成的地址（如 https://abc123.ngrok.io/webhook/feishu）填入飞书事件订阅
```

**生产环境（Nginx 反向代理 + SSL）**：
```nginx
server {
    listen 443 ssl http2;
    server_name your-domain.com;

    ssl_certificate /etc/letsencrypt/live/your-domain.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/your-domain.com/privkey.pem;

    location /webhook/feishu {
        proxy_pass http://127.0.0.1:8080;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

申请免费 SSL 证书：
```bash
sudo apt install certbot python3-certbot-nginx
sudo certbot --nginx -d your-domain.com
```

### 4. 微信适配器配置

基于 [pywechat](https://github.com/Hello-Mr-Crab/pywechat) 实现，通过 UI 自动化操作微信客户端。

**前提条件**：
- Windows 10/11 系统
- 微信客户端已安装并登录

**安装（根据微信版本选择）**：
```bash
# 微信 3.9+ 版本（推荐，稳定性最好）
pip install pywechat127==1.9.7

# 微信 4.1+ 版本
pip install git+https://github.com/Hello-Mr-Crab/pywechat.git
```

**配置**：
```yaml
adapters:
  wechat:
    wechat_version: "3.9"          # "3.9" 或 "4.1"
    poll_interval: 1.0
    monitored_chats: ["好友名", "群名"]  # 监控的聊天列表，空=全部
```

**版本差异**：

| 特性 | 微信 3.9 | 微信 4.1 |
|------|---------|---------|
| 自动监听 | 支持 | 有兼容性问题 |
| 稳定性 | 高 | 一般 |
| 系统要求 | Windows 7/10 | Windows 10/11 |


> 智能轮询：程序仅在微信窗口可见时进行轮询，最小化时自动暂停。

### 5. MCP 文档服务（可选）

如果需要同步飞书文档作为知识库：

```bash
# 进入 MCP 服务目录
cd /path/to/feishu-doc-mcp

# 启动 MCP 服务（默认端口 3000）
node getFeishuDocMcp.py
```

MCP 服务提供 SSE 接口用于实时文档拉取。

### 6. 运行服务

#### 方式一：Docker 部署（推荐，适合运维）

Docker 相关文件在 `docker/` 目录下。

```bash
# 1. 准备配置文件
cp config.example.yaml config.yaml
# 编辑 config.yaml，填入 API Key、飞书凭证等

# 2. 构建并启动（二选一）

# GPU 版本（需要 nvidia-container-toolkit）
cd docker
docker-compose up -d ai-assistant-gpu

# CPU 版本
cd docker
docker-compose up -d ai-assistant-cpu
```

**常用操作**：
```bash
# 查看日志
docker-compose logs -f ai-assistant-gpu

# 停止服务
docker-compose down

# 重启
docker-compose restart

# 查看状态
docker-compose ps
```

**挂载说明**：
| 宿主机路径 | 容器路径 | 说明 |
|-----------|----------|------|
| `../config.yaml` | `/app/config.yaml` (ro) | 配置文件（只读） |
| `../models/` | `/app/models/` (ro) | Embedding 模型 |
| `../data/` | `/app/data/` | 文档缓存 + 向量数据库 |
| `../logs/` | `/app/logs/` | 日志文件 |

**GPU 版本要求**：
- NVIDIA 驱动已安装
- [nvidia-container-toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/install-guide.html) 已安装
- docker-compose.yml 中 `device_ids: ['0']` 可改为其他卡号

也可以使用交互式部署脚本：
```bash
cd docker
bash docker-deploy.sh
```

#### 方式二：使用 service.sh（本地部署）

```bash
# 启动服务（后台运行）
./service.sh start

# 停止服务
./service.sh stop

# 重启服务
./service.sh restart

# 查看状态
./service.sh status

# 调试模式（前台运行，输出日志）
./service.sh debug

# 监控模式（每 30s 检查，崩溃自动重启）
./service.sh monitor
```

#### 方式三：直接运行

```bash
# 前台运行
uv run run.py

# 后台运行（nohup）
nohup uv run run.py > output.log 2>&1 &
```

### 7. 使用 Web 界面

服务启动后访问：`http://localhost:8080`

**功能**：
- 文本输入框（支持 Shift+Enter 换行）
- 图片上传（拖拽/粘贴/点击上传，最大 10MB）
- 对话历史（localStorage 持久化，最多 50 个对话，每个最多 100 条消息）
- Markdown 渲染（支持代码高亮、链接自动跳转）

## 项目结构

```
AI-Personal-Assistant/
│
├── run.py                       # 启动入口（uv run run.py）
├── sync_docs.py                 # 手动同步文档的独立脚本
├── service.sh                   # 服务管理脚本（start/stop/restart/status/debug/monitor）
├── config.example.yaml          # 配置模板（复制为 config.yaml 使用）
├── config.yaml                  # 实际配置文件（已 .gitignore）
├── pyproject.toml               # 项目元信息和依赖定义（uv 使用）
├── uv.lock                      # 依赖锁文件
├── requirements.txt             # pip 兼容依赖（自动生成）
├── requirements-cpu.txt         # CPU 版依赖
├── requirements-gpu.txt         # GPU 版依赖
│
├── docker/                      # Docker 部署相关
│   ├── Dockerfile               # GPU 版镜像（nvidia/cuda:11.8 + Python 3.12）
│   ├── Dockerfile.cpu           # CPU 版镜像
│   ├── docker-compose.yml       # Compose 编排（GPU/CPU 两个 service）
│   ├── docker-deploy.sh         # 交互式部署脚本（引导选择 GPU/CPU）
│   ├── .dockerignore            # Docker 构建忽略文件
│   └── README.md                # Docker 部署详细说明
│
├── src/ai_assistant/            # 主程序源码
│   ├── __init__.py
│   ├── main.py                  # 主循环：初始化适配器、事件队列、线程池调度
│   ├── webhook_server.py        # Flask 服务：Webhook 接收 + Web API + 静态文件
│   │
│   ├── core/                    # 核心模块
│   │   ├── models.py            # 数据模型（Message, Session, Event 等）
│   │   ├── config.py            # 配置加载和验证（读取 config.yaml）
│   │   ├── context_manager.py   # 上下文管理：session 级多轮对话窗口
│   │   ├── ai_provider.py       # AI Provider 基类（定义统一接口）
│   │   ├── reply_executor.py    # 回复执行器：调用 AI + 注入文档上下文
│   │   ├── feishu_doc_manager.py # 飞书文档管理：MCP 同步 + 缓存 + RAG 编排
│   │   ├── hybrid_search.py     # 混合检索引擎：ChromaDB 向量 + BM25 + RRF 融合
│   │   ├── simple_mcp_client.py # MCP 协议客户端（SSE 通信）
│   │   ├── chat_history.py      # 对话历史持久化（JSONL 格式按天存储）
│   │   └── trace_context.py     # 请求追踪上下文（日志关联）
│   │
│   ├── adapters/                # IM 适配器（插件化，各自独立）
│   │   ├── base.py              # 适配器基类（定义 start/stop/send_reply 接口）
│   │   ├── feishu_bot.py        # 飞书适配器：Bot API + Webhook 事件处理
│   │   └── wechat_adapter.py    # 微信适配器：pywinauto UI 自动化
│   │
│   ├── providers/               # AI Provider 实现
│   │   ├── anthropic_provider.py # Anthropic Claude API
│   │   ├── openai_provider.py   # OpenAI / 兼容 API（如 Azure、本地模型）
│   │   └── dify_provider.py     # Dify 平台（chatbot/agent/workflow）
│   │
│   ├── static/                  # Web 前端静态资源
│   │   └── index.html           # Web 聊天界面（纯前端 SPA）
│   │
│   └── utils/                   # 工具函数
│       └── __init__.py
│
├── tests/                       # 测试目录
│   └── unit/                    # 单元测试
│       ├── test_models.py       # 数据模型测试
│       ├── test_config.py       # 配置加载测试
│       ├── test_context_manager.py  # 上下文管理测试
│       ├── test_feishu_adapter.py   # 飞书适配器测试
│       ├── test_reply_executor.py   # 回复执行器测试
│       ├── test_embedding.py    # 向量化测试
│       └── test_wechat.py       # 微信适配器测试
│
├── models/                      # 本地模型文件（需手动下载）
│   └── text2vec-base-chinese/   # 中文 Embedding 模型（ONNX 格式）
│       ├── model.onnx           # ONNX 模型文件
│       ├── tokenizer.json       # Tokenizer 配置
│       └── config.json          # 模型配置
│
├── data/                        # 运行时数据（自动生成，已 .gitignore）
│   ├── feishu_docs/             # 飞书文档缓存（按 source token 分目录）
│   │   └── {source_token}/      # 保持飞书原始目录结构
│   │       ├── metadata.json    # 缓存元信息（token、时间戳、文档列表）
│   │       └── *.txt            # 文档内容文本
│   ├── local_docs/              # 本地知识库文档（手动放置）
│   │   └── sql/                 # 示例：SQL 脚本
│   └── chat_history/            # 对话历史（JSONL，按天分文件）
│       └── 2026-06-10.jsonl     # 每行一条：{timestamp, session_id, query, answer, ...}
│
└── logs/                        # 日志目录
    └── ai-assistant.log         # 主日志文件（loguru 按大小轮转）
```

### 关键文件说明

| 文件 | 职责 | 修改场景 |
|------|------|----------|
| `core/feishu_doc_manager.py` | 飞书文档同步 + RAG 检索编排 | 修改检索逻辑、文档过滤规则 |
| `core/hybrid_search.py` | 向量+BM25 混合检索 | 调整检索权重、分块策略 |
| `core/reply_executor.py` | AI 回复生成 | 修改 prompt 模板、上下文注入方式 |
| `adapters/feishu_bot.py` | 飞书消息收发 | 适配新版本飞书 API |
| `providers/anthropic_provider.py` | Claude API 调用 | 切换模型、调整参数 |
| `webhook_server.py` | Web 服务和 API | 添加新 API 接口 |
| `config.example.yaml` | 配置模板 | 新增配置项时同步更新 |

## 技术栈

### 后端
- **Python 3.10+**：主语言
- **Flask + Waitress**：Web 服务
- **pywinauto + pyautogui**：Windows UI 自动化
- **requests + httpx-sse**：HTTP 客户端和 SSE 支持
- **loguru**：日志管理
- **pycryptodome**：飞书消息加密解密

### AI 与 NLP
- **Anthropic Claude API**：主 AI Provider
- **ChromaDB**：向量数据库
- **transformers**：Hugging Face 模型加载
- **ONNX Runtime (GPU)**：text2vec-base-chinese 模型推理
- **jieba**：中文分词（BM25 关键词检索）
- **rank-bm25**：BM25 算法实现

### 前端
- **原生 HTML + CSS + JavaScript**
- **marked.js**：Markdown 渲染
- **localStorage**：对话历史持久化

### 依赖管理
- **uv**：快速 Python 包管理器（类似 pip，但更快）

## 使用说明

### 飞书群聊/私聊
1. 将 Bot 添加到群聊
2. 在消息中包含触发关键词（默认 `【ai】`）或 @机器人
3. Bot 自动回复（支持多轮对话，上下文窗口 10 条消息）

### 微信（UI 自动化模式）
1. 打开微信客户端
2. 服务自动监控配置的聊天窗口
3. 包含触发关键词的消息自动回复

### Web 界面
访问 `http://localhost:8080`，直接输入问题，支持：
- 图片上传（多模态输入）
- 对话历史切换
- 复制回复内容
- 链接自动跳转

## 常见问题

### 1. GPU 加速不生效？
检查：
- NVIDIA 驱动是否安装
- CUDA 版本是否匹配（`nvidia-smi` 查看）
- `config.yaml` 中 `vector_db.use_gpu: true`
- 安装了 `onnxruntime-gpu`（`uv sync --extra gpu`）

### 2. 飞书 Bot 收不到消息？
检查：
- Webhook URL 是否公网可访问（或使用内网穿透如 ngrok）
- `verification_token` 和 `encrypt_key` 是否正确
- 飞书应用权限是否开启
- 事件订阅是否配置

### 3. 检索不到飞书文档？
检查：
- MCP 服务是否运行（`http://localhost:3000/sse`）
- `feishu_docs.sources` 中的 `token` 是否正确
- 飞书应用是否有文档访问权限
- 日志中是否有同步错误

### 4. 向量化内存溢出？
降低 `vector_db.batch_size`：
- 8GB 显存：batch_size=64
- 4GB 显存：batch_size=32
- CPU 模式：batch_size=16

### 5. 对话历史在哪？
- **前端**：浏览器 localStorage（`http://localhost:8080`）
- **后端**：`data/chat_history/` 目录，JSONL 格式，按天分文件

## 开发指南

### 添加新的 IM 适配器
1. 在 `src/ai_assistant/adapters/` 创建新文件
2. 继承 `IMAdapter` 基类
3. 实现 `start()`, `stop()`, `send_reply()` 方法
4. 在 `config.example.yaml` 添加配置项

### 添加新的 AI Provider
1. 在 `src/ai_assistant/providers/` 创建新文件
2. 继承 `AIProvider` 基类
3. 实现 `send_message()` 和 `check_health()` 方法
4. 在 `config.py` 中注册

### 运行测试
```bash
# 单元测试
uv sync --extra dev
uv run pytest tests/unit/ -v

# 测试覆盖率
uv run pytest tests/unit/ --cov=ai_assistant --cov-report=html
```

## 安全与隐私

- **敏感配置**：`config.yaml` 已加入 `.gitignore`，不会提交到 Git
- **消息日志**：仅记录消息长度和时间戳，不记录完整内容
- **数据隔离**：对话历史仅保存在本地，不上传到第三方服务
- **非侵入式**：不修改 IM 客户端文件，不注入进程

## 许可证

MIT License

## 贡献

欢迎提交 Issue 和 Pull Request。

## 更新日志

### 2026-07-03
- **AI Provider 错误处理优化**：识别欠费、认证失败、速率限制等错误码，返回友好提示而非流程卡住
- **MCP 调用失败保护**：MCP 调用失败时保留本地缓存，不误删文档，自动重试
- **飞书告警集成**：MCP 故障时通过 Webhook 发送红色告警卡片，及时通知运维

### 2026-07-02
- **文档检索架构重构**：统一文档检索到 AIProvider 基类，OpenAI/Dify 获得文档检索能力
- **文档索引友好提示**：索引更新期间返回友好提示，避免用户困惑

### 2026-07-01
- **飞书消息优化**：支持代码块、表格、引用块的正确渲染
- **文档删除同步**：自动检测飞书文档删除并同步到本地缓存

### 2026-06-10
- 修复进程卡死问题（索引期间持有锁）
- 修复双重索引问题（并发控制）
- 添加对话历史持久化功能
- 优化 Web 前端 Markdown 链接渲染

### 2026-06-08
- 支持问答结果保存
- 修复混合检索候选数量截断问题
- 关键词提取强制保留版本号

### 2026-06-07
- 初始版本发布
- 支持飞书 Bot API 和微信 UI 自动化
- 实现 RAG 混合检索
- 添加 Web 聊天界面
