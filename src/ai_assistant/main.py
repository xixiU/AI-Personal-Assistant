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
from ai_assistant.providers.cherrystudio import CherryStudioProvider
from ai_assistant.adapters.feishu import FeishuAdapter


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

        self.ai_provider = CherryStudioProvider(
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
        if any(adapter.get("name") == "feishu" and adapter.get("enabled", False)
               for adapter in self.config.__dict__.get("adapters", [])):
            self.adapters.append(FeishuAdapter())

        self.running = False

        logger.info("AI Assistant initialized successfully")

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
        poll_interval = 0.5  # 轮询间隔（秒）

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
            if self.reply_executor.execute(reply):
                logger.info("Reply executed successfully")
            else:
                logger.error("Failed to execute reply")

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
