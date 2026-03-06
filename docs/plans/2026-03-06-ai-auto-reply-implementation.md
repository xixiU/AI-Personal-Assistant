# AI自动回复系统 - 实施计划（MVP阶段）

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 实现飞书桌面版的AI自动回复功能，支持文本消息处理、短期上下文管理和CherryStudio集成

**Architecture:** 插件化架构，通过pywinauto监听飞书窗口，提取消息后调用CherryStudio API，将回复复制到剪贴板

**Tech Stack:** Python 3.10+, pywinauto, pyautogui, requests, pyyaml, loguru, pytest

---

## 前置准备

### Task 0: 项目初始化

**Files:**
- Create: `requirements.txt`
- Create: `pyproject.toml`
- Create: `config.yaml`
- Create: `.gitignore`
- Create: `README.md`

**Step 1: 创建项目依赖文件**

创建 `requirements.txt`:
```txt
pywinauto>=0.6.8
pyautogui>=0.9.54
Pillow>=10.0.0
requests>=2.31.0
pyyaml>=6.0
loguru>=0.7.0
pytest>=7.4.0
pytest-asyncio>=0.21.0
```

**Step 2: 创建 pyproject.toml**

创建 `pyproject.toml`:
```toml
[project]
name = "ai-auto-reply"
version = "0.1.0"
description = "AI-powered auto-reply assistant for IM tools"
requires-python = ">=3.10"
dependencies = [
    "pywinauto>=0.6.8",
    "pyautogui>=0.9.54",
    "Pillow>=10.0.0",
    "requests>=2.31.0",
    "pyyaml>=6.0",
    "loguru>=0.7.0",
]

[project.optional-dependencies]
dev = [
    "pytest>=7.4.0",
    "pytest-asyncio>=0.21.0",
]
```

**Step 3: 创建默认配置文件**

创建 `config.yaml`:
```yaml
# 触发规则
trigger:
  keyword: "【ai】"
  check_mention: true
  check_private: true

# 上下文策略
context:
  mode: "short"
  max_messages: 10
  session_timeout: 3600

# AI配置
ai:
  primary:
    provider: "cherrystudio"
    base_url: "http://localhost:8000"
    api_key: ""
    model: "gpt-4-vision-preview"

  timeout: 30
  multimodal: false

# 回复执行
reply:
  mode: "clipboard"
  notification: true

# IM适配器
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

**Step 4: 创建 .gitignore**

创建 `.gitignore`:
```
__pycache__/
*.py[cod]
*$py.class
*.so
.Python
venv/
env/
logs/
*.log
config.local.yaml
.pytest_cache/
.coverage
htmlcov/
dist/
build/
*.egg-info/
```

**Step 5: 创建项目目录结构**

运行:
```bash
mkdir -p src/ai_assistant/{adapters,core,utils}
mkdir -p tests/{unit,integration}
mkdir -p logs
touch src/ai_assistant/__init__.py
touch src/ai_assistant/adapters/__init__.py
touch src/ai_assistant/core/__init__.py
touch src/ai_assistant/utils/__init__.py
```

**Step 6: 安装依赖**

运行:
```bash
pip install -r requirements.txt
```

预期: 所有依赖成功安装

**Step 7: 提交初始化**

```bash
git init
git add .
git commit -m "chore: initialize project structure"
```

---

## Task 1: 数据模型定义

**Files:**
- Create: `src/ai_assistant/core/models.py`
- Create: `tests/unit/test_models.py`

**Step 1: 编写数据模型测试**

创建 `tests/unit/test_models.py`:
```python
from datetime import datetime
from ai_assistant.core.models import Content, Message, Session


def test_content_text_creation():
    content = Content(type="text", data="Hello")
    assert content.type == "text"
    assert content.data == "Hello"


def test_message_creation():
    content = Content(type="text", data="Test message")
    message = Message(
        role="user",
        content=[content],
        timestamp=datetime.now()
    )
    assert message.role == "user"
    assert len(message.content) == 1
    assert message.content[0].data == "Test message"


def test_session_add_message():
    session = Session(
        session_id="test_session",
        messages=[],
        context_mode="short",
        max_messages=10
    )

    content = Content(type="text", data="Message 1")
    message = Message(role="user", content=[content], timestamp=datetime.now())

    session.add_message(message)
    assert len(session.messages) == 1


def test_session_max_messages_limit():
    session = Session(
        session_id="test_session",
        messages=[],
        context_mode="short",
        max_messages=3
    )

    for i in range(5):
        content = Content(type="text", data=f"Message {i}")
        message = Message(role="user", content=[content], timestamp=datetime.now())
        session.add_message(message)

    assert len(session.messages) == 3
    assert session.messages[0].content[0].data == "Message 2"
```

**Step 2: 运行测试确认失败**

运行:
```bash
pytest tests/unit/test_models.py -v
```

预期: FAIL - 模块不存在

**Step 3: 实现数据模型**

创建 `src/ai_assistant/core/models.py`:
```python
from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Union


@dataclass
class Content:
    """消息内容（支持多模态）"""
    type: str  # "text" | "image" | "video"
    data: Union[str, bytes]


@dataclass
class Message:
    """单条消息"""
    role: str  # "user" | "assistant"
    content: List[Content]
    timestamp: datetime


@dataclass
class Session:
    """会话上下文"""
    session_id: str
    messages: List[Message] = field(default_factory=list)
    context_mode: str = "short"
    max_messages: int = 10
    last_active: datetime = field(default_factory=datetime.now)

    def add_message(self, message: Message) -> None:
        """添加消息到会话，自动维护最大消息数限制"""
        self.messages.append(message)
        self.last_active = datetime.now()

        # 保持消息数量在限制内
        if len(self.messages) > self.max_messages:
            self.messages = self.messages[-self.max_messages:]

    def get_context_messages(self) -> List[Message]:
        """获取上下文消息"""
        return self.messages.copy()
```

**Step 4: 运行测试确认通过**

运行:
```bash
pytest tests/unit/test_models.py -v
```

预期: PASS - 所有测试通过

**Step 5: 提交**

```bash
git add src/ai_assistant/core/models.py tests/unit/test_models.py
git commit -m "feat: add core data models (Content, Message, Session)"
```

---

## Task 2: 配置管理模块

**Files:**
- Create: `src/ai_assistant/core/config.py`
- Create: `tests/unit/test_config.py`

**Step 1: 编写配置管理测试**

创建 `tests/unit/test_config.py`:
```python
import os
import tempfile
import yaml
from ai_assistant.core.config import Config


def test_load_config_from_file():
    # 创建临时配置文件
    config_data = {
        "trigger": {"keyword": "【test】"},
        "context": {"max_messages": 5},
        "ai": {
            "primary": {
                "provider": "cherrystudio",
                "base_url": "http://localhost:8000"
            }
        }
    }

    with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
        yaml.dump(config_data, f)
        temp_path = f.name

    try:
        config = Config.load(temp_path)
        assert config.trigger_keyword == "【test】"
        assert config.context_max_messages == 5
        assert config.ai_primary_provider == "cherrystudio"
    finally:
        os.unlink(temp_path)


def test_config_default_values():
    config = Config()
    assert config.trigger_keyword == "【ai】"
    assert config.context_max_messages == 10
    assert config.reply_mode == "clipboard"
```

**Step 2: 运行测试确认失败**

运行:
```bash
pytest tests/unit/test_config.py -v
```

预期: FAIL - Config类不存在

**Step 3: 实现配置管理**

创建 `src/ai_assistant/core/config.py`:
```python
from dataclasses import dataclass
from typing import Optional
import yaml
from pathlib import Path


@dataclass
class Config:
    """配置管理类"""
    # 触发规则
    trigger_keyword: str = "【ai】"
    trigger_check_mention: bool = True
    trigger_check_private: bool = True

    # 上下文策略
    context_mode: str = "short"
    context_max_messages: int = 10
    context_session_timeout: int = 3600

    # AI配置
    ai_primary_provider: str = "cherrystudio"
    ai_primary_base_url: str = "http://localhost:8000"
    ai_primary_api_key: str = ""
    ai_primary_model: str = "gpt-4-vision-preview"
    ai_timeout: int = 30
    ai_multimodal: bool = False

    # 回复执行
    reply_mode: str = "clipboard"
    reply_notification: bool = True

    # 日志
    logging_level: str = "INFO"
    logging_file: str = "logs/ai-assistant.log"

    @classmethod
    def load(cls, config_path: str) -> "Config":
        """从YAML文件加载配置"""
        path = Path(config_path)
        if not path.exists():
            raise FileNotFoundError(f"Config file not found: {config_path}")

        with open(path, 'r', encoding='utf-8') as f:
            data = yaml.safe_load(f)

        config = cls()

        # 解析触发规则
        if "trigger" in data:
            config.trigger_keyword = data["trigger"].get("keyword", config.trigger_keyword)
            config.trigger_check_mention = data["trigger"].get("check_mention", config.trigger_check_mention)
            config.trigger_check_private = data["trigger"].get("check_private", config.trigger_check_private)

        # 解析上下文策略
        if "context" in data:
            config.context_mode = data["context"].get("mode", config.context_mode)
            config.context_max_messages = data["context"].get("max_messages", config.context_max_messages)
            config.context_session_timeout = data["context"].get("session_timeout", config.context_session_timeout)

        # 解析AI配置
        if "ai" in data:
            if "primary" in data["ai"]:
                primary = data["ai"]["primary"]
                config.ai_primary_provider = primary.get("provider", config.ai_primary_provider)
                config.ai_primary_base_url = primary.get("base_url", config.ai_primary_base_url)
                config.ai_primary_api_key = primary.get("api_key", config.ai_primary_api_key)
                config.ai_primary_model = primary.get("model", config.ai_primary_model)

            config.ai_timeout = data["ai"].get("timeout", config.ai_timeout)
            config.ai_multimodal = data["ai"].get("multimodal", config.ai_multimodal)

        # 解析回复执行
        if "reply" in data:
            config.reply_mode = data["reply"].get("mode", config.reply_mode)
            config.reply_notification = data["reply"].get("notification", config.reply_notification)

        # 解析日志
        if "logging" in data:
            config.logging_level = data["logging"].get("level", config.logging_level)
            config.logging_file = data["logging"].get("file", config.logging_file)

        return config
```

**Step 4: 运行测试确认通过**

运行:
```bash
pytest tests/unit/test_config.py -v
```

预期: PASS

**Step 5: 提交**

```bash
git add src/ai_assistant/core/config.py tests/unit/test_config.py
git commit -m "feat: add configuration management"
```

---

## Task 3: 上下文管理器

**Files:**
- Create: `src/ai_assistant/core/context_manager.py`
- Create: `tests/unit/test_context_manager.py`

**Step 1: 编写上下文管理器测试**

创建 `tests/unit/test_context_manager.py`:
```python
from datetime import datetime
from ai_assistant.core.context_manager import ContextManager
from ai_assistant.core.models import Content, Message


def test_get_or_create_session():
    manager = ContextManager(max_messages=10)
    session = manager.get_or_create_session("test_session")

    assert session.session_id == "test_session"
    assert len(session.messages) == 0


def test_add_message_to_session():
    manager = ContextManager(max_messages=10)

    content = Content(type="text", data="Hello")
    message = Message(role="user", content=[content], timestamp=datetime.now())

    manager.add_message("test_session", message)

    session = manager.get_or_create_session("test_session")
    assert len(session.messages) == 1


def test_get_context_messages():
    manager = ContextManager(max_messages=3)

    for i in range(5):
        content = Content(type="text", data=f"Message {i}")
        message = Message(role="user", content=[content], timestamp=datetime.now())
        manager.add_message("test_session", message)

    messages = manager.get_context_messages("test_session")
    assert len(messages) == 3
```

**Step 2: 运行测试确认失败**

运行:
```bash
pytest tests/unit/test_context_manager.py -v
```

预期: FAIL

**Step 3: 实现上下文管理器**

创建 `src/ai_assistant/core/context_manager.py`:
```python
from typing import Dict, List
from datetime import datetime, timedelta
from ai_assistant.core.models import Session, Message


class ContextManager:
    """上下文管理器 - 管理多个会话的消息历史"""

    def __init__(self, max_messages: int = 10, session_timeout: int = 3600):
        self.max_messages = max_messages
        self.session_timeout = session_timeout
        self.sessions: Dict[str, Session] = {}

    def get_or_create_session(self, session_id: str) -> Session:
        """获取或创建会话"""
        if session_id not in self.sessions:
            self.sessions[session_id] = Session(
                session_id=session_id,
                max_messages=self.max_messages
            )
        return self.sessions[session_id]

    def add_message(self, session_id: str, message: Message) -> None:
        """添加消息到指定会话"""
        session = self.get_or_create_session(session_id)
        session.add_message(message)

    def get_context_messages(self, session_id: str) -> List[Message]:
        """获取会话的上下文消息"""
        session = self.get_or_create_session(session_id)
        return session.get_context_messages()

    def cleanup_expired_sessions(self) -> None:
        """清理过期的会话"""
        now = datetime.now()
        expired_sessions = []

        for session_id, session in self.sessions.items():
            if (now - session.last_active).total_seconds() > self.session_timeout:
                expired_sessions.append(session_id)

        for session_id in expired_sessions:
            del self.sessions[session_id]
```

**Step 4: 运行测试确认通过**

运行:
```bash
pytest tests/unit/test_context_manager.py -v
```

预期: PASS

**Step 5: 提交**

```bash
git add src/ai_assistant/core/context_manager.py tests/unit/test_context_manager.py
git commit -m "feat: add context manager for session handling"
```

---

