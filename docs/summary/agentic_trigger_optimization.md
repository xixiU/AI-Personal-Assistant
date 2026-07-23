# Agentic 模式触发逻辑

## 概述

`AnthropicProvider._should_use_agentic_mode()` 决定用户消息走**标准 RAG 模式**（查知识库文档回答）还是 **Agentic 模式**（调用 Git 工具排查代码）。

为避免误触发普通文档查询，触发条件**收紧为仅两种显式信号**，用户必须明确表达代码排查意图。

## 修改文件

- `src/ai_assistant/providers/anthropic_provider.py`

## 当前触发条件（仅两种）

前提：Git 工具已启用（`git_tools_enabled=True`），否则一律走标准 RAG。

### 1. 显式斜杠指令

用户消息包含以下任一指令即触发：

- `/排查`
- `/查代码`
- `/code`
- `/search`

**示例**：
```
用户：/查代码 fastjson2 序列化错误
系统：→ 进入 Agentic 模式
```

### 2. 图片消息（日志截图）

消息中包含图片时触发，适配"发日志截图排查"的核心场景。

**示例**：
```
用户：[发送报错日志截图]
系统：→ 进入 Agentic 模式
```

## 设计决策：为什么不用关键词自动触发

早期版本曾尝试根据关键词/技术特征自动触发，但实践中误触发严重，已全部移除：

| 曾用规则 | 移除原因 |
|---------|---------|
| 技术特征（版本号、模块名、接口路径） | "4.3.6 部署注意什么"只是查文档，却因版本号触发 |
| 排查关键词（报错、异常、错误、bug） | "fastjson2 报错怎么解决"是查文档，却因"报错"触发 |
| 意图关键词（查代码、看代码等） | 与斜杠指令重复，且中文短语匹配易误伤 |
| 上下文延续（前几轮提到代码） | 逻辑隐晦，用户难以预测何时触发 |

**核心原则**：代码排查是重操作（多轮工具调用、耗时长、消耗 token），必须由用户**显式**发起，不做隐式猜测。普通提问一律走快速的标准 RAG。

## 触发效果对照

| 用户输入 | 模式 |
|---------|------|
| `4.3.6 fastjson2 报错怎么解决` | 标准 RAG（查文档） |
| `4.3.6 版本部署注意什么` | 标准 RAG（查文档） |
| `/查代码 fastjson2 序列化错误` | Agentic（查代码） |
| `/排查 /count/list 接口` | Agentic（查代码） |
| `[日志截图]` | Agentic（查代码） |

## 日志

触发时记录原因，便于排查：

```
触发 Agentic 模式：显式指令
触发 Agentic 模式：检测到图片消息
```

未触发则记录：`使用标准 RAG 模式`。

## 实现

```python
def _should_use_agentic_mode(self, messages):
    if not self.git_tools_enabled or not self.git_tools:
        return False
    # 1. 显式斜杠指令
    if self._has_explicit_command(self._extract_last_user_text(messages)):
        return True
    # 2. 图片消息
    for msg in messages:
        if any(c.type == "image" for c in msg.content):
            return True
    return False
```

辅助方法：`_has_explicit_command(text)` — 检测 `/排查`、`/查代码`、`/code`、`/search`。

## 演进记录

- **2026-07-16**：初版，含技术特征 / 意图关键词 / 上下文延续 / 排查关键词等多重自动触发。
- **2026-07-23**：移除技术特征、上下文延续自动触发（版本号误触发问题）。
- **2026-07-23**：进一步移除意图关键词、排查关键词触发（"报错"等词误触发问题）。**最终仅保留斜杠指令 + 图片两种显式信号。**
