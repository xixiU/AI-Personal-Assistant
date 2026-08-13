# 反馈机制重构方案：基于 chat_history 扩展 + 飞书 Modal

## 一、要解决的两个问题

### 问题 1：数据冗余 + 无关联
- `chat_history` 存了 `{session_id, query, answer}`
- `feedback` 又重复存了 `{session_id, user_query, ai_response}`
- 两者无关联字段，query/answer 存两遍

### 问题 2：飞书点踩收集不到文字反馈
- 当前点踩直接存空 `feedback_text`，只知道"踩了"
- 需要实现飞书 Modal 表单收集文字

### 🔴 探索中发现的额外 Bug（必须一并修复）

**飞书反馈按钮加错了地方**：
- 飞书机器人是 **webhook 驱动**，真实回复路径是 `main.py:_process_event` → `_send_feishu_reply`（第 430 行）
- 但 agent 把反馈按钮和消息缓存加到了 `feishu_bot.py:send_reply`（第 885 行 `_handle_trigger` 调用，这是**轮询驱动**路径，飞书不走）
- **结果：飞书实际发出的卡片根本没有反馈按钮，缓存也没写入**
- 必须把反馈按钮 + 缓存逻辑迁移到 `_send_feishu_reply`

---

## 二、核心设计：message_id 外键关联

### 2.1 统一的 message_id 生成策略

**关键约束**：chat_history 保存发生在 `ai_provider.call()` 内部，此时飞书 AI 回复消息还没发出，拿不到飞书 message_id。

**解决方案**：由系统生成一个**内部统一的 record_id（UUID）**作为主键，各渠道的原生 message_id 作为附加映射。

- `chat_history` 保存时生成 `record_id`（UUID），返回给调用方
- 反馈记录只存 `{record_id, feedback_type, feedback_text}` 轻量数据
- 通过 `record_id` 关联回 chat_history 取完整 query/answer

**各渠道如何拿到 record_id 用于反馈**：
- **Web**：`/api/chat` 响应里返回 `record_id`，前端反馈时带上
- **飞书**：AI 回复消息发送后，建立 `飞书message_id → record_id` 映射缓存；用户点按钮时用飞书 message_id 反查 record_id

### 2.2 chat_history 数据结构扩展

```json
{
  "record_id": "uuid",          // 新增：内部唯一主键
  "timestamp": "2026-07-24 15:30:45",
  "session_id": "chat_xxx",
  "source": "feishu|web|wechat",
  "query": "用户提问",
  "answer": "AI 回答",
  "latency_ms": 1234,
  "feedback": null              // 新增：反馈信息（可选，后写入）
}
```

**feedback 字段结构**（点赞/点踩后回填）：
```json
"feedback": {
  "type": "like|dislike",
  "text": "用户反馈文字",
  "feedback_time": "2026-07-24 15:31:00"
}
```

### 2.3 关联存储方案

采用**独立反馈文件 + record_id 外键**（不直接改写 chat_history 的 JSONL，避免重写整个文件）：

`data/feedback/YYYY-MM-DD.jsonl`：
```json
{
  "feedback_id": "uuid",
  "record_id": "关联的 chat_history record_id",  // 外键
  "timestamp": "2026-07-24 15:31:00",
  "session_id": "chat_xxx",
  "source": "feishu|web",
  "feedback_type": "like|dislike",
  "feedback_text": "用户反馈"
}
```

**消除冗余**：反馈文件不再存 user_query/ai_response，只存 record_id 外键。

**Prompt 注入时**：
1. `get_session_negative_feedbacks(session_id)` 查反馈记录，拿到 record_id 列表
2. 用 record_id 去 chat_history 反查完整 query/answer
3. 拼接注入 prompt

---

## 三、模块改造清单

### 模块 1：ChatHistoryManager 扩展

**文件**：`src/ai_assistant/core/chat_history.py`

**改动**：
1. `save()` 生成 `record_id`（UUID），写入记录，**返回 record_id**
2. 新增 `get_record_by_id(record_id)`：按 record_id 查询单条记录（跨天查找，最多 7 天）
3. 新增 `get_records_by_ids(record_ids)`：批量查询（Prompt 注入用）

### 模块 2：FeedbackManager 重构

**文件**：`src/ai_assistant/core/feedback_manager.py`

**改动**：
1. `save_feedback()` 参数简化：
   - 移除 `user_query`, `ai_response`（不再冗余存储）
   - 改为必填 `record_id`（关联 chat_history）
   - 保留 `session_id, source, feedback_type, feedback_text`
2. `get_session_negative_feedbacks()` 返回记录带 `record_id`
   - 新增参数 `chat_history_manager`，用 record_id 反查 query/answer
   - 或返回 record_id 列表，由调用方（provider）自己查

### 模块 3：ai_provider.call() 返回 record_id

**文件**：`src/ai_assistant/core/ai_provider.py`

**改动**：
1. `chat_history.save()` 返回的 record_id 保存下来
2. `call()` 方法**返回值从 `str` 改为 `(reply, record_id)`** 元组
   - 或在 provider 上暂存 `last_record_id`（线程不安全，不推荐）
   - 推荐返回元组
3. `_build_feedback_prompt()` 改为：查负反馈的 record_id → 用 chat_history 反查 query/answer → 拼接

### 模块 4：main.py 传递 record_id

**文件**：`src/ai_assistant/main.py`

**改动**：
1. `call()` 返回值解包 `(reply, record_id)`
2. **飞书路径（_process_event → _send_feishu_reply）**：
   - `_send_feishu_reply` 增加参数接收 record_id
   - 调用带反馈按钮的 `ai_reply_card`
   - 发送后拿到飞书 new_message_id，建立 `飞书message_id → record_id` 映射（存到 adapter）
3. Web 路径（handle_chat）：响应返回 record_id

### 模块 5：飞书反馈按钮迁移 + Modal

**文件**：`src/ai_assistant/adapters/feishu_bot.py`, `webhook_server.py`

**改动**：
1. **迁移反馈按钮到实际路径**：
   - `message_cache` 改为存 `飞书message_id → record_id`（轻量）
   - `process_card_action` 用飞书 message_id 反查 record_id，再存反馈

2. **实现飞书 Modal 表单**（点踩收集文字）：
   - 点踩时，webhook 响应体返回 Modal 卡片 JSON（不是发新消息）
   - Modal 表单含 input 输入框
   - 用户提交 Modal → 触发新的 `card.action.trigger` 事件（带 form_value）
   - 解析 form_value 的文字，回填反馈记录

   **飞书 Modal 技术细节**：
   - webhook 收到点踩按钮事件后，HTTP 响应返回：
     ```json
     {
       "toast": {"type": "info", "content": "请填写反馈"},
       "card": {
         "type": "raw",
         "data": { /* 含 input 表单的卡片 */ }
       }
     }
     ```
   - 或用 callback 更新原卡片为表单形态
   - 表单提交按钮 value 带 `{"action": "submit_feedback", "record_id": "xxx"}`

3. **webhook_server.py**：
   - `/webhook/feishu/card` 需要**返回响应体**（当前返回空 200）
   - 根据 action 类型返回 toast/Modal

### 模块 6：Web 端传递 record_id

**文件**：`src/ai_assistant/static/index.html`, `webhook_server.py`

**改动**：
1. `handle_chat` 响应增加 `record_id` 字段
2. 前端 `sendMessage` 保存 record_id 到消息对象
3. 反馈提交时用 record_id（替代当前的 crypto.randomUUID + sessionStorage 映射）
4. `handle_feedback` 参数改为接收 record_id（移除 user_query/ai_response）

---

## 四、时序图

### Web 反馈流程
```
用户提问 → /api/chat
  → ai_provider.call() 返回 (reply, record_id)
  → chat_history 已存 {record_id, query, answer}
  → 响应返回 {reply, session_id, record_id}
前端保存 record_id
用户点赞 → /api/feedback {record_id, feedback_type, feedback_text}
  → feedback 存 {feedback_id, record_id, type, text}
下一轮提问
  → _build_feedback_prompt: 查负反馈 record_id → chat_history 反查 → 注入
```

### 飞书反馈流程
```
用户提问 → webhook → _process_event
  → ai_provider.call() 返回 (reply, record_id)
  → _send_feishu_reply(带按钮) 发送卡片
  → 拿到飞书 new_message_id
  → adapter 存映射 {飞书message_id → record_id}
用户点赞按钮 → webhook /webhook/feishu/card
  → 从事件 context.open_message_id 拿飞书 message_id
  → 反查 record_id
  → 存反馈 + 返回 toast 确认
用户点踩按钮 → webhook 返回 Modal 表单卡片
  → 用户填写提交 → 新 card.action 事件（带 form_value）
  → 解析文字 + 存反馈
```

---

## 五、实施顺序（子 agent 可部分并行）

### Phase 1：数据层（串行，其他都依赖）
**Task A**：ChatHistoryManager 扩展（record_id 生成 + 查询方法）
**Task B**：FeedbackManager 重构（record_id 外键，移除冗余字段）

### Phase 2：核心链路（依赖 Phase 1）
**Task C**：ai_provider.call() 返回 record_id + Prompt 注入改造
**Task D**：main.py 飞书路径修复（按钮迁移到 _send_feishu_reply + record_id 映射）

### Phase 3：渠道适配（依赖 Phase 2）
**Task E**：飞书 Modal 表单实现（webhook 响应 + form_value 解析）
**Task F**：Web 端 record_id 传递（前端 + API 改造）

### Phase 4：验证
**Task G**：端到端测试 + 单元测试更新 + 文档更新

---

## 六、风险与注意点

1. **call() 返回值变更**：`str` → `(str, str)`，需检查所有调用方（main.py 两处 + 可能的测试）
2. **飞书 Modal 兼容性**：私有化部署（open.xfchat.iflytek.com）的飞书版本可能不支持最新 Modal API，需降级方案（引导回复）
3. **record_id 映射失效**：飞书 message_id → record_id 映射存在 adapter 内存，重启丢失。历史消息反馈会找不到 record_id → 降级：只存 feedback_type，text 为空
4. **旧数据兼容**：已有的 feedback 文件格式变了，旧记录读取要容错
5. **单元测试更新**：test_feedback_manager.py 的 save_feedback 签名变了，需同步改

---

## 七、验收标准

- [ ] chat_history 记录含 record_id，query/answer 只存一份
- [ ] 反馈文件只存 record_id 外键 + 反馈内容，无 query/answer 冗余
- [ ] 飞书实际发出的卡片**带反馈按钮**（修复 bug）
- [ ] 飞书点赞 → toast 确认 + 反馈落库
- [ ] 飞书点踩 → 弹 Modal 表单 → 填写提交 → 文字落库
- [ ] Web 点赞/点踩正常，反馈带 record_id
- [ ] Prompt 注入：查负反馈 record_id → chat_history 反查 → 注入成功
- [ ] 所有 Python 文件语法通过
- [ ] 单元测试更新并通过
