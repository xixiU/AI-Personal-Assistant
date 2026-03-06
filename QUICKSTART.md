# 快速启动指南

## 前提条件

1. **Python 3.10+** 已安装
2. **CherryStudio** 或其他 OpenAI 兼容的 AI 服务正在运行

## 5 分钟快速开始

### 1. 安装依赖

```bash
pip install -r requirements.txt
```

### 2. 配置文件

```bash
# 复制示例配置
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

### 3. 启动程序

```bash
python run.py
```

看到以下输出表示启动成功：
```
AI Auto-Reply Assistant Starting...
Assistant is running. Press Ctrl+C to stop.
```

### 4. 测试使用

1. **打开飞书**
2. **在聊天窗口中选中一条包含【ai】的消息**，例如：
   ```
   【ai】你好，请介绍一下自己
   ```
3. **按 Ctrl+C 复制消息**
4. **等待几秒**，程序会调用 AI 生成回复
5. **看到通知**："🔔 AI 回复已复制到剪贴板"
6. **在飞书输入框按 Ctrl+V 粘贴**，然后发送

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

## 下一步

- 查看 [README.md](README.md) 了解完整功能
- 查看 [config.example.yaml](config.example.yaml) 了解所有配置选项
- 查看日志文件 `logs/ai-assistant.log` 排查问题

## 技术支持

遇到问题？请查看：
1. 日志文件：`logs/ai-assistant.log`
2. 运行测试：`PYTHONPATH=src pytest tests/unit/ -v`
3. 提交 Issue 到 GitHub
