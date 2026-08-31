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
import signal
import atexit
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
from ai_assistant.core.ai_provider import DocIndexingInProgressError


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

        # 注册脱敏器已知密钥：从配置中递归收集 API Key、账号密码、飞书
        # app_secret、Git 认证凭据等真实敏感值，供回复发送前精确遮蔽。
        from ai_assistant.utils.redactor import register_secrets_from_config
        register_secrets_from_config(self.config)

        # 配置输入侧提示词攻击防护：由 AI 自主判断越狱/指令覆盖、窃取账号密码密钥等恶意输入，
        # 命中则在调用大模型前拒绝响应并上报告警（走统一告警通道 alert.webhook）。
        from ai_assistant.utils.prompt_guard import configure_guard
        configure_guard(
            enabled=getattr(self.config, 'security_prompt_guard_enabled', True),
            alert_webhook=getattr(self.config, 'alert_webhook', None),
        )

        # 记录进程 PID（用于排查进程退出问题）
        logger.info(f"Process PID: {os.getpid()}")

        # 注册信号处理器
        self._setup_signal_handlers()

        # 注册退出清理函数
        atexit.register(self._cleanup_on_exit)

        # 初始化各个模块
        self.context_manager = ContextManager(
            max_messages=self.config.context_max_messages,
            session_timeout=self.config.context_session_timeout
        )

        # 初始化反馈管理器
        from ai_assistant.core.feedback_manager import FeedbackManager
        self.feedback_manager = FeedbackManager()

        # 初始化 AI Provider
        provider_type = self.config.ai_primary_provider

        # 先初始化 Provider（文档管理器需要用到 Provider 的关键词提取能力）
        doc_manager = None
        self.doc_manager = None

        if provider_type == "anthropic":
            from ai_assistant.providers.anthropic_provider import AnthropicProvider
            self.ai_provider = AnthropicProvider(
                api_key=self.config.ai_primary_api_key,
                model=self.config.ai_primary_model,
                base_url=self.config.ai_primary_base_url if self.config.ai_primary_base_url else None,
                timeout=self.config.ai_timeout,
                feedback_manager=self.feedback_manager,
            )
        elif provider_type == "dify":
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

        # 初始化代码排查工具（如果启用）
        if getattr(self.config, 'troubleshoot_enabled', False):
            self.ai_provider.max_rounds = self.config.troubleshoot_max_rounds
            self.ai_provider.timeout_mode = getattr(self.config, 'troubleshoot_timeout_mode', 'time')
            self.ai_provider.max_time = getattr(self.config, 'troubleshoot_max_time', 300)
            self.ai_provider.tool_timeout = getattr(self.config, 'troubleshoot_tool_timeout', 30)
            repositories = getattr(self.config, 'troubleshoot_repositories', None)

            if repositories and len(repositories) >= 1:
                # 多仓库模式：使用 RepoManager 统一管理
                from ai_assistant.tools.repo_manager import RepoManager
                try:
                    repo_manager = RepoManager(repositories)
                    if hasattr(self.ai_provider, 'set_repo_manager'):
                        self.ai_provider.set_repo_manager(repo_manager)
                        # 注入 branch_hint（兼容旧配置，单仓库时仍可用）
                        branch_hint = getattr(self.config, 'troubleshoot_branch_hint', '')
                        if branch_hint:
                            self.ai_provider.branch_hint = branch_hint
                        repo_manager.start_background_fetch()
                        repo_names = [r.name for r in repositories]
                        logger.info(f"代码排查已启用（多仓库模式）: {repo_names}")
                    else:
                        logger.warning("当前 AI Provider 不支持多仓库模式")
                except Exception as e:
                    logger.error(f"初始化多仓库管理器失败: {e}")
                    logger.warning("代码排查功能将不可用")
            else:
                # 旧逻辑兜底：直接用单仓库 GitTools
                from ai_assistant.tools.git_tools import GitTools
                try:
                    git_tools = GitTools(
                        repo_path=self.config.troubleshoot_repo_path,
                        default_ref=self.config.troubleshoot_default_ref
                    )
                    if hasattr(self.ai_provider, 'set_git_tools'):
                        branch_hint = getattr(self.config, 'troubleshoot_branch_hint', '')
                        self.ai_provider.set_git_tools(git_tools, enabled=True, branch_hint=branch_hint)
                        logger.info(f"代码排查已启用: repo={self.config.troubleshoot_repo_path}")

                        def periodic_fetch():
                            while True:
                                time.sleep(1800)
                                try:
                                    git_tools.fetch_updates()
                                except Exception as e:
                                    logger.error(f"Git fetch 失败: {e}")

                        fetch_thread = threading.Thread(target=periodic_fetch, daemon=True, name="git-fetch")
                        fetch_thread.start()
                except Exception as e:
                    logger.error(f"初始化 Git 工具失败: {e}")
                    logger.warning("代码排查功能将不可用")

        # 初始化飞书文档管理器（如果启用）
        if self.config.feishu_docs_enabled:
            from ai_assistant.core.feishu_doc_manager import FeishuDocManager
            # 所有 Provider 均通过基类 extract_keywords() 接口提供关键词提取能力，
            # 无需再按 provider_type 判断，直接由 set_ai_provider() 注入

            doc_manager = FeishuDocManager(
                mcp_url=self.config.feishu_docs_mcp_url,
                cache_dir=self.config.feishu_docs_cache_dir,
                cache_ttl=self.config.feishu_docs_cache_ttl,
                sources=self.config.feishu_docs_sources,
                local_docs=self.config.local_docs,
                use_gpu=getattr(self.config, 'vector_db_use_gpu', False),
                gpu_id=getattr(self.config, 'vector_db_gpu_id', 0),
                batch_size=getattr(self.config, 'vector_db_batch_size', 32),
                doc_base_url=getattr(self.config, 'feishu_docs_doc_base_url', ''),
                alert_webhook=getattr(self.config, 'alert_webhook', None),
            )
            # 回填 doc_manager 到 Provider
            self.ai_provider.doc_manager = doc_manager
            # 注入 AI provider，同时启用关键词提取、通用问题分类、文档标题过滤
            doc_manager.set_ai_provider(self.ai_provider)
            self.doc_manager = doc_manager
            logger.info("飞书文档管理器已启用")

        self.reply_executor = ReplyExecutor(
            mode=self.config.reply_mode,
            notification=self.config.reply_notification
        )

        # 初始化对话历史管理器
        from ai_assistant.core.chat_history import ChatHistoryManager
        if getattr(self.config, 'chat_history_enabled', True):
            self.chat_history = ChatHistoryManager(
                history_dir=getattr(self.config, 'chat_history_dir', './data/chat_history')
            )
            # 设置到 AIProvider 基类（所有渠道共享）
            from ai_assistant.core.ai_provider import AIProvider
            AIProvider.set_chat_history(self.chat_history)
        else:
            self.chat_history = None

        # 事件队列 + 线程池（支持多用户并发）
        self.event_queue = queue.Queue(maxsize=self.config.system_event_queue_size)
        self.executor = ThreadPoolExecutor(
            max_workers=self.config.system_max_concurrent_workers,
            thread_name_prefix="event-worker"
        )
        self._processing_sessions = set()  # 正在处理中的 session，防重复提交
        self._processing_lock = threading.Lock()

        # 处理期间被暂存的消息（含文字的真实提问），当前请求结束后补跑
        # 格式: {session_id: event_data}
        self._deferred_messages = {}

        # 纯图片消息等待后续文字的合并窗口（秒），0 表示关闭
        # 用户常"先发截图、再发问题描述"，等一下能合成一次完整提问
        self._lone_image_wait = getattr(self.config, "context_lone_image_wait", 8.0)
        # 等待期内收到的纯图片消息标记: {session_id: timestamp}
        self._pending_lone_images = {}

        # 文档索引更新期间的延迟重试队列
        # 格式: [(timestamp, event_data), ...]
        self.pending_retry_queue = []
        self._last_indexing_check_time = 0  # 上次检查索引状态的时间

        # 初始化适配器
        self.adapters = []
        self.webhook_server = None
        self._init_adapters()

        self.running = False

        logger.info(
            f"AI Assistant initialized (queue_size={self.config.system_event_queue_size}, "
            f"max_workers={self.config.system_max_concurrent_workers})"
        )
        logger.info(f"Adapters loaded: {[type(a).__name__ for a in self.adapters]}")
        logger.info(f"AI Provider: {type(self.ai_provider).__name__}")
        logger.info(f"Webhook server: {'enabled' if self.webhook_server else 'disabled'}")

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

            self.webhook_server = WebhookServer(
                host="0.0.0.0",
                port=self.config.system_webhook_port,
                web_frontend_enabled=getattr(self.config, 'system_web_frontend_enabled', False),
            )
            self.webhook_server.set_feishu_adapter(feishu_adapter)
            self.webhook_server.set_event_queue(self.event_queue)
            # 注入 AI 组件用于 Web 聊天接口（仅在前端启用时实际生效）
            self.webhook_server.set_ai_components(self.ai_provider, self.context_manager)

            # 在后台线程启动服务器（使用生产级服务器）
            server_thread = threading.Thread(
                target=self.webhook_server.run_production,
                daemon=False,  # 改为非 daemon 线程，避免主线程退出时服务器被强制终止
                name="webhook-server"
            )
            server_thread.start()
            logger.info(f"Webhook server started on port {self.config.system_webhook_port} (production mode)")

        except Exception as e:
            logger.error(f"Failed to start webhook server: {e}", exc_info=True)

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
        import psutil
        import os

        start_time = time_mod.time()
        process = psutil.Process(os.getpid())
        start_cpu = process.cpu_percent()
        start_mem = process.memory_info().rss / 1024 / 1024  # MB

        trace_id = event_data["trace_id"]
        adapter = event_data["adapter"]
        raw_data = event_data["raw_data"]  # 原始 webhook 数据

        # 设置当前线程的 trace_id
        set_trace_id(trace_id)

        try:
            logger.info(f"⏱️  Event processing started (queue_size={self.event_queue.qsize()}, "
                       f"cpu={start_cpu:.1f}%, mem={start_mem:.1f}MB)")

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
            logger.info("parsed message:{}",parsed)

            session_id = parsed["chat_id"]
            text = parsed["text"]
            image_data = parsed.get("image_data")
            message_id = parsed["message_id"]
            user_id = parsed.get("sender_id", "unknown")

            # 新消息到达：清除该 session 的纯图片等待标记，让等待线程立即放行
            image_already_in_context = False
            with self._processing_lock:
                if self._pending_lone_images.pop(session_id, None) is not None:
                    # 之前那张纯图片已由等待逻辑放入上下文，本次不要重复添加
                    image_already_in_context = True

            # 纯图片无文字：用户很可能正在分两条发（先图后文），
            # 立即调 AI 只能反问。这里短暂等待后续文字消息合并处理。
            if image_data and not text and self._lone_image_wait > 0:
                if self._defer_lone_image(session_id, image_data, message_id):
                    return
                # 等待期内没有后续文字，图片已入上下文，避免下方重复添加
                image_already_in_context = True

            # 防重复：同一 session 已有请求在处理中，将消息加入上下文
            # 注意：不能无条件丢弃，否则用户"先发图、AI 处理中再发文字"时，
            # 这条文字会被吞掉，永远得不到回答。
            with self._processing_lock:
                if session_id in self._processing_sessions:
                    # 构建消息内容
                    content_parts = []
                    if image_data and not image_already_in_context:
                        content_parts.append(Content(type="image", data=image_data))
                    if text:
                        content_parts.append(Content(type="text", data=text))

                    if content_parts:
                        user_message = Message(
                            role="user",
                            content=content_parts,
                            timestamp=datetime.now()
                        )
                        self.context_manager.add_message(session_id, user_message)

                    # 带文字的消息是真实提问，记为待处理，等当前请求结束后补跑，
                    # 避免用户的问题被静默丢弃
                    if text:
                        self._deferred_messages[session_id] = event_data
                        logger.info(
                            f"Session {session_id} 处理中，本条含文字已入上下文并登记为待处理，"
                            f"当前请求结束后补跑"
                        )
                    else:
                        logger.info(
                            f"Session {session_id} 处理中，本条无文字仅入上下文，跳过 AI 调用"
                        )
                    return
                self._processing_sessions.add(session_id)

            logger.info(f"Processing message for session: {session_id}, text:{text}, has_image:{image_data is not None}")

            # 添加"思考中"表情回复（飞书体验优化，从 adapter 配置读取）
            if hasattr(adapter, 'add_reaction'):
                # 检查 adapter 配置中的 thinking_reaction 开关
                thinking_enabled = True  # 默认开启
                thinking_emoji = "THINKING"  # 默认 🤔
                if hasattr(adapter, 'bot_config'):
                    thinking_enabled = adapter.bot_config.get('thinking_reaction', True)
                    thinking_emoji = adapter.bot_config.get('thinking_emoji', 'THINKING')

                if thinking_enabled:
                    try:
                        adapter.add_reaction(message_id, emoji_type=thinking_emoji)
                    except Exception as e:
                        logger.warning(f"添加思考表情失败（不影响主流程）: {e}")

            # 构建用户消息（支持图文）
            # image_already_in_context: 图片已由纯图片等待逻辑入过上下文，不重复添加
            content_parts = []
            if image_data and not image_already_in_context:
                content_parts.append(Content(type="image", data=image_data))
            if text:
                content_parts.append(Content(type="text", data=text))

            # 内容为空时不入上下文（例如纯图片已提前入过），避免产生空消息
            if content_parts:
                user_message = Message(
                    role="user",
                    content=content_parts,
                    timestamp=datetime.now()
                )
                self.context_manager.add_message(session_id, user_message)

            # 获取上下文消息
            context_messages = self.context_manager.get_context(session_id)
            logger.info(f"Sending {len(context_messages)} messages to AI")

            # 调用 AI 生成回复
            ai_start = time_mod.time()
            record_id = None
            metadata = {}
            try:
                reply, record_id, metadata = self.ai_provider.call(context_messages, session_id=session_id, source="feishu")
            except DocIndexingInProgressError:
                # 文档索引更新中，加入延迟重试队列
                logger.info(f"文档索引更新中，将消息加入延迟重试队列: session={session_id}")
                self.pending_retry_queue.append((time_mod.time(), event_data))
                # 先给用户回复提示
                reply = "📚 文档索引正在更新中，请稍后（约1-2分钟）再试或等待我稍后回复，或者您可以先问我通用技术问题。"

            ai_duration = time_mod.time() - ai_start

            # 将 AI 回复添加到上下文
            ai_message = Message(
                role="assistant",
                content=[Content(type="text", data=reply)],
                timestamp=datetime.now(),
                metadata=metadata  # 保存模式信息（如 mode: "agentic"）
            )
            self.context_manager.add_message(session_id, ai_message)

            # 通过适配器发送回复（使用 message_id 回复具体消息）
            send_start = time_mod.time()
            self._send_feishu_reply(adapter, message_id, session_id, reply, record_id, ai_duration, metadata)
            send_duration = time_mod.time() - send_start

            # 计算资源使用
            end_cpu = process.cpu_percent()
            end_mem = process.memory_info().rss / 1024 / 1024  # MB
            cpu_delta = end_cpu - start_cpu
            mem_delta = end_mem - start_mem

            total_duration = time_mod.time() - start_time
            logger.info(f"⏱️  Total processing time: {total_duration:.2f}s "
                       f"(AI: {ai_duration:.2f}s, Send: {send_duration:.2f}s), "
                       f"cpu_delta={cpu_delta:.1f}%, mem_delta={mem_delta:.1f}MB")

        except Exception as e:
            logger.error(f"Failed to process event: {e}", exc_info=True)
        finally:
            # 释放 session 处理锁
            deferred_event = None
            if 'session_id' in dir():
                with self._processing_lock:
                    self._processing_sessions.discard(session_id)
                    # 取出处理期间被暂存的提问，补跑一次
                    deferred_event = self._deferred_messages.pop(session_id, None)

            if deferred_event is not None:
                # 补跑是递归调用，限制链长，避免用户狂发消息时栈过深、worker 被长期占用
                depth = event_data.get("_deferred_depth", 0)
                if depth >= 2:
                    logger.warning(
                        f"补跑链已达深度 {depth}，丢弃本次暂存提问避免递归过深: session={session_id}"
                    )
                else:
                    deferred_event["_deferred_depth"] = depth + 1
                    logger.info(f"补跑处理期间暂存的提问 (depth={depth + 1}): session={session_id}")
                    try:
                        self._process_event(deferred_event)
                    except Exception as e:
                        logger.error(f"补跑暂存提问失败: {e}", exc_info=True)

    def _parse_feishu_event(self, event_data: dict, adapter=None) -> Optional[dict]:
        """
        从飞书事件数据中解析出消息信息

        Args:
            event_data: 飞书事件数据（v2.0 格式或 v1.0 转换后的格式）
            adapter: 飞书适配器（用于白名单检查和图片下载）

        Returns:
            解析后的消息字典，包含 chat_id, text, image_data, message_id, sender_id
            如果不是有效消息则返回 None
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

            # 处理文本消息
            if message_type == "text":
                content_str = message.get("content", "{}")
                content_data = json_mod.loads(content_str)
                text = content_data.get("text", "")

                if not text:
                    return None

                # 检查是否包含 @所有人 并且配置了忽略（检查文本内容）
                if self.config.trigger_ignore_mention_all:
                    if "@_all" in text:
                        logger.info("忽略包含 @_all 的消息（根据配置）")
                        return None

                return {
                    "chat_id": chat_id,
                    "chat_type": message.get("chat_type", "p2p"),
                    "text": text,
                    "message_id": message_id,
                    "sender_id": sender_id,
                    "image_data": None
                }

            # 处理图片消息
            elif message_type == "image":
                content_str = message.get("content", "{}")
                content_data = json_mod.loads(content_str)
                image_key = content_data.get("image_key", "")

                if not image_key or not adapter:
                    logger.warning("图片消息但无 image_key 或 adapter")
                    return None

                # 下载图片
                image_data = adapter.download_image(message_id, image_key)
                if not image_data:
                    logger.error(f"图片下载失败: image_key={image_key}")
                    return None

                return {
                    "chat_id": chat_id,
                    "chat_type": message.get("chat_type", "p2p"),
                    "text": "",  # 图片消息无文本
                    "message_id": message_id,
                    "sender_id": sender_id,
                    "image_data": image_data
                }

            # 处理富文本消息（文字+图片）
            elif message_type == "post":
                content_str = message.get("content", "{}")
                content_data = json_mod.loads(content_str)

                text, image_keys = self._parse_post_content(content_data)

                # 解析不出任何内容时，跳过 AI 调用（否则会发出空消息，触发 400）
                if not text and not image_keys:
                    logger.error(
                        f"富文本消息解析为空，跳过 AI 调用。"
                        f"顶层字段={list(content_data.keys())}, content 预览={content_str[:200]}"
                    )
                    return None

                if len(image_keys) > 1:
                    logger.info(f"富文本消息含 {len(image_keys)} 张图片，当前仅取第一张")

                # 下载第一张图片（如果有）
                image_data = None
                if image_keys:
                    if not adapter:
                        logger.error("富文本消息含图片但 adapter 为空，无法下载")
                    else:
                        image_data = adapter.download_image(message_id, image_keys[0])
                        if not image_data:
                            # 图片是进入代码排查模式的触发条件，下载失败会退化为知识库模式
                            logger.error(
                                f"富文本消息中图片下载失败，将退化为纯文本处理"
                                f"（不会进入代码排查模式）: image_key={image_keys[0]}"
                            )

                # 图片下载失败且无文本时无内容可发，跳过
                if not text and not image_data:
                    logger.error("富文本消息仅含图片且下载失败，跳过 AI 调用")
                    return None

                logger.info(
                    f"解析富文本消息: text_len={len(text)}, images={len(image_keys)}, "
                    f"image_ready={image_data is not None}"
                )

                return {
                    "chat_id": chat_id,
                    "chat_type": message.get("chat_type", "p2p"),
                    "text": text,
                    "message_id": message_id,
                    "sender_id": sender_id,
                    "image_data": image_data  # 可能为 None
                }

            else:
                content_preview = str(message.get("content", ""))[:200]
                logger.info(
                    f"跳过非文本/图片消息: {message_type}, content 预览={content_preview}"
                )
                return None

        except Exception as e:
            logger.error(f"Failed to parse feishu event: {e}")
            return None

    def _defer_lone_image(self, session_id: str, image_data: dict, message_id: str) -> bool:
        """
        纯图片消息延迟处理：等待用户紧随其后发送的文字描述

        用户经常分两条发送："先发日志截图 → 再发问题描述"。
        如果收到图片就立即调用 AI，AI 手里没有任何问题描述，只能反问，
        既慢又答不到点上。这里先把图片存入上下文，等待一个短窗口：
        - 窗口内收到了文字消息 → 由那条文字消息触发 AI（图片已在上下文里，
          _should_use_agentic_mode 会因历史图片进入排查模式）
        - 窗口内没有文字 → 返回 False，按纯图片正常处理（AI 会主动询问）

        Args:
            session_id: 会话 ID
            image_data: 已下载的图片数据
            message_id: 飞书消息 ID

        Returns:
            True 表示已交给后续文字消息处理，本次应直接返回；
            False 表示等待超时，调用方应继续按纯图片处理
        """
        import time as time_mod

        # 图片先入上下文，这样后续文字消息触发 AI 时能看到它
        self.context_manager.add_message(
            session_id,
            Message(
                role="user",
                content=[Content(type="image", data=image_data)],
                timestamp=datetime.now()
            )
        )

        arrive_ts = time_mod.time()
        with self._processing_lock:
            self._pending_lone_images[session_id] = arrive_ts

        logger.info(
            f"收到纯图片消息，图片已入上下文，等待 {self._lone_image_wait}s 看是否有后续文字描述: "
            f"session={session_id}"
        )

        # 轮询等待：一旦有新消息进入该 session，标记会被覆盖或清除
        deadline = arrive_ts + self._lone_image_wait
        while time_mod.time() < deadline:
            time_mod.sleep(0.3)
            with self._processing_lock:
                current = self._pending_lone_images.get(session_id)
            # 标记被后续消息清除/更新，说明已有新消息接管
            if current != arrive_ts:
                logger.info(f"等待期内收到后续消息，纯图片交由后续消息一并处理: session={session_id}")
                return True

        # 超时：清除标记，按纯图片继续处理
        with self._processing_lock:
            if self._pending_lone_images.get(session_id) == arrive_ts:
                del self._pending_lone_images[session_id]
        logger.info(f"等待超时，未收到文字描述，按纯图片处理（AI 将主动询问）: session={session_id}")
        return False

    @staticmethod
    def _parse_post_content(content_data: dict) -> tuple:
        """
        解析飞书富文本（post）消息内容，提取文字和图片 key

        兼容多种结构差异：
        - 语言包裹：{"zh_cn": {"content": [...]}}、{"en_us": ...}，或私有化部署下无语言包裹
        - 文字标签：text / md / a（链接）
        - 嵌套层级：content 可能是二维数组，也可能出现更深的嵌套

        Args:
            content_data: message.content 反序列化后的字典

        Returns:
            (text, image_keys) 元组
        """
        import re

        # 定位实际的 content 数组：优先语言包裹，回退到顶层
        post_content = None
        for lang_key in ("zh_cn", "en_us"):
            value = content_data.get(lang_key)
            if isinstance(value, dict):
                post_content = value
                break

        if post_content is None:
            # 无语言包裹（私有化部署）或直接是 {"content": [...]} 结构
            if isinstance(content_data.get("content"), list):
                post_content = content_data
            else:
                # 兜底：取第一个包含 content 的字典型字段
                for value in content_data.values():
                    if isinstance(value, dict) and isinstance(value.get("content"), list):
                        post_content = value
                        break

        if post_content is None:
            return "", []

        text_parts = []
        image_keys = []

        # 文字类标签：text 用 text 字段，a（超链接）取显示文字，md 取原始 markdown
        text_tags = {"text", "md", "a"}

        def walk(node):
            """递归遍历，兼容任意嵌套深度的 content 结构"""
            if isinstance(node, list):
                for item in node:
                    walk(item)
                return
            if not isinstance(node, dict):
                return

            tag = node.get("tag", "")
            if tag in text_tags:
                value = node.get("text", "")
                if value:
                    text_parts.append(value)
            elif tag == "img":
                img_key = node.get("image_key", "")
                if img_key:
                    image_keys.append(img_key)

            # at 标签不取文字（避免把 @机器人 名字混入问题），但继续递归其子节点
            children = node.get("content")
            if isinstance(children, list):
                walk(children)

        walk(post_content.get("content", []))

        # 富文本的换行由 block 切分表达，这里用空格拼接后归一化空白
        text = " ".join(text_parts)
        text = re.sub(r"\s+", " ", text).strip()

        # 去掉飞书 @ 占位符（如 @_user_1），避免污染检索关键词
        text = re.sub(r"@_user_\d+\s*", "", text).strip()

        return text, image_keys

    def _send_feishu_reply(
        self,
        adapter,
        message_id: str,
        chat_id: str,
        reply_text: str,
        record_id: Optional[str] = None,
        elapsed_time: Optional[float] = None,
        metadata: Optional[dict] = None,
    ):
        """
        通过飞书适配器发送回复（使用消息卡片样式，支持 Markdown，带反馈按钮）

        这是飞书 webhook 的真实回复路径。发送带反馈按钮的卡片后，将新消息 ID
        与本次对话历史的 record_id 建立映射，供后续反馈按钮点击时反查。

        Args:
            adapter: 飞书适配器实例
            message_id: 要回复的消息 ID
            chat_id: 聊天 ID
            reply_text: 回复文本
            record_id: 本次对话历史记录的唯一标识（用于反馈按钮回填）
            elapsed_time: 回复耗时（秒）
            metadata: 附加元数据（mode、tool_rounds、doc_count 等）
        """
        from ai_assistant.utils.feishu_message import FeishuMessageBuilder
        from ai_assistant.utils.redactor import redact

        # 纵深防御：发送前再脱敏一次。正常路径回复已在 ai_provider.call() 脱敏，
        # 此处覆盖未来可能绕过 call() 的新增路径（幂等，对已脱敏文本无副作用）。
        reply_text = redact(reply_text)

        try:
            token = adapter.get_tenant_access_token()

            # 从 metadata 提取信息
            mode = metadata.get("mode") if metadata else None
            tool_rounds = metadata.get("tool_rounds") if metadata else None
            doc_count = metadata.get("doc_count") if metadata else None

            # 带反馈按钮的卡片（message_id 仅用于触发按钮渲染，实际映射走 record_id）
            payload = FeishuMessageBuilder.ai_reply_card(
                reply_text,
                message_id="placeholder",
                elapsed_time=elapsed_time,
                tool_rounds=tool_rounds,
                doc_count=doc_count,
                mode=mode,
            )
            success, new_message_id = FeishuMessageBuilder.send(adapter.base_url, token, message_id, payload)

            # 发送成功且有 record_id 时，缓存 新消息ID → record_id 映射
            if success and new_message_id and record_id:
                if hasattr(adapter, "cache_message_record"):
                    adapter.cache_message_record(new_message_id, record_id)
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

    def _setup_signal_handlers(self):
        """设置信号处理器（捕获 SIGTERM/SIGINT/SIGHUP）"""
        def signal_handler(signum, frame):
            sig_name = signal.Signals(signum).name
            logger.warning(f"Received signal {sig_name} ({signum}), shutting down gracefully...")
            self.stop()
            sys.exit(0)

        # 注册信号处理器
        signal.signal(signal.SIGTERM, signal_handler)  # kill 命令默认信号
        signal.signal(signal.SIGINT, signal_handler)   # Ctrl+C
        if hasattr(signal, 'SIGHUP'):  # Windows 没有 SIGHUP
            signal.signal(signal.SIGHUP, signal_handler)  # 终端断开

        logger.info("Signal handlers registered (SIGTERM, SIGINT, SIGHUP)")

    def _cleanup_on_exit(self):
        """进程退出时的清理函数（通过 atexit 注册）"""
        logger.warning("Process exiting, running cleanup...")
        if hasattr(self, 'running') and self.running:
            self.stop()

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
        logger.info("Stopping AI Assistant...")
        self.running = False

        # 停止飞书文档后台同步
        if self.doc_manager:
            self.doc_manager.stop_background_sync()

        # 停止 webhook 服务器
        if self.webhook_server:
            try:
                self.webhook_server.shutdown()
                logger.info("Webhook server stopped")
            except Exception as e:
                logger.error(f"Error stopping webhook server: {e}")

        # 关闭线程池
        self.executor.shutdown(wait=True)
        logger.info("AI Assistant stopped")

    def _main_loop(self):
        """主循环（只处理需要轮询的适配器，如微信）"""
        from ai_assistant.adapters.feishu_bot import FeishuBotAdapter

        poll_interval = self.config.system_poll_interval
        heartbeat_interval = 60  # 每 60 秒输出一次心跳日志
        last_heartbeat = time.time()

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

                # 心跳日志（证明进程还活着）
                now = time.time()
                if now - last_heartbeat >= heartbeat_interval:
                    logger.info(f"💓 Heartbeat: process alive, queue_size={self.event_queue.qsize()}, "
                               f"active_sessions={len(self.context_manager.sessions)}")
                    last_heartbeat = now

                    # 检查并重试索引更新期间积压的消息
                    self._check_and_retry_pending_messages(now)

                # 等待下一次轮询
                time.sleep(poll_interval)

            except Exception as e:
                logger.error(f"Error in main loop: {e}", exc_info=True)
                time.sleep(poll_interval)

    def _check_and_retry_pending_messages(self, now: float):
        """
        检查文档索引状态，如果已完成则重试队列中的消息

        Args:
            now: 当前时间戳
        """
        # 限流：每 30 秒检查一次索引状态（避免频繁检查）
        if now - self._last_indexing_check_time < 30:
            return

        self._last_indexing_check_time = now

        # 如果队列为空，跳过
        if not self.pending_retry_queue:
            return

        # 检查索引是否完成（尝试调用检索，捕获异常判断）
        if not self.doc_manager:
            # 无文档管理器，清空队列
            logger.warning("无文档管理器，清空延迟重试队列")
            self.pending_retry_queue.clear()
            return

        # 轻量级检查索引状态（不触发实际检索）
        if not self.doc_manager.is_index_ready():
            logger.info(f"文档索引仍在更新中，延迟重试队列: {len(self.pending_retry_queue)} 条")
            return

        # 索引已完成，重试队列中的消息
        logger.info(f"📚 文档索引已完成，开始重试 {len(self.pending_retry_queue)} 条延迟消息")

        retry_count = 0
        while self.pending_retry_queue:
            timestamp, event_data = self.pending_retry_queue.pop(0)
            age = now - timestamp
            logger.info(f"重试延迟消息（已等待 {age:.0f}s）")

            # 重新入队处理
            try:
                self.event_queue.put(event_data, timeout=1)
                retry_count += 1
            except queue.Full:
                logger.warning("事件队列已满，剩余延迟消息暂不重试")
                self.pending_retry_queue.insert(0, (timestamp, event_data))  # 放回队列
                break

        if retry_count > 0:
            logger.info(f"✅ 已重新入队 {retry_count} 条延迟消息")

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
            # 根据适配器类型判断来源
            adapter_class_name = adapter.__class__.__name__
            if adapter_class_name == "WeChatAdapter":
                source = "wechat"
            elif adapter_class_name == "FeishuBotAdapter":
                source = "feishu"
            else:
                source = "unknown"

            record_id = None
            metadata = {}
            try:
                reply, record_id, metadata = self.ai_provider.call(context_messages, session_id=session_id, source=source)
            except DocIndexingInProgressError:
                # 文档索引更新中，直接返回提示（Web/微信不支持自动重试）
                logger.info(f"文档索引更新中: session={session_id}, source={source}")
                reply = "📚 文档索引正在更新中，请稍后（约1-2分钟）再试或等待我稍后回复，或者您可以先问我通用技术问题。"

            # 将 AI 回复添加到上下文
            ai_message = Message(
                role="assistant",
                content=[Content(type="text", data=reply)],
                timestamp=datetime.now(),
                metadata=metadata  # 保存模式信息（如 mode: "agentic"）
            )
            self.context_manager.add_message(session_id, ai_message)

            # 执行回复（adapter_class_name 已在上面获取）
            if adapter_class_name == "FeishuBotAdapter":
                # 统一走 _send_feishu_reply，带反馈按钮 + record_id 映射
                # （注：飞书为 webhook 驱动，此轮询分支实际不会命中，仅保证一致性）
                latest = getattr(adapter, "latest_message", None)
                if latest and latest.get("message_id"):
                    # 注：轮询模式没有精确的 ai_duration，传 None
                    self._send_feishu_reply(
                        adapter, latest["message_id"], session_id, reply, record_id, None, metadata
                    )
                    adapter.clear_latest_event()
                    logger.info("Reply sent via Feishu Bot API successfully")
                else:
                    logger.error("Failed to send reply via Feishu Bot API: no latest_message")
            elif adapter_class_name == "WeChatAdapter":
                if adapter.send_message(reply):
                    logger.info("Reply sent to WeChat successfully")
                else:
                    logger.error("Failed to send reply to WeChat")

        except Exception as e:
            logger.error(f"Failed to handle trigger: {e}")


def main():
    """主入口函数"""
    # 设置全局异常处理器（捕获所有未处理的异常）
    def global_exception_handler(exc_type, exc_value, exc_traceback):
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc_value, exc_traceback)
            return
        logger.critical(
            "Uncaught exception",
            exc_info=(exc_type, exc_value, exc_traceback)
        )

    sys.excepthook = global_exception_handler

    try:
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

    except KeyboardInterrupt:
        logger.info("Received KeyboardInterrupt, exiting...")
        sys.exit(0)
    except Exception as e:
        logger.critical(f"Fatal error in main(): {e}", exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
