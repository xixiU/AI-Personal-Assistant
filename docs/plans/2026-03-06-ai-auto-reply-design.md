# AI自动回复系统 - 设计文档

**日期**: 2026-03-06
**版本**: 1.0
**状态**: 已批准

## 1. 项目概述

### 1.1 目标
开发一个桌面辅助工具，为微信和飞书等IM工具提供AI自动回复能力，支持混合使用场景（工作协作、客户服务、个人助理）。

### 1.2 核心需求
- 触发机制：@提及/私聊消息以【ai】开头
- 支持平台：企业私有化飞书（优先）、微信
- 消息类型：文本、图片、视频（多模态处理）
- AI集成：本地CherryStudio + OpenAI兼容API备用
- 实现方式：非侵入式，允许用户正常使用IM客户端

### 1.3 技术约束
- 个人用户无企业权限，无法使用官方Bot API
- 必须支持Windows桌面版IM客户端
- 需要处理多模态内容（图片、视频）

## 2. 技术方案选择

### 2.1 方案对比

| 方案 | 优势 | 劣势 | 适用性 |
|------|------|------|--------|
| 桌面客户端辅助工具 | 无需权限、非侵入、支持多IM | 依赖UI自动化 | ✅ 推荐 |
| 浏览器插件 | 实现简单、易维护 | 仅支持Web版 | ❌ 不适用 |
| 消息转发中间件 | 完全合规 | 用户体验差 | ❌ 不适用 |

### 2.2 最终方案
**桌面客户端辅助工具** - 通过Windows无障碍API监听IM窗口，实现自动回复功能。

## 3. 系统架构

### 3.1 整体架构图

```
┌─────────────────────────────────────────────────────┐
│                   用户层                              │
│  ┌──────────┐              ┌──────────┐             │
│  │ 飞书客户端 │              │ 微信客户端 │             │
│  └─────┬────┘              └─────┬────┘             │
└────────┼───────────────────────┼──────────────────┘
         │                       │
         │  UI监听（无障碍API）    │
         │                       │
┌────────▼───────────────────────▼──────────────────┐
│              AI助手核心服务                         │
│  ┌─────────────────────────────────────────┐      │
│  │  消息监听模块 (Message Listener)          │      │
│  │  - UI自动化引擎                           │      │
│  │  - 消息提取器                             │      │
│  │  - 触发词检测                             │      │
│  └──────────┬──────────────────────────────┘      │
│             │                                      │
│  ┌──────────▼──────────────────────────────┐      │
│  │  上下文管理模块 (Context Manager)         │      │
│  │  - 短期消息缓存                           │      │
│  │  - 会话状态管理                           │      │
│  └──────────┬──────────────────────────────┘      │
│             │                                      │
│  ┌──────────▼──────────────────────────────┐      │
│  │  AI集成模块 (AI Integration)              │      │
│  │  - CherryStudio适配器                     │      │
│  │  - OpenAI兼容接口                         │      │
│  │  - 多模态处理                             │      │
│  └──────────┬──────────────────────────────┘      │
│             │                                      │
│  ┌──────────▼──────────────────────────────┐      │
│  │  回复执行模块 (Reply Executor)            │      │
│  │  - 剪贴板操作                             │      │
│  │  - 自动输入（可选）                        │      │
│  └─────────────────────────────────────────┘      │
│                                                    │
│  ┌─────────────────────────────────────────┐      │
│  │  配置管理 (Config Manager)                │      │
│  │  - IM适配器注册                           │      │
│  │  - 规则配置                               │      │
│  └─────────────────────────────────────────┘      │
└────────────────────────────────────────────────────┘
```

### 3.2 核心设计原则
1. **插件化架构** - 每个IM工具作为独立适配器，便于扩展
2. **非侵入式** - 通过UI自动化而非hook或注入
3. **异步处理** - 消息监听和AI调用异步执行，不阻塞用户操作
4. **可配置** - 触发规则、上下文策略、AI模型均可配置

## 4. 核心组件设计

### 4.1 消息监听模块 (Message Listener)

**职责：**
- 监听IM窗口的消息变化
- 识别@提及和私聊消息
- 检测触发关键词【ai】
- 提取消息内容（文本、图片、视频）

**IM适配器接口：**
```python
class IMAdapter:
    def detect_active_window() -> bool
    def extract_messages(count: int) -> List[Message]
    def check_trigger(message: Message) -> bool
    def get_message_content(message: Message) -> List[Content]
```

**飞书适配器实现要点：**
- 窗口标题匹配: "飞书" / "Lark"
- 消息区域定位: 通过UI元素树定位
- @提及检测: 识别"@你"标记
- 图片提取: 截图 + 临时保存

**微信适配器实现要点：**
- 窗口标题匹配: "微信" / "WeChat"
- 消息区域定位: 通过控件ID定位
- 私聊检测: 窗口标题不含"(群)"
- 图片提取: 右键保存 + OCR

### 4.2 上下文管理模块 (Context Manager)

**职责：**
- 维护短期消息历史（默认最近10条）
- 管理会话状态（按窗口/对话ID）
- 智能判断是否需要上下文（可选）

**数据结构：**
```python
@dataclass
class Session:
    session_id: str          # 窗口标识
    messages: List[Message]  # 消息历史（最多N条）
    last_active: datetime    # 最后活跃时间
    context_mode: str        # "short" | "smart"

@dataclass
class Message:
    role: str               # "user" | "assistant"
    content: List[Content]  # 支持多模态
    timestamp: datetime

@dataclass
class Content:
    type: str              # "text" | "image" | "video"
    data: Union[str, bytes]  # 文本或二进制数据
```

**上下文策略：**
- **短期模式**（默认）：保留最近10条消息
- **智能模式**（可选）：根据消息相关性动态调整上下文范围

### 4.3 AI集成模块 (AI Integration)

**职责：**
- 统一的AI调用接口
- 支持CherryStudio和OpenAI兼容API
- 多模态内容转换

**AI Provider接口：**
```python
class AIProvider:
    def send_message(messages: List[Message], multimodal: bool = True) -> str
    def stream_response() -> Iterator[str]  # 可选
    def check_health() -> bool
```

**CherryStudio Provider：**
- 本地HTTP API调用
- 支持多模态模型（GPT-4V/Claude 3）
- 配置: base_url, api_key, model

**OpenAI Provider：**
- OpenAI兼容接口
- 图片转base64编码
- 配置: base_url, api_key, model

### 4.4 回复执行模块 (Reply Executor)

**职责：**
- 将AI回复发送到IM窗口
- 支持人工审核模式

**执行策略：**
- **剪贴板模式**（默认）：复制到剪贴板 + 系统通知，用户Ctrl+V发送
- **自动输入模式**（可选）：模拟键盘输入 + 自动发送

## 5. 数据流程

### 5.1 完整流程

```
1. 消息监听循环 (每500ms轮询)
   ↓
2. 检测活动窗口 (飞书/微信)
   ↓
3. 提取最新消息
   ↓
4. 触发词检测 (【ai】开头)
   ↓
5. 加载上下文 (最近10条消息)
   ↓
6. 构建AI请求 (多模态内容转换)
   ↓
7. 调用AI API (CherryStudio优先)
   ↓
8. 获取回复内容
   ↓
9. 复制到剪贴板 + 通知用户
   ↓
10. 记录到上下文历史
```

### 5.2 时序图

```
用户 -> 飞书: 发送消息 "【ai】帮我总结一下"
飞书 -> 监听模块: 消息变化事件
监听模块 -> 上下文管理: 提取最近10条消息
上下文管理 -> AI集成: 构建请求 (含上下文)
AI集成 -> CherryStudio: HTTP API调用
CherryStudio -> AI集成: 返回回复
AI集成 -> 回复执行: 处理回复内容
回复执行 -> 剪贴板: 复制回复
回复执行 -> 用户: 系统通知 "AI回复已复制"
用户 -> 飞书: Ctrl+V 粘贴发送
```

## 6. 错误处理策略

### 6.1 IM窗口识别失败
- 重试3次，间隔1秒
- 失败后记录日志，继续监听其他窗口

### 6.2 消息提取失败
- 降级策略：使用OCR截图识别
- 仍失败则跳过本次，等待下次轮询

### 6.3 AI API调用失败
- CherryStudio失败 → 自动切换到OpenAI兼容接口
- 两者都失败 → 通知用户"AI服务暂时不可用"
- 超时设置：30秒

### 6.4 多模态内容处理失败
- 图片加载失败 → 仅处理文本部分
- 视频不支持 → 提示"暂不支持视频，请描述内容"

### 6.5 回复发送失败
- 剪贴板操作失败 → 显示弹窗，用户手动复制
- 自动输入失败 → 降级到剪贴板模式

## 7. 配置管理

### 7.1 配置文件结构 (config.yaml)

```yaml
# 触发规则
trigger:
  keyword: "【ai】"
  check_mention: true
  check_private: true

# 上下文策略
context:
  mode: "short"  # short | smart
  max_messages: 10
  session_timeout: 3600  # 秒

# AI配置
ai:
  primary:
    provider: "cherrystudio"
    base_url: "http://localhost:8000"
    api_key: ""
    model: "gpt-4-vision-preview"

  fallback:
    provider: "openai"
    base_url: "https://api.openai.com/v1"
    api_key: "sk-xxx"
    model: "gpt-4-vision-preview"

  timeout: 30
  multimodal: true

# 回复执行
reply:
  mode: "clipboard"  # clipboard | auto_input
  notification: true

# IM适配器
adapters:
  - name: "feishu"
    enabled: true
    priority: 1
  - name: "wechat"
    enabled: false
    priority: 2

# 日志
logging:
  level: "INFO"
  file: "logs/ai-assistant.log"
  rotation: "daily"
  retention: 7
```

## 8. 技术栈

### 8.1 开发语言
- **Python 3.10+** - 主要开发语言

### 8.2 核心依赖
- **pywinauto** - Windows UI自动化
- **pyautogui** - 屏幕截图和键盘模拟
- **Pillow** - 图像处理
- **requests** - HTTP API调用
- **pyyaml** - 配置文件解析
- **loguru** - 日志管理

### 8.3 可选依赖
- **paddleocr** - OCR文字识别（多模态降级）
- **opencv-python** - 视频帧提取

## 9. 实施计划

### 9.1 第一阶段（MVP）
- ✅ 飞书桌面版适配器
- ✅ 文本消息处理
- ✅ 短期上下文管理
- ✅ CherryStudio集成
- ✅ 剪贴板回复模式

### 9.2 第二阶段
- 图片多模态支持
- OpenAI兼容接口
- 智能上下文判断

### 9.3 第三阶段
- 微信桌面版适配器
- 视频处理支持
- 自动输入模式

### 9.4 第四阶段
- 性能优化
- 配置界面（GUI）
- 更多IM适配器（钉钉、Slack等）

## 10. 风险与限制

### 10.1 技术风险
- **UI自动化稳定性**：IM客户端更新可能导致适配失效
  - 缓解：版本检测 + 快速适配机制
- **多模态API成本**：图片处理消耗token较多
  - 缓解：可配置是否启用多模态
- **响应延迟**：AI调用可能需要5-30秒
  - 缓解：异步处理 + 进度提示

### 10.2 使用限制
- 仅支持Windows平台（初期）
- 需要用户授权无障碍权限
- 无法处理语音消息（暂不支持）
- 群聊需要@提及才能触发

## 11. 安全与隐私

### 11.1 数据安全
- 消息内容仅在本地处理，不上传第三方服务器
- AI API调用使用HTTPS加密
- 上下文历史存储在本地，定期清理

### 11.2 权限控制
- 最小权限原则：仅请求必要的无障碍权限
- 用户可随时禁用自动回复功能
- 配置文件中的API Key加密存储

## 12. 测试策略

### 12.1 单元测试
- IM适配器的消息提取逻辑
- 上下文管理的消息缓存
- AI Provider的接口调用

### 12.2 集成测试
- 完整的消息监听 → AI回复流程
- 错误处理和降级策略
- 多IM工具切换

### 12.3 手动测试
- 不同IM客户端版本兼容性
- 多模态内容处理准确性
- 用户体验和响应速度

---

**文档结束**
