from dataclasses import dataclass, field
from typing import Optional, List, Dict, Any
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

    # 系统配置
    system_poll_interval: float = 5.0

    # 日志
    logging_level: str = "INFO"
    logging_file: str = "logs/ai-assistant.log"

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

        # 解析系统配置
        if "system" in data:
            config.system_poll_interval = data["system"].get("poll_interval", config.system_poll_interval)

        # 解析日志
        if "logging" in data:
            config.logging_level = data["logging"].get("level", config.logging_level)
            config.logging_file = data["logging"].get("file", config.logging_file)

        # 解析适配器
        if "adapters" in data:
            config.adapters = data["adapters"]

        return config
