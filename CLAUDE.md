# AI 自动回复助手 - 开发规范

## 项目概述

**目标**: 为飞书/微信/web提供基于大模型的智能自动回复功能  
**技术栈**: Python 3.10+, Flask, SQLAlchemy, ChromaDB, Anthropic Claude  
**当前状态**: 生产环境运行中，本地开发，云端部署，不需要在本地安装运行环境，只需要进行基础的代码静测。

## 核心架构

### 1. 适配器模式
- **IM 适配器**: 飞书（webhook）、微信（pywinauto）
- **AI Provider**: Anthropic Claude (主), OpenAI (备)
- **文档检索**: 飞书文档 + ChromaDB 向量库

### 2. 数据管理
- **用户系统**: SQLAlchemy ORM，支持飞书/微信用户
- **问答记录**: 记录所有对话，支持反馈
- **UUID 统一**: 所有实体使用 UUID 主键

### 3. Web 管理后台
- **管理员**: 数据统计、问答历史、用户管理
- **普通用户**: 个人对话历史、反馈记录、词云

## 关键约束

### 代码规范
- ✅ 类型注解必须完整（Python 3.10+）
- ✅ 异常处理必须完善（不允许裸 except）
- ✅ 日志记录使用 loguru（带 request_id）
- ❌ 禁止修改 IM 客户端文件
- ❌ 禁止硬编码敏感信息

### Git 规范
- 提交信息格式: `feat/fix/refactor: 简短描述`
- 新功能必须创建分支，测试后合并
- 禁止直接 push 到 main（CI/CD 保护）

### 安全规范
- 密码使用 bcrypt 加密存储
- API 使用 JWT token 认证
- 飞书 webhook 事件加密验证
- 敏感配置使用环境变量

## 项目结构

```
src/ai_assistant/
├── adapters/           # IM 适配器（飞书、微信）
├── api/               # Web API（管理后台、数据接口）
├── core/              # 核心模块（用户、对话、反馈、文档）
├── providers/         # AI Provider（Anthropic、OpenAI）
├── static/            # 前端静态资源
├── templates/         # HTML 模板
└── utils/             # 工具函数

scripts/               # 数据迁移、部署脚本
tests/                # 单元测试
```

## 开发流程

### 1. 新功能开发
```bash
git checkout -b feature/xxx
# 开发 + 测试
git commit -m "feat: xxx"
git push origin feature/xxx
# 创建 PR，review 后合并
```

### 2. Bug 修复
```bash
git checkout -b fix/xxx
# 修复 + 测试
git commit -m "fix: xxx"
# 直接合并或创建 PR
```

### 3. 部署
```bash
docker restart ai-assistant-gpu
# 验证日志
docker logs -f ai-assistant-gpu
```

## 常见任务

### 添加新的 IM 适配器
1. 继承 `IMAdapter` 基类
2. 实现 `check_trigger()` 和 `get_latest_message()`
3. 在 `main.py` 注册适配器

### 添加新的统计指标
1. 在 `core/chat_history_db.py` 或 `core/feedback_db_query.py` 添加查询方法
2. 在 `api/admin.py` 的 `/api/admin/stats` 返回新指标
3. 前端适配展示

### 添加新的前端页面
1. 在 `templates/` 创建 HTML
2. 在 `static/js/` 添加 JS 逻辑
3. 在 `api/` 添加后端接口

## 技术债务

### 已知问题
1. 微信适配器依赖 Windows，需要迁移到 wechaty
2. 文档检索需要优化缓存策略
3. 前端需要统一使用组件化框架

### 待优化
1. 增加单元测试覆盖率（当前 < 30%）
2. 添加性能监控（响应时间、token 消耗）
3. 优化数据库索引（大数据量场景）

## 参考资源

- [Anthropic Claude API](https://docs.anthropic.com/)
- [Flask 文档](https://flask.palletsprojects.com/)
- [SQLAlchemy 文档](https://docs.sqlalchemy.org/)
- [飞书开放平台](https://open.feishu.cn/document/)

---

**最后更新**: 2026-11-23
