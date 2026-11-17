# 飞书消息链路前置获取用户信息

## 背景

之前在反馈点赞/点踩时实现过"通过飞书 API 获取反馈人信息"。现在将这个能力前置到所有飞书消息链路，在保存问答历史时带上用户信息（user_id、姓名等），方便核对谁通过机器人操作了什么。

## 改动说明

### 1. 扩展 `chat_history.save()` 接口

**文件**: `src/ai_assistant/core/chat_history.py`

新增两个可选参数：
- `user_id: Optional[str] = None` - 用户标识（飞书 open_id、web session user 等）
- `user_name: Optional[str] = None` - 用户昵称（飞书通讯录姓名等）

保存到 JSONL 时，只有当这两个字段非空时才写入记录，兼容其他来源（web、wechat）不传用户信息的场景。

### 2. 扩展 `ai_provider.call()` 接口

**文件**: `src/ai_assistant/core/ai_provider.py`

新增两个可选参数：
- `user_id: Optional[str] = None`
- `user_name: Optional[str] = None`

在两处调用 `chat_history.save()` 时传递用户信息：
- 提示词防护拦截时保存拦截记录
- 正常 AI 回复时保存对话历史

### 3. 飞书消息入口获取用户信息

**文件**: `src/ai_assistant/main.py`

在 `_process_event()` 方法中，调用 AI 之前：
1. 从 parsed 消息中提取 `sender_id`（飞书 open_id）
2. 调用 `adapter.get_user_name(sender_id)` 获取用户昵称
3. 获取失败时记录 warning 日志，不阻断流程
4. 将 user_id 和 user_name 传递给 `ai_provider.call()`

复用了 `feishu_bot.get_user_name()` 方法，该方法：
- 调用飞书通讯录 API `/open-apis/contact/v3/users/{user_id}`
- 带内存缓存，避免重复请求
- 失败时返回空字符串，不抛异常

### 4. 其他来源兼容

**web**: `webhook_server.py` 中调用 `ai_provider.call()` 时不传 user_id 和 user_name，自动为 None，数据库中不保存这两个字段。

**wechat/其他**: 同样不传这两个参数，兼容现有逻辑。

## 数据格式

JSONL 文件中的记录格式（飞书场景）：
```json
{
  "record_id": "uuid",
  "timestamp": "2024-01-01 12:00:00",
  "session_id": "oc_xxx",
  "source": "feishu",
  "query": "用户提问",
  "answer": "AI 回答",
  "latency_ms": 1500,
  "user_id": "ou_xxx",
  "user_name": "张三",
  "context_key": "om_xxx",
  "metadata": {...}
}
```

其他来源（web/wechat）的记录不包含 `user_id` 和 `user_name` 字段。

## 测试

新增测试文件：`tests/unit/test_user_info_in_history.py`

测试覆盖：
1. `chat_history.save()` 能保存用户信息到 JSONL
2. 用户信息为空时不影响保存（兼容其他来源）
3. `ai_provider.call()` 能接收用户信息并传递给 `chat_history.save()`

测试结果：3/3 通过

## 注意事项

1. **获取用户信息失败不阻断流程**：调用飞书通讯录 API 可能因权限不足、网络问题等失败，仅记录 warning 日志，不影响消息处理
2. **内存缓存**：`get_user_name()` 带缓存，避免同一用户反复请求
3. **隐私保护**：用户信息仅保存在本地 JSONL 文件，不上传第三方
4. **向后兼容**：新增字段为可选，旧代码调用 `save()` 和 `call()` 时不传这些参数仍正常工作

## 相关文件

- `src/ai_assistant/core/chat_history.py` - 扩展 save() 接口
- `src/ai_assistant/core/ai_provider.py` - 扩展 call() 接口
- `src/ai_assistant/main.py` - 飞书消息入口获取用户信息
- `src/ai_assistant/adapters/feishu_bot.py` - get_user_name() 方法（已有）
- `tests/unit/test_user_info_in_history.py` - 新增测试
