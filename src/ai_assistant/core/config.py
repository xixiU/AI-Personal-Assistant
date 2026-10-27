from dataclasses import dataclass, field
from typing import Optional, List, Dict, Any
import yaml
from pathlib import Path


@dataclass
class RepositoryConfig:
    """单个仓库配置"""
    name: str                         # 仓库标识名（如 "backend", "frontend"）
    repo_path: str                    # 仓库本地路径
    default_ref: str = "origin/main"  # 默认分支/tag
    description: str = ""             # 仓库描述（注入给 AI 辨别用途）
    auth_mode: str = "none"           # 认证模式: none / https / ssh
    auth_username: str = ""           # HTTPS 模式的用户名
    auth_password: str = ""           # HTTPS 模式的密码/token
    auth_ssh_key: str = ""            # SSH 模式指定私钥路径（可选）


@dataclass
class Config:
    """配置管理类"""
    # 触发规则
    trigger_keyword: str = "【ai】"
    trigger_check_mention: bool = True
    trigger_check_private: bool = True
    trigger_ignore_mention_all: bool = True  # 忽略 @所有人 的消息（默认不回复）

    # 上下文策略
    context_mode: str = "short"
    context_max_messages: int = 10
    context_session_timeout: int = 3600
    # 纯图片消息等待后续文字描述的合并窗口（秒），0 表示关闭等待
    context_lone_image_wait: float = 8.0
    # 飞书 bot 模式：收集当前消息前 N 条历史消息（支持"先发图、后发文字"场景）
    # 0 表示只处理当前消息（旧行为），>=1 表示向前回溯 N 条
    context_feishu_lookback_count: int = 5

    # 日志文件附件（用户上传 .txt/.log 或 zip 日志包，自动进入代码排查模式）
    log_files_enabled: bool = True
    log_files_dir: str = "./data/log_files"       # 下载落地目录（按会话分子目录）
    log_files_max_size_mb: int = 20               # 单文件大小上限（MB），超限跳过
    log_files_inline_chars: int = 20000           # 直接注入 AI 的文本字符数上限，超出只给路径
    log_files_cleanup_hour: int = 23              # 每日清理时刻（0-23，本地时间）
    log_files_retention_hours: int = 24           # 保留时长（小时），早于此的文件被清理
    log_files_allowed_ext: List[str] = None       # 允许的扩展名，None 时用内置默认

    # AI配置
    ai_primary_provider: str = "openai"
    ai_primary_base_url: str = "http://localhost:8000"
    ai_primary_api_key: str = ""
    ai_primary_model: str = "gpt-4-vision-preview"
    ai_timeout: int = 30
    ai_multimodal: bool = False

    # 飞书文档配置
    feishu_docs_enabled: bool = False
    feishu_docs_mcp_url: str = "http://localhost:50070/sse"
    feishu_docs_doc_base_url: str = ""  # 飞书文档域名（如 https://xxx.feishu.cn）
    feishu_docs_cache_dir: str = "./data/feishu_docs"
    feishu_docs_cache_ttl: int = 86400  # 缓存有效期（秒），默认1天
    feishu_docs_sources: List[str] = None  # 知识库/云空间 token 列表

    # 统一告警通道（文档同步失败、提示词攻击拦截等都走这里）
    # 兼容旧配置 feishu_docs.alert_webhook，加载时会归并到此字段
    alert_webhook: Optional[str] = None  # 飞书告警 Webhook（可选）

    # AI 调用失败告警开关（额度不足、Key 失效、限流、超时等，走 alert.webhook）
    alert_ai_error_enabled: bool = True

    # 安全防护配置
    security_prompt_guard_enabled: bool = True  # 输入侧提示词攻击防护开关（AI 自主判断）

    # 本地离线文档配置
    local_docs: List[Dict[str, str]] = None  # [{path, description}]

    # 回复执行
    reply_mode: str = "clipboard"
    reply_notification: bool = True

    # 系统配置
    system_poll_interval: float = 5.0
    system_webhook_port: int = 8080  # Webhook 服务器端口
    system_disable_proxy: bool = True  # 禁用系统代理（避免代理干扰内网访问）
    system_event_queue_size: int = 100  # 事件队列最大长度
    system_max_concurrent_workers: int = 5  # 最大并发处理数
    # 自有前端 Web（首页 / Web 聊天 / Web 反馈接口）开关。
    # 默认关闭：当前前端无鉴权，任何人都能访问，公网/共享环境下有风险。
    # 关闭时仍保留飞书 webhook（/webhook/feishu、/webhook/feishu/card）与 /health。
    system_web_frontend_enabled: bool = False

    # 对话历史
    chat_history_enabled: bool = True  # 默认开启
    chat_history_dir: str = "./data/chat_history"

    # 向量数据库配置
    vector_db_use_gpu: bool = False  # 是否使用 GPU 加速 Embedding 生成
    vector_db_gpu_id: Any = 0  # GPU 配置：单卡用 int (0/1)，多卡用 list ([0,1]) 或 str ("0,1")
    vector_db_batch_size: int = 32  # Embedding 批处理大小（GPU: 128-256, CPU: 16-32）

    # 日志
    logging_level: str = "INFO"
    logging_file: str = "logs/ai-assistant.log"

    # 代码排查配置
    troubleshoot_enabled: bool = False  # 是否启用代码排查功能
    troubleshoot_repo_path: str = "./data/business_repo"  # 业务系统代码仓库路径
    troubleshoot_default_ref: str = "origin/main"  # 默认分支/tag
    troubleshoot_timeout_mode: str = "time"  # 超时模式: time(总时间限制) / rounds(轮次限制)
    troubleshoot_max_time: int = 300  # 总时间限制（秒），默认 5 分钟
    troubleshoot_max_rounds: int = 6  # Agentic 最大工具调用轮数（timeout_mode=rounds 时生效）
    troubleshoot_tool_timeout: int = 30  # 单个工具超时时间（秒）
    troubleshoot_branch_hint: str = ""  # 版本号→分支映射提示（注入给 AI）
    troubleshoot_repositories: List['RepositoryConfig'] = None  # 多仓库配置列表

    # 运维操作配置
    operations: Optional[Dict[str, Any]] = None  # 运维功能配置（machines/applications/authorization）

    # IM 适配器配置
    adapters: List[Dict[str, Any]] = None

    def __post_init__(self):
        """初始化后处理"""
        if self.adapters is None:
            self.adapters = [{
                "name": "feishu",
                "enabled": True,
                "priority": 1,
                "mode": "ui_automation"
            }]

    @classmethod
    def load(cls, config_path: str) -> "Config":
        """从YAML文件加载配置"""
        path = Path(config_path)
        if not path.exists():
            raise FileNotFoundError(f"Config file not found: {config_path}")

        # 优先 UTF-8，失败则尝试 GBK（兼容 Windows 记事本等以 GBK 保存的配置）
        data = None
        for encoding in ("utf-8", "utf-8-sig", "gbk"):
            try:
                with open(path, 'r', encoding=encoding) as f:
                    data = yaml.safe_load(f)
                if encoding != "utf-8":
                    import warnings
                    warnings.warn(
                        f"配置文件 {config_path} 使用 {encoding} 编码，建议转为 UTF-8"
                    )
                break
            except UnicodeDecodeError:
                continue

        if data is None:
            raise ValueError(
                f"无法解码配置文件 {config_path}，请确认文件编码为 UTF-8 或 GBK"
            )

        config = cls()

        # 解析触发规则
        if "trigger" in data:
            config.trigger_keyword = data["trigger"].get("keyword", config.trigger_keyword)
            config.trigger_check_mention = data["trigger"].get("check_mention", config.trigger_check_mention)
            config.trigger_check_private = data["trigger"].get("check_private", config.trigger_check_private)
            config.trigger_ignore_mention_all = data["trigger"].get("ignore_mention_all", config.trigger_ignore_mention_all)

        # 解析上下文策略
        if "context" in data:
            config.context_mode = data["context"].get("mode", config.context_mode)
            config.context_max_messages = data["context"].get("max_messages", config.context_max_messages)
            config.context_session_timeout = data["context"].get("session_timeout", config.context_session_timeout)
            config.context_lone_image_wait = float(
                data["context"].get("lone_image_wait", config.context_lone_image_wait)
            )
            config.context_feishu_lookback_count = data["context"].get(
                "feishu_lookback_count", config.context_feishu_lookback_count
            )

        # 解析日志文件附件配置
        if "log_files" in data:
            lf = data["log_files"]
            config.log_files_enabled = lf.get("enabled", config.log_files_enabled)
            config.log_files_dir = lf.get("dir", config.log_files_dir)
            config.log_files_max_size_mb = lf.get("max_size_mb", config.log_files_max_size_mb)
            config.log_files_inline_chars = lf.get("inline_chars", config.log_files_inline_chars)
            config.log_files_cleanup_hour = lf.get("cleanup_hour", config.log_files_cleanup_hour)
            config.log_files_retention_hours = lf.get("retention_hours", config.log_files_retention_hours)
            config.log_files_allowed_ext = lf.get("allowed_ext", config.log_files_allowed_ext)

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

        # 解析飞书文档配置
        if "feishu_docs" in data:
            docs = data["feishu_docs"]
            config.feishu_docs_enabled = docs.get("enabled", config.feishu_docs_enabled)
            config.feishu_docs_mcp_url = docs.get("mcp_url", config.feishu_docs_mcp_url)
            config.feishu_docs_doc_base_url = docs.get("doc_base_url", config.feishu_docs_doc_base_url)
            config.feishu_docs_cache_dir = docs.get("cache_dir", config.feishu_docs_cache_dir)
            config.feishu_docs_cache_ttl = docs.get("cache_ttl", config.feishu_docs_cache_ttl)
            config.feishu_docs_sources = docs.get("sources", config.feishu_docs_sources)
            # 向后兼容：旧配置把 alert_webhook 放在 feishu_docs 下，先读到统一字段
            if "alert_webhook" in docs:
                config.alert_webhook = docs.get("alert_webhook", config.alert_webhook)

        # 解析统一告警通道（顶层 alert 段优先，覆盖旧的 feishu_docs.alert_webhook）
        if "alert" in data:
            config.alert_webhook = data["alert"].get("webhook", config.alert_webhook)
            config.alert_ai_error_enabled = data["alert"].get(
                "ai_error_enabled", config.alert_ai_error_enabled
            )

        # 解析安全防护配置
        if "security" in data:
            sec = data["security"]
            config.security_prompt_guard_enabled = sec.get(
                "prompt_guard_enabled", config.security_prompt_guard_enabled
            )

        # 解析本地离线文档配置
        if "local_docs" in data:
            config.local_docs = data["local_docs"]

        # 解析回复执行
        if "reply" in data:
            config.reply_mode = data["reply"].get("mode", config.reply_mode)
            config.reply_notification = data["reply"].get("notification", config.reply_notification)

        # 解析系统配置
        if "system" in data:
            config.system_poll_interval = data["system"].get("poll_interval", config.system_poll_interval)
            config.system_webhook_port = data["system"].get("webhook_port", config.system_webhook_port)
            config.system_disable_proxy = data["system"].get("disable_proxy", config.system_disable_proxy)
            config.system_event_queue_size = data["system"].get("event_queue_size", config.system_event_queue_size)
            config.system_max_concurrent_workers = data["system"].get("max_concurrent_workers", config.system_max_concurrent_workers)
            config.system_web_frontend_enabled = data["system"].get("web_frontend_enabled", config.system_web_frontend_enabled)

        # 解析向量数据库配置
        if "vector_db" in data:
            config.vector_db_use_gpu = data["vector_db"].get("use_gpu", config.vector_db_use_gpu)
            config.vector_db_gpu_id = data["vector_db"].get("gpu_id", config.vector_db_gpu_id)
            config.vector_db_batch_size = data["vector_db"].get("batch_size", config.vector_db_batch_size)

        # 解析日志
        if "logging" in data:
            config.logging_level = data["logging"].get("level", config.logging_level)
            config.logging_file = data["logging"].get("file", config.logging_file)

        # 解析对话历史
        if "chat_history" in data:
            config.chat_history_enabled = data["chat_history"].get("enabled", config.chat_history_enabled)
            config.chat_history_dir = data["chat_history"].get("dir", config.chat_history_dir)

        # 解析代码排查配置
        if "troubleshoot" in data:
            ts = data["troubleshoot"]
            config.troubleshoot_enabled = ts.get("enabled", config.troubleshoot_enabled)
            config.troubleshoot_repo_path = ts.get("repo_path", config.troubleshoot_repo_path)
            config.troubleshoot_default_ref = ts.get("default_ref", config.troubleshoot_default_ref)
            config.troubleshoot_timeout_mode = ts.get("timeout_mode", config.troubleshoot_timeout_mode)
            config.troubleshoot_max_time = ts.get("max_time", config.troubleshoot_max_time)
            config.troubleshoot_max_rounds = ts.get("max_rounds", config.troubleshoot_max_rounds)
            config.troubleshoot_tool_timeout = ts.get("tool_timeout", config.troubleshoot_tool_timeout)
            config.troubleshoot_branch_hint = ts.get("branch_hint", config.troubleshoot_branch_hint)

            # 解析多仓库配置
            if "repositories" in ts:
                # 新格式：显式的 repositories 列表
                config.troubleshoot_repositories = [
                    RepositoryConfig(
                        name=repo.get("name", "default"),
                        repo_path=repo.get("repo_path", config.troubleshoot_repo_path),
                        default_ref=repo.get("default_ref", config.troubleshoot_default_ref),
                        description=repo.get("description", ""),
                        auth_mode=repo.get("auth_mode", "none"),
                        auth_username=repo.get("auth_username", ""),
                        auth_password=repo.get("auth_password", ""),
                        auth_ssh_key=repo.get("auth_ssh_key", ""),
                    )
                    for repo in ts["repositories"]
                ]
            else:
                # 旧格式：用单仓库字段自动构建单元素列表
                config.troubleshoot_repositories = [
                    RepositoryConfig(
                        name="default",
                        repo_path=config.troubleshoot_repo_path,
                        default_ref=config.troubleshoot_default_ref,
                        description="",
                    )
                ]

        # 解析适配器
        if "adapters" in data:
            config.adapters = data["adapters"]

        # 解析运维操作配置
        if "operations" in data:
            config.operations = data["operations"]

        return config
