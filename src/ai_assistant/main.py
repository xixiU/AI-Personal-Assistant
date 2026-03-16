#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
AI 自动回复助手 - 主程序

监听 IM 工具窗口，检测触发关键词，调用 AI 生成回复
"""

import time
import sys
from pathlib import Path
from datetime import datetime
from loguru import logger

from ai_assistant.core.config import Config
from ai_assistant.core.context_manager import ContextManager
from ai_assistant.core.reply_executor import ReplyExecutor
from ai_assistant.core.models import Message, Content


class AIAssistant:
    """AI 自动回复助手主类"""

    def __init__(self, config_path: str = "config.yaml"):
        """
        初始化 AI 助手

        Args:
            config_path: 配置文件路径
        """
        # 加载配置
        self.config = self._load_config(config_path)

        # 初始化日志
        self._setup_logging()

        # 初始化各个模块
        self.context_manager = ContextManager(
            max_messages=self.config.context_max_messages,
            session_timeout=self.config.context_session_timeout
        )

        # 初始化 AI Provider
        provider_type = self.config.ai_primary_provider
        if provider_type == "dify":
            from ai_assistant.providers.dify_provider import DifyProvider
            # Dify 平台
            self.ai_provider = DifyProvider(
                base_url=self.config.ai_primary_base_url,
                api_key=self.config.ai_primary_api_key,
                app_type=getattr(self.config, 'ai_dify_app_type', 'chat'),
                user=getattr(self.config, 'ai_dify_user', 'default-user'),
                timeout=self.config.ai_timeout
            )
        else:
            from ai_assistant.providers.openai_provider import OpenAIProvider
            # 默认使用 OpenAI 兼容接口
            self.ai_provider = OpenAIProvider(
                base_url=self.config.ai_primary_base_url,
                api_key=self.config.ai_primary_api_key,
                model=self.config.ai_primary_model,
                timeout=self.config.ai_timeout
            )

        self.reply_executor = ReplyExecutor(
            mode=self.config.reply_mode,
            notification=self.config.reply_notification
        )

        # 初始化适配器
        self.adapters = []
        self.webhook_server = None
        self._init_adapters()

        self.running = False

        logger.info("AI Assistant initialized successfully")

    def _init_adapters(self):
        """初始化 IM 适配器"""
        for adapter_config in self.config.adapters:
            if not adapter_config.get("enabled", False):
                continue

            name = adapter_config.get("name")

            if name == "feishu":
                from ai_assistant.adapters.feishu_bot import FeishuBotAdapter
                bot_config = adapter_config.get("bot_api", {})
                adapter = FeishuBotAdapter(bot_config)
                self.adapters.append(adapter)
                logger.info("Feishu Bot API adapter initialized")
                self._start_webhook_server(adapter)

            elif name == "wechat":
                try:
                    from ai_assistant.adapters.wechat_adapter import WeChatAdapter
                    adapter = WeChatAdapter(adapter_config)
                    self.adapters.append(adapter)
                    logger.info("WeChat adapter initialized")
                except ImportError as e:
                    logger.error(f"Failed to initialize WeChat adapter: {e}")
                    logger.error("Install pywechat with: pip install pywechat127==1.9.7")

    def _start_webhook_server(self, feishu_adapter):
        """启动 webhook 服务器（用于机器人模式）"""
        try:
            from ai_assistant.webhook_server import WebhookServer
            import threading

            self.webhook_server = WebhookServer(host="0.0.0.0", port=self.config.system_webhook_port)
            self.webhook_server.set_feishu_adapter(feishu_adapter)
            # 注册消息处理回调，事件到达时立即处理，无需等待主循环轮询
            self.webhook_server.set_message_handler(self._handle_trigger)

            # 在后台线程启动服务器
            server_thread = threading.Thread(
                target=self.webhook_server.run,
                kwargs={"debug": False},
                daemon=True
            )
            server_thread.start()
            logger.info(f"Webhook server started on port {self.config.system_webhook_port}")

        except Exception as e:
            logger.error(f"Failed to start webhook server: {e}")

    def _load_config(self, config_path: str) -> Config:
        """加载配置文件"""
        try:
            if not Path(config_path).exists():
                logger.warning(f"Config file not found: {config_path}, using defaults")
                return Config()

            config = Config.load(config_path)
            logger.info(f"Config loaded from: {config_path}")
            return config

        except Exception as e:
            logger.error(f"Failed to load config: {e}")
            logger.info("Using default configuration")
            return Config()

    def _setup_logging(self):
        """设置日志"""
        # 移除默认的 handler
        logger.remove()

        # 添加控制台输出
        logger.add(
            sys.stderr,
            level=self.config.logging_level,
            format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <level>{level: <8}</level> | <cyan>{name}</cyan>:<cyan>{function}</cyan> - <level>{message}</level>"
        )

        # 添加文件输出
        log_file = Path(self.config.logging_file)
        log_file.parent.mkdir(parents=True, exist_ok=True)

        logger.add(
            log_file,
            level=self.config.logging_level,
            rotation="00:00",
            retention="7 days",
            encoding="utf-8"
        )

        logger.info("Logging configured")

    def start(self):
        """启动 AI 助手"""
        logger.info("=" * 60)
        logger.info("AI Auto-Reply Assistant Starting...")
        logger.info("=" * 60)

        # 检查 AI 服务健康状态
        if not self.ai_provider.check_health():
            logger.warning("AI service health check failed, but continuing...")

        self.running = True
        logger.info("Assistant is running. Press Ctrl+C to stop.")

        try:
            self._main_loop()
        except KeyboardInterrupt:
            logger.info("Received stop signal")
        finally:
            self.stop()

    def stop(self):
        """停止 AI 助手"""
        self.running = False
        logger.info("AI Assistant stopped")

    def _main_loop(self):
        """主循环"""
        poll_interval = self.config.system_poll_interval  # 从配置读取轮询间隔

        while self.running:
            try:
                # 遍历所有适配器
                for adapter in self.adapters:
                    # 检测活动窗口
                    if adapter.detect_active_window():
                        # 检查触发关键词
                        if adapter.check_trigger(self.config.trigger_keyword):
                            logger.info("Trigger detected!")
                            self._handle_trigger(adapter)

                # 定期清理过期会话
                self.context_manager.cleanup_expired_sessions()

                # 等待下一次轮询
                time.sleep(poll_interval)

            except Exception as e:
                logger.error(f"Error in main loop: {e}")
                time.sleep(poll_interval)

    def _handle_trigger(self, adapter):
        """
        处理触发事件

        Args:
            adapter: 触发的 IM 适配器
        """
        try:
            # 获取会话 ID
            session_id = adapter.get_session_id()
            if not session_id:
                logger.warning("Failed to get session ID")
                return

            logger.info(f"Processing trigger for session: {session_id}")

            # 获取最后的消息
            last_message = adapter.get_last_message_as_message()
            if not last_message:
                logger.warning("No message to process")
                return

            # 添加到上下文
            self.context_manager.add_message(session_id, last_message)

            # 获取上下文消息
            context_messages = self.context_manager.get_context(session_id)

            logger.info(f"Sending {len(context_messages)} messages to AI")

            # 调用 AI 生成回复
            reply = self.ai_provider.send_message(context_messages)

            logger.info(f"AI reply received: {reply[:100]}...")

            # 将 AI 回复添加到上下文
            ai_message = Message(
                role="assistant",
                content=[Content(type="text", data=reply)],
                timestamp=datetime.now()
            )
            self.context_manager.add_message(session_id, ai_message)

            # 执行回复
            # 根据适配器类名判断类型（避免顶层导入）
            adapter_class_name = adapter.__class__.__name__

            if adapter_class_name == "FeishuBotAdapter":
                # 飞书机器人：直接通过 API 回复原消息
                if adapter.send_reply(reply):
                    logger.info("Reply sent via Feishu Bot API successfully")
                else:
                    logger.error("Failed to send reply via Feishu Bot API")
            elif adapter_class_name == "WeChatAdapter":
                # 微信适配器：直接发送消息
                if adapter.send_message(reply):
                    logger.info("Reply sent to WeChat successfully")
                else:
                    logger.error("Failed to send reply to WeChat")

        except Exception as e:
            logger.error(f"Failed to handle trigger: {e}")


def main():
    """主入口函数"""
    config_path = "config.yaml"

    # 检查配置文件
    if not Path(config_path).exists():
        print(f"❌ 配置文件不存在: {config_path}")
        print(f"请先复制 config.example.yaml 为 config.yaml 并配置")
        print(f"\n命令: cp config.example.yaml config.yaml")
        sys.exit(1)

    # 创建并启动助手
    assistant = AIAssistant(config_path)
    assistant.start()


if __name__ == "__main__":
    main()
