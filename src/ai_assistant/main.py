#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
AI 自动回复助手 - 主程序

监听 IM 工具窗口，检测触发关键词，调用 AI 生成回复。
使用事件队列 + 线程池架构，支持多用户并发处理。
"""

import time
import sys
import os
import queue
import threading
import requests
from typing import Optional
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from datetime import datetime
from loguru import logger

from ai_assistant.core.config import Config
from ai_assistant.core.context_manager import ContextManager
from ai_assistant.core.reply_executor import ReplyExecutor
from ai_assistant.core.models import Message, Content
from ai_assistant.core.trace_context import get_trace_id, with_new_trace_id, set_trace_id


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

        # 禁用代理（如果配置了）
        if self.config.system_disable_proxy:
            os.environ['HTTP_PROXY'] = ''
            os.environ['HTTPS_PROXY'] = ''
            os.environ['http_proxy'] = ''
            os.environ['https_proxy'] = ''
            logger.info("System proxy disabled")

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
            self.ai_provider = DifyProvider(
                base_url=self.config.ai_primary_base_url,
                api_key=self.config.ai_primary_api_key,
                app_type=getattr(self.config, 'ai_dify_app_type', 'chat'),
                user=getattr(self.config, 'ai_dify_user', 'default-user'),
                timeout=self.config.ai_timeout
            )
        else:
            from ai_assistant.providers.openai_provider import OpenAIProvider
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

        # 事件队列 + 线程池（支持多用户并发）
        self.event_queue = queue.Queue(maxsize=self.config.system_event_queue_size)
        self.executor = ThreadPoolExecutor(
            max_workers=self.config.system_max_concurrent_workers,
            thread_name_prefix="event-worker"
        )

        # 初始化适配器
        self.adapters = []
        self.webhook_server = None
        self._init_adapters()

        self.running = False

        logger.info(
            f"AI Assistant initialized (queue_size={self.config.system_event_queue_size}, "
            f"max_workers={self.config.system_max_concurrent_workers})"
        )

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
        """启动 webhook 服务器"""
        try:
            from ai_assistant.webhook_server import WebhookServer

            self.webhook_server = WebhookServer(host="0.0.0.0", port=self.config.system_webhook_port)
            self.webhook_server.set_feishu_adapter(feishu_adapter)
            self.webhook_server.set_event_queue(self.event_queue)

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

    def _start_event_consumer(self):
        """启动事件消费线程，从队列取事件提交到线程池并发处理"""
        def consumer():
            logger.info("Event consumer started")
            while self.running:
                try:
                    event_data = self.event_queue.get(timeout=1)
                    self.executor.submit(self._process_event, event_data)
                except queue.Empty:
                    continue
                except Exception as e:
                    logger.error(f"Event consumer error: {e}")

        self.consumer_thread = threading.Thread(
            target=consumer,
            daemon=True,
            name="event-consumer"
        )
        self.consumer_thread.start()

    def _process_event(self, event_data: dict):
        """
        处理单个事件（在线程池工作线程中执行）

        Args:
            event_data: 事件数据，包含 trace_id, adapter, raw_data
        """
        import time as time_mod
        start_time = time_mod.time()

        trace_id = event_data["trace_id"]
        adapter = event_data["adapter"]
        raw_data = event_data["raw_data"]  # 原始 webhook 数据

        # 设置当前线程的 trace_id
        set_trace_id(trace_id)

        try:
            logger.info(f"⏱️  Event processing started (queue_size={self.event_queue.qsize()})")

            # 第一步：让适配器处理原始事件（解密、解析、处理欢迎消息等）
            # 适配器返回需要 AI 回复的消息事件，或 None（不需要 AI 回复）
            processed_event = adapter.process_webhook_event(raw_data)
            if not processed_event:
                logger.debug("Adapter returned None, no AI reply needed")
                return

            # 第二步：解析消息内容
            parsed = self._parse_feishu_event(processed_event, adapter)
            if not parsed:
                logger.debug("Event parsing returned None, skipping")
                return

            session_id = parsed["chat_id"]
            text = parsed["text"]
            message_id = parsed["message_id"]

            logger.info(f"Processing message for session: {session_id}")

            # 构建用户消息
            user_message = Message(
                role="user",
                content=[Content(type="text", data=text)],
                timestamp=datetime.now()
            )

            # 添加到上下文
            self.context_manager.add_message(session_id, user_message)

            # 获取上下文消息
            context_messages = self.context_manager.get_context(session_id)
            logger.info(f"Sending {len(context_messages)} messages to AI")

            # 调用 AI 生成回复（记录耗时）
            ai_start = time_mod.time()
            reply = self.ai_provider.send_message(context_messages, session_id=session_id)
            ai_duration = time_mod.time() - ai_start
            logger.info(f"⏱️  AI reply received in {ai_duration:.2f}s: {reply[:100]}...")

            # 将 AI 回复添加到上下文
            ai_message = Message(
                role="assistant",
                content=[Content(type="text", data=reply)],
                timestamp=datetime.now()
            )
            self.context_manager.add_message(session_id, ai_message)

            # 通过适配器发送回复（使用 message_id 回复具体消息）
            send_start = time_mod.time()
            self._send_feishu_reply(adapter, message_id, session_id, reply)
            send_duration = time_mod.time() - send_start

            total_duration = time_mod.time() - start_time
            logger.info(f"⏱️  Total processing time: {total_duration:.2f}s (AI: {ai_duration:.2f}s, Send: {send_duration:.2f}s)")

        except Exception as e:
            logger.error(f"Failed to process event: {e}")

    def _parse_feishu_event(self, event_data: dict, adapter=None) -> Optional[dict]:
        """
        从飞书事件数据中解析出消息信息

        Args:
            event_data: 飞书事件数据（v2.0 格式或 v1.0 转换后的格式）
            adapter: 飞书适配器（用于白名单检查）

        Returns:
            解析后的消息字典，包含 chat_id, text, message_id, sender_id
            如果不是有效的文本消息则返回 None
        """
        import json as json_mod

        try:
            event = event_data.get("event", event_data)
            if "event" in event_data:
                event = event_data["event"]

            message = event.get("message", {})
            sender = event.get("sender", {})

            chat_id = message.get("chat_id", "")
            message_type = message.get("message_type", "")
            message_id = message.get("message_id", "")
            sender_id = sender.get("sender_id", {}).get("open_id", "")

            # 白名单检查
            if adapter:
                if adapter.allowed_chats and chat_id not in adapter.allowed_chats:
                    logger.info(f"❌ Chat {chat_id} not in whitelist, skipping")
                    return None
                if adapter.allowed_users and sender_id not in adapter.allowed_users:
                    logger.info(f"❌ User {sender_id} not in whitelist, skipping")
                    return None

            if message_type != "text":
                logger.info(f"Skipping non-text message type: {message_type}")
                return None

            content_str = message.get("content", "{}")
            content_data = json_mod.loads(content_str)
            text = content_data.get("text", "")

            if not text:
                return None

            return {
                "chat_id": chat_id,
                "chat_type": message.get("chat_type", "p2p"),
                "text": text,
                "message_id": message_id,
                "sender_id": sender_id
            }

        except Exception as e:
            logger.error(f"Failed to parse feishu event: {e}")
            return None

    def _send_feishu_reply(self, adapter, message_id: str, chat_id: str, reply_text: str):
        """
        通过飞书适配器发送回复（线程安全，不依赖 adapter 共享状态）

        Args:
            adapter: 飞书适配器实例
            message_id: 要回复的消息 ID
            chat_id: 聊天 ID
            reply_text: 回复文本
        """
        import json as json_mod

        try:
            token = adapter.get_tenant_access_token()
            url = f"{adapter.base_url}/open-apis/im/v1/messages/{message_id}/reply"

            headers = {
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json"
            }

            payload = {
                "msg_type": "text",
                "content": json_mod.dumps({"text": reply_text})
            }

            response = requests.post(url, headers=headers, json=payload, timeout=10)
            response.raise_for_status()
            result = response.json()

            if result.get("code") == 0:
                logger.info(f"Reply sent successfully to message {message_id}")
            else:
                logger.error(f"Failed to send reply: code={result.get('code')}, msg={result.get('msg')}")

        except Exception as e:
            logger.error(f"Error sending feishu reply: {e}")

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

        # 自定义日志格式化函数，从 contextvars 读取 trace_id
        def format_record(record):
            from ai_assistant.core.trace_context import get_trace_id
            record["extra"]["trace_id"] = get_trace_id()
            return record

        # 日志格式中加入 trace_id
        log_format = (
            "<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <level>{level: <8}</level> | "
            "<cyan>{name}</cyan>:<cyan>{function}</cyan> | "
            "<yellow>[{extra[trace_id]}]</yellow> - <level>{message}</level>"
        )

        # 添加控制台输出
        logger.add(
            sys.stderr,
            level=self.config.logging_level,
            format=log_format
        )

        # 添加文件输出
        log_file = Path(self.config.logging_file)
        log_file.parent.mkdir(parents=True, exist_ok=True)

        logger.add(
            log_file,
            level=self.config.logging_level,
            rotation="00:00",
            retention="7 days",
            encoding="utf-8",
            format="{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {name}:{function} | [{extra[trace_id]}] - {message}"
        )

        # 配置 patcher，在每条日志前自动注入 trace_id
        logger.configure(patcher=format_record)

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

        # 启动事件消费线程
        self._start_event_consumer()

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
        self.executor.shutdown(wait=True)
        logger.info("AI Assistant stopped")

    def _main_loop(self):
        """主循环（只处理需要轮询的适配器，如微信）"""
        from ai_assistant.adapters.feishu_bot import FeishuBotAdapter

        poll_interval = self.config.system_poll_interval

        # 过滤出需要轮询的适配器（排除 webhook 驱动的飞书适配器）
        polling_adapters = [a for a in self.adapters if not isinstance(a, FeishuBotAdapter)]

        if not polling_adapters:
            logger.info("No polling adapters, main loop only handles session cleanup")

        while self.running:
            try:
                # 只轮询非 webhook 驱动的适配器（微信等）
                for adapter in polling_adapters:
                    if adapter.detect_active_window():
                        trace_id = with_new_trace_id()
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

            # 调用 AI 生成回复（传递 session_id 支持多用户并发）
            reply = self.ai_provider.send_message(context_messages, session_id=session_id)

            logger.info(f"AI reply received: {reply[:100]}...")

            # 将 AI 回复添加到上下文
            ai_message = Message(
                role="assistant",
                content=[Content(type="text", data=reply)],
                timestamp=datetime.now()
            )
            self.context_manager.add_message(session_id, ai_message)

            # 执行回复
            adapter_class_name = adapter.__class__.__name__

            if adapter_class_name == "FeishuBotAdapter":
                if adapter.send_reply(reply):
                    logger.info("Reply sent via Feishu Bot API successfully")
                else:
                    logger.error("Failed to send reply via Feishu Bot API")
            elif adapter_class_name == "WeChatAdapter":
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
