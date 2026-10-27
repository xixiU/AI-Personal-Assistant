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
import re
import queue
import threading
import requests
import signal
import atexit
from typing import Optional, List
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

    # 附件消息无文字说明时的兜底提问。只在延迟等待窗口超时后使用——
    # 解析阶段不要补，否则延迟判断的 `not text` 恒为假（见 _process_event）。
    _DEFAULT_ATTACHMENT_QUESTION = "请分析这个日志文件中的问题。"

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

        # 配置 AI 调用失败告警：额度耗尽、API Key 失效、限流、超时等故障，
        # 用户侧只看到一句"调用失败"，需要主动推送给维护者（复用同一个 alert.webhook）。
        from ai_assistant.utils.ai_alert import configure_ai_alert
        configure_ai_alert(
            enabled=getattr(self.config, 'alert_ai_error_enabled', True),
            webhook=getattr(self.config, 'alert_webhook', None),
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

        # 初始化运维功能（如果启用）
        if hasattr(self.config, 'operations') and self.config.operations:
            try:
                from ai_assistant.core.operations_manager import OperationsManager
                from ai_assistant.tools.operations_tools import OperationsTools

                # 使用 from_config 工厂方法创建 OperationsManager
                self.operations_manager = OperationsManager.from_config(self.config.operations)
                self.operations_tools = OperationsTools(operations_manager=self.operations_manager)

                # 注入到 AI Provider
                if hasattr(self.ai_provider, 'set_operations_tools'):
                    self.ai_provider.set_operations_tools(self.operations_tools, enabled=True)
                    logger.info("运维操作功能已启用")

                    # 测试SSH连接
                    logger.info("开始测试SSH连接...")
                    test_results = self.operations_manager.test_connections()

                    # 输出测试摘要
                    success_count = sum(1 for r in test_results.values() if r["success"])
                    total_count = len(test_results)

                    if success_count == total_count:
                        logger.info(f"✅ SSH连接测试: 全部成功 ({success_count}/{total_count})")
                    elif success_count > 0:
                        logger.warning(f"⚠️  SSH连接测试: 部分成功 ({success_count}/{total_count})")
                        for name, result in test_results.items():
                            if not result["success"]:
                                logger.warning(f"  - {name}: {result['message']}")
                    else:
                        logger.error(f"❌ SSH连接测试: 全部失败 ({success_count}/{total_count})")
                        for name, result in test_results.items():
                            logger.error(f"  - {name}: {result['message']}")
                else:
                    logger.warning("当前 AI Provider 不支持运维功能")
            except Exception as e:
                logger.error(f"初始化运维功能失败: {e}")
                logger.warning("运维功能将不可用")
                import traceback
                logger.debug(f"详细错误: {traceback.format_exc()}")

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

        # 引用回查到的日志附件缓存: file_key -> log_file 类型的 Content。
        # 同一条引用链会被反复追问（用户引用机器人回答继续问），每轮都回查到同一个
        # file_key，不缓存就是每轮重下一遍几百 KB 的日志包。文件已落地磁盘，这里只
        # 缓存"落地路径 + 截断后正文"这个 Content，内存有界。
        self._quoted_attachment_cache = {}
        self._quoted_cache_lock = threading.Lock()
        # 上限保护，防长跑内存无界增长（同 feishu_bot._updated_fp_max_keys 的做法）
        self._quoted_cache_max_keys = 200

        # 文档索引更新期间的延迟重试队列
        # 格式: [(timestamp, event_data), ...]
        self.pending_retry_queue = []
        self._last_indexing_check_time = 0  # 上次检查索引状态的时间

        # 日志文件管理器：用户上传 .log/.txt/zip 时落地到本地并注入 AI 上下文，
        # 带日志文件的消息自动进入代码排查模式。需在 _init_adapters 之前建好，
        # 适配器构造时要拿到它。
        from ai_assistant.utils.log_files import LogFileManager
        self.log_file_manager = LogFileManager(
            enabled=getattr(self.config, "log_files_enabled", True),
            base_dir=getattr(self.config, "log_files_dir", "./data/log_files"),
            max_size_mb=getattr(self.config, "log_files_max_size_mb", 20),
            inline_chars=getattr(self.config, "log_files_inline_chars", 20000),
            cleanup_hour=getattr(self.config, "log_files_cleanup_hour", 23),
            retention_hours=getattr(self.config, "log_files_retention_hours", 24),
            allowed_ext=getattr(self.config, "log_files_allowed_ext", None),
        )
        self.log_file_manager.start_cleanup_scheduler()

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
                # 注入历史消息回溯配置（支持"先发图、后发文字"场景）
                bot_config["lookback_count"] = self.config.context_feishu_lookback_count
                # 注入日志文件管理器（用户上传 .log/.txt/zip 时落地并注入上下文）
                bot_config["log_file_manager"] = self.log_file_manager
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
            # 同时注入日志文件管理器：Web 端上传 .log/.zip 附件依赖它落地与读取，
            # 不注入时带 files 的请求会被明确拒绝（而非静默丢附件）。
            self.webhook_server.set_ai_components(
                self.ai_provider, self.context_manager, self.log_file_manager
            )

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

            # 编辑事件标记：适配器把 im.message.updated_v1 归一化成 receive_v1 同构结构时
            # 打上此标记。在解析之后统一注入，避免在 _parse_feishu_event 的 5 个返回分支里
            # 各写一遍（漏一个就会让该类型的编辑退化成普通新消息，重复答且污染历史）。
            is_updated = bool(
                (processed_event.get("event") or {}).get("_is_message_updated")
            )
            parsed["is_updated"] = is_updated
            # 打印解析结果时，图片只打大小+类型，不打 base64 本体（避免日志刷屏）
            _parsed_log = dict(parsed)
            _img = _parsed_log.get("image_data")
            if isinstance(_img, dict):
                _b64 = _img.get("data", "")
                _parsed_log["image_data"] = (
                    f"<image {_img.get('media_type', '?')}, ~{int(len(_b64) * 3 / 4)}B>"
                )
            # 日志文件同样必须脱敏（防回归）：Content 是 dataclass，默认 repr 会把
            # data["text"] 整段打出来——用户传一份几万字的 app.log，DEBUG 级别下
            # 整份日志原文就落进了日志文件，既违反「不得记录完整用户消息」也会刷屏。
            _logs = _parsed_log.get("log_contents")
            if _logs:
                _parsed_log["log_contents"] = [
                    c.summary() if hasattr(c, "summary") else str(c) for c in _logs
                ]
            logger.debug("parsed message:{}", _parsed_log)

            session_id = parsed["chat_id"]
            text = parsed["text"]
            image_data = parsed.get("image_data")
            message_id = parsed["message_id"]
            user_id = parsed.get("sender_id", "unknown")

            # 上下文关联 key（历史注入维度，独立于 session_id）：
            #   - 用户「引用」了某条消息 → 用 root_id（整条引用链共享历史，实现"引用即延续话题"）
            #     机器人用 reply API 回复用户消息 A 时，引用链根就是 A；用户之后引用机器人回复
            #     再追问，其 root_id 仍指向 A —— 于是与第 1 轮存历史用的 key(=message_id A) 对齐。
            #   - 未引用 → 用本条 message_id：每条消息都是独立 key，get_context 必然返回空，
            #     即"全新对话、零历史、从头开始"，彻底杜绝跨话题/跨用户的历史粘连。
            # session_id（=chat_id 群级）保持不变，仍用于并发锁/去重/思考表情/回复发送等房间级逻辑。
            root_id = parsed.get("root_id", "")
            context_key = root_id if root_id else message_id
            logger.info(
                f"上下文 key: {context_key} "
                f"({'引用链 root_id' if root_id else '无引用→独立 message_id（全新对话）'})"
            )

            # 保存原始 text 用于运维模式检测（需要保留 @ 提及来识别命令）
            original_text = text

            # 本条自带的日志文件附件（解析阶段已下载落地）
            own_logs = parsed.get("log_contents") or []

            # 取回该 session 暂存的纯附件（先附件后文字场景），必要时本条也进等待窗口。
            # pop 与 defer 必须成对、顺序正确，故收口在一个方法里（见其 docstring）。
            pending_image, pending_logs, deferred = self._take_pending_and_defer(
                session_id, image_data, own_logs, text, message_id
            )
            if deferred:
                # 等待期内收到后续消息，附件已被该消息取走一并处理
                return

            # 等待窗口已过（或未启用等待）仍无文字说明时，给日志文件补一句默认提问，
            # 避免 text 为空导致 AI 无从下手。必须放在延迟判断之后：解析阶段就补会让
            # 上面的 `not text` 恒为假，"先发文件后发问题"直接失效（本次修复的回归）。
            if not text and (own_logs or pending_logs):
                text = self._DEFAULT_ATTACHMENT_QUESTION
                logger.info("附件消息无文字说明，使用默认提问")

            # 防重复：同一 session 已有请求在处理中，将消息加入上下文
            # 注意：不能无条件丢弃，否则用户"先发图、AI 处理中再发文字"时，
            # 这条文字会被吞掉，永远得不到回答。
            with self._processing_lock:
                if session_id in self._processing_sessions:
                    # 构建消息内容
                    content_parts = []
                    # 先附件后文场景：先并入暂存的图片/日志文件，再并入当前消息自带的图/文
                    if pending_image is not None:
                        content_parts.append(Content(type="image", data=pending_image))
                    if image_data:
                        content_parts.append(Content(type="image", data=image_data))
                    for _lc in pending_logs:
                        content_parts.append(_lc)
                    for _lc in own_logs:
                        content_parts.append(_lc)
                    if text:
                        content_parts.append(Content(type="text", data=text))

                    if content_parts:
                        user_message = Message(
                            role="user",
                            content=content_parts,
                            timestamp=datetime.now()
                        )
                        # 历史入库用 context_key（引用链维度），与主流程一致
                        self.context_manager.add_message(context_key, user_message)

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

            # 引用追问时主动回查被引用消息的附件。
            # 这是「群聊里拖文件 → 引用该文件提问」唯一能拿到日志的通路：那条纯文件
            # 消息不会推 webhook（权限只推 @机器人 的消息），机器人从未见过它，
            # pending_logs 恒为空。详见 _fetch_quoted_attachments 的 docstring。
            #
            # 与历史重建正交，不是 fallback：jsonl 的 record 结构不存附件，
            # 历史命中与否和附件拿不拿得到互不相干，两条补全各自独立执行。
            #
            # 本轮已经有附件就不回查：白费一次 API 调用，且用户当前发的附件才是
            # 他想问的东西，引用链上的旧附件此时不该再抢位置。
            #
            # 为什么上面那条"session 处理中"分支（:518 起）不回查也不丢附件
            # ——依据必须写准，这个耦合很脆：
            #   回查的触发条件要求 `text` 非空 → 落到那条分支的消息必然带文字
            #   → 必然命中 :545 的 `if text:` 把 event_data 登记进
            #   _deferred_messages → finally 里必然补跑一次，补跑时走完整路径
            #   并执行这里的回查（同一个 file_key 命中缓存，不重复下载）。
            # 注意不是"那条分支的消息都会补跑"：:551 的无文字分支只入上下文、
            # 直接 return、**不补跑**。所以哪天有人放宽回查条件允许无文字触发，
            # 这条路径就会静默丢附件 —— 放宽前必须先让无文字分支也能补跑。
            quoted_logs = []
            quoted_image = None
            if (
                not image_data
                and not own_logs
                and not pending_logs
                and pending_image is None
                and text
                and parsed.get("parent_id")
            ):
                quoted_logs, quoted_image, quoted_notice = self._fetch_quoted_attachments(
                    adapter,
                    parsed.get("parent_id", ""),
                    root_id,
                    session_id,
                )
                # 回查失败不静默：把说明并进文本，让 AI 据实告知用户，
                # 而不是以为"用户只发了文字"去凭空作答。
                if quoted_notice:
                    text = f"{text} {quoted_notice}".strip()
                if quoted_logs or quoted_image is not None:
                    logger.info(
                        "引用回查附件已并入本轮: {}",
                        [c.summary() for c in quoted_logs]
                        + ([f"image({quoted_image.get('media_type', '?')})"]
                           if quoted_image else []),
                    )

            # 构建用户消息（支持图文）
            # 先图后文场景：pending_image 是上一条纯图片消息暂存的图片，随本条一并注入，
            # 与当前消息归到同一个 context_key，杜绝写入键/读取键不一致导致的丢图。
            content_parts = []
            if pending_image is not None:
                content_parts.append(Content(type="image", data=pending_image))
            if image_data:
                content_parts.append(Content(type="image", data=image_data))
            if quoted_image is not None:
                content_parts.append(Content(type="image", data=quoted_image))
            # 日志文件附件（已在解析阶段下载落地），与图片同样触发代码排查模式。
            # pending_logs 来自上一条纯文件消息的暂存（先发文件、紧接着发问题），
            # own_logs 是本条消息自带的附件，quoted_logs 来自引用链回查，三者都要并入。
            # 顺序约束（防回归）：必须全部排在下方 text 的 Content 之前，与
            # pending/own 既有顺序一致 —— provider 侧按内容块顺序拼 prompt，
            # 附件在问题之后会让"先给材料再提问"的结构倒置。
            for _lc in pending_logs:
                content_parts.append(_lc)
            for _lc in own_logs:
                content_parts.append(_lc)
            for _lc in quoted_logs:
                content_parts.append(_lc)

            # 在构建消息前，先用原始 text 检测运维模式，然后再清理 @ 提及
            is_operations_mode = original_text and ("/运维" in original_text or "/ops" in original_text)

            # 去掉飞书 @ 占位符（如 @_user_1），避免污染 AI 输入和检索关键词
            if text:
                text = re.sub(r"@_user_\d+\s*", "", text).strip()
                content_parts.append(Content(type="text", data=text))

            # 内容为空时不入上下文（例如纯图片已提前入过），避免产生空消息
            user_message = None
            if content_parts:
                user_message = Message(
                    role="user",
                    content=content_parts,
                    timestamp=datetime.now()
                )
                # 发言人身份 metadata 一律注入，不再只在带 /运维 前缀时注入。
                # 原先用 is_operations_mode（只认当前文本里的 /运维、/ops）作为开关，
                # 但运维模式还能由「引用链继承」触发：用户引用上一轮运维回答追问
                # 「用你建议的方式再查一遍」时，文本里没有前缀 → 不注入 metadata →
                # provider 侧倒序找不到身份 → user_id=unknown、chat_id=None →
                # 即使人在授权群里也被判无权限（本次修复的 bug）。
                # 身份是「谁在说话」这一客观事实，与本轮走哪个模式无关，故无条件注入；
                # 鉴权仍在 provider 侧按当前发言人校验，不因此放宽任何权限。
                user_message.metadata = {
                    "operator_id": parsed.get("sender_id", ""),
                    "operator_name": parsed.get("sender_name", ""),
                    "operator_display_name": parsed.get("sender_display_name", ""),
                    "chat_id": parsed.get("chat_id", ""),  # 群组ID用于群组白名单
                    "source": "feishu"
                }
                if is_operations_mode:
                    logger.info(f"已注入运维 metadata: {user_message.metadata}")
                # 编辑重答（is_updated）先清掉该 key 的旧内存上下文：
                # 编辑的是同一条消息，context_key 与首次回答时相同，若直接 append，
                # AI 会同时看到"改错前的问题 + 上次的错答案 + 改对后的问题"，
                # 被旧答案带偏，且同一问题在上下文里出现两遍。编辑语义是"这条消息现在
                # 应该是这个内容"，不是追加一轮对话，所以清空后重建为唯一一轮。
                if parsed.get("is_updated"):
                    self.context_manager.clear_context(context_key)
                    logger.info(f"编辑重答：已清空该 key 的旧上下文，按新内容独立回答: {context_key}")
                # 历史入库仍写内存（保留短窗口能力，兼容无引用的极短连续追问）
                self.context_manager.add_message(context_key, user_message)

            # 构建注入给 AI 的上下文消息：
            #   - 有引用(root_id) → 路径 B：从持久化 chat_history 按 context_key 重建整条
            #     引用链的完整问答，作为唯一真相源。不依赖内存窗口（内存有 max_messages=10
            #     截断 + 会话过期 + 重启易失三大限制，无法保证"完整历史"）。
            #   - 无引用 → 全新对话：内存该 key 必为空，context_messages 只含当前这条，零历史。
            if root_id and self.chat_history:
                context_messages = self._rebuild_context_from_chain(
                    context_key,
                    user_message,
                    # 编辑重答时排除被编辑消息自己的旧轮次，避免新旧两版同时进上下文
                    exclude_message_id=message_id if parsed.get("is_updated") else "",
                )
            else:
                context_messages = self.context_manager.get_context(context_key)
            logger.info(f"Sending {len(context_messages)} messages to AI (context_key={context_key})")

            # 图片链路诊断日志：逐条打印将送入 AI 的消息 content 组成，快速定位图片在哪一步丢失。
            # 关键排查项：本轮若发了图，这里必须出现 image(...) 块；若只剩 text，则图在
            # 消息构建/上下文读取阶段已丢，而非 provider 转换阶段。摘要不含 base64 本体。
            for _i, _m in enumerate(context_messages):
                logger.info(f"  [→AI] msg#{_i} {_m.summary()}")

            # 引用链排查字段：随历史落盘，便于核对飞书 root_id 行为
            chain_info = {
                "message_id": message_id,
                "parent_id": parsed.get("parent_id", ""),
                "root_id": root_id,
            }

            # 调用 AI 生成回复
            ai_start = time_mod.time()
            record_id = None
            metadata = {}
            try:
                reply, record_id, metadata = self.ai_provider.call(
                    context_messages,
                    session_id=session_id,
                    source="feishu",
                    context_key=context_key,
                    chain_info=chain_info,
                )
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
            # AI 回复入库同样用 context_key，保证下一轮引用能取到本轮问答
            self.context_manager.add_message(context_key, ai_message)

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

    def _rebuild_context_from_chain(
        self,
        context_key: str,
        current_user_message,
        exclude_message_id: str = "",
    ) -> List[Message]:
        """
        路径 B：按引用链标识从持久化 chat_history 重建整条链的完整历史。

        用于"用户引用某条消息追问"场景。从 jsonl 里捞出该 context_key 的全部历史轮
        （每条含 query/answer），按时间升序展开成交替的 user/assistant 消息，最后接上
        当前这条新提问。不依赖内存 ContextManager，因此不受 max_messages 截断、
        会话过期、进程重启影响 —— 这正是"注入整条链所有对话与回答"的落地。

        Args:
            context_key: 引用链标识（飞书 root_id）
            current_user_message: 当前这轮用户消息（Message 对象；内容为空时可能为 None）
            exclude_message_id: 需要排除的飞书 message_id（编辑重答时传被编辑那条）。
                编辑的是同一条消息，它此前那轮"旧问题+旧答案"已落盘在同一条链上，
                若一并注入，AI 会同时看到改错前后的两个版本并被旧答案带偏。

        Returns:
            重建后的消息列表：[历史 user/assistant 交替...] + [当前 user 消息]
        """
        rebuilt: List[Message] = []
        try:
            records = self.chat_history.get_records_by_context_key(context_key)
        except Exception as e:
            logger.warning(f"按引用链重建历史失败，回退到当前消息: {e}")
            records = []

        if exclude_message_id:
            before = len(records)
            records = [
                r for r in records
                if (r.get("chain_info") or {}).get("message_id") != exclude_message_id
            ]
            if before != len(records):
                logger.info(
                    f"编辑重答：已从链历史中剔除被编辑消息的旧轮次 "
                    f"{before - len(records)} 条: message_id={exclude_message_id}"
                )

        for rec in records:
            query = (rec.get("query") or "").strip()
            answer = (rec.get("answer") or "").strip()
            # 去掉飞书 @ 占位符，保持与实时消息一致的干净文本
            if query:
                query = re.sub(r"@_user_\d+\s*", "", query).strip()
            if query:
                rebuilt.append(Message(
                    role="user",
                    content=[Content(type="text", data=query)],
                    timestamp=datetime.now(),
                ))
            if answer:
                # 把该轮落盘的 metadata（含 mode）一并带回：模式继承判定读它来识别
                # "这条链此前用过 agentic/operations"，比重新扫描历史文本猜测更准
                # （mode 是实际执行结果，而文本里出现"查代码"未必真进过工具模式）。
                rebuilt.append(Message(
                    role="assistant",
                    content=[Content(type="text", data=answer)],
                    timestamp=datetime.now(),
                    metadata=rec.get("metadata") or {},
                ))

        # 末尾追加当前这轮新提问（尚未落盘，链里不含，不会重复）
        if current_user_message is not None:
            rebuilt.append(current_user_message)

        logger.info(
            f"引用链重建: context_key={context_key}, 命中历史 {len(records)} 轮, "
            f"注入消息 {len(rebuilt)} 条"
        )
        return rebuilt

    def _fetch_quoted_attachments(
        self,
        adapter,
        parent_id: str,
        root_id: str,
        chat_id: str,
    ) -> tuple:
        """
        回查被引用消息的附件（引用文件消息追问场景的唯一通路）

        为什么必须主动回查（这是整条链路的根因，改动前请先读懂）：
        群聊里机器人的权限是 im:message.group_at_msg:readonly，只能收到 @机器人
        的消息。用户拖入文件时飞书客户端立即发送，那条纯文件消息**根本不会推
        webhook 事件** —— 机器人对它毫无感知，`_defer_lone_attachment` 没有机会
        执行，`pending_logs` 恒为空。用户随后引用该文件消息 @机器人提问时，机器人
        只收到这一个事件，事件里只有 parent_id / root_id 两个 ID，不内联被引用
        消息的内容。所以附件只能靠这两个 ID 回查拿到。

        与历史重建是两个**正交**的补全，不是互为 fallback（防回归）：
        chat_history 的 jsonl record 结构里压根不存附件（见 core/chat_history.py），
        所以「历史命中几轮」与「附件能不能拿到」是两件独立的事。历史照旧由
        _rebuild_context_from_chain 按 context_key 从 jsonl 重建，附件单独走这里。
        若把回查写成「历史没命中才回查」，历史命中时附件就永远拿不到了。

        parent / root 的取舍（最多两次回查，不做无界向上爬）：
        - 首轮「引用文件消息提问」：文件消息自身不是回复，parent_id == root_id
          == 文件消息，查 parent 一次命中（root 会被相等判断跳过，不多花调用）。
        - 追问轮「引用机器人回答再追问」：parent_id 是机器人那条纯文本回答（无附件），
          root_id 才是最初的文件消息 —— 这时查 root 是拿到日志的唯一通路。
        - 链中间节点都是「机器人回答 / 用户追问」这类纯文本，附件只可能出现在
          直接引用点或链根，继续向上爬收益为零而每跳一次都是一次网络往返，
          长链路下延迟不可控，故硬性封顶两次。

        Args:
            adapter: 飞书适配器（需提供 get_message / _collect_log_files / download_image）
            parent_id: 直接被引用的消息 ID
            root_id: 引用链根消息 ID
            chat_id: 当前会话 ID（日志文件按会话分目录落地）

        Returns:
            (log_contents, image_data, notice) 三元组：
              log_contents 为 log_file 类型的 Content 列表；
              image_data 为图片 dict 或 None；
              notice 为回查失败时给 AI 的说明文本（成功或无附件时为空串）
        """
        import json as json_mod

        if not hasattr(adapter, "get_message"):
            # 非飞书机器人适配器（如 UI 自动化版）没有回查能力，安静跳过
            return [], None, ""

        # 候选 ID：parent 优先，root 兜底。去重 + 去空，天然封顶两次。
        candidates = [mid for mid in (parent_id, root_id) if mid]
        candidates = list(dict.fromkeys(candidates))

        for index, message_id in enumerate(candidates):
            item = adapter.get_message(message_id)
            if not item:
                # get_message 内部已按错误码打过 error 日志，这里只给链路上下文
                logger.warning(
                    f"引用回查：消息详情取不到，跳过该节点 "
                    f"(第 {index + 1}/{len(candidates)} 个: {message_id})"
                )
                continue

            msg_type = item.get("msg_type") or ""
            content_str = (item.get("body") or {}).get("content") or "{}"
            try:
                content_data = json_mod.loads(content_str)
            except (ValueError, TypeError) as e:
                logger.error(f"引用回查：content 不是合法 JSON: message_id={message_id}, {e}")
                continue

            # 复用适配器既有解析能力，不另写一套递归 walk：
            # _parse_message_content 已覆盖 text/image/file/post 四种类型
            text, image_keys, files = adapter._parse_message_content(msg_type, content_data)

            if not image_keys and not files:
                # 被引用的是纯文本（最常见：引用机器人上一轮回答追问）。
                # 这是正常情况而非故障，用 debug 级别，不刷 error 日志。
                logger.debug(
                    f"引用回查：该节点无附件，继续看下一个 "
                    f"(message_id={message_id}, msg_type={msg_type}, text_len={len(text)})"
                )
                continue

            log_contents, failed_files = self._collect_quoted_log_files(
                adapter, message_id, files, chat_id
            )

            # 图片不入缓存：base64 本体一张截图就几百 KB，200 条上限即上百 MB，
            # 而「引用一张图反复追问」远少于「引用一份日志反复追问」，不值得担这个
            # 内存风险，走每次重新下载（与当前轮直接发图的行为一致）。
            image_data = None
            if image_keys:
                if len(image_keys) > 1:
                    logger.info(f"引用回查：该节点含 {len(image_keys)} 张图片，当前仅取第一张")
                image_data = adapter.download_image(message_id, image_keys[0])
                if not image_data:
                    logger.error(
                        f"引用回查：图片下载失败 "
                        f"(message_id={message_id}, image_key={image_keys[0]})"
                    )

            if log_contents or image_data:
                logger.info(
                    f"引用回查命中附件: message_id={message_id} "
                    f"({'parent_id' if message_id == parent_id else 'root_id'}), "
                    f"日志块={len(log_contents)}, 图片={'有' if image_data else '无'}"
                )
                # 部分文件失败时也要说明，不静默：用户发了 3 个日志只读到 2 个，
                # AI 不知情就会按"全都看过了"作答。
                notice = ""
                if failed_files:
                    notice = (
                        f"（用户引用的消息里有 {len(failed_files)} 个附件未能读取"
                        f"：{'、'.join(failed_files)}）"
                    )
                return log_contents, image_data, notice

            # 有附件标识但全军覆没（下载失败 / 类型不支持 / 超过大小上限）。
            # 必须出声：否则 AI 以为用户只发了文字，会凭空猜一个答案。
            notice = (
                f"（用户引用的消息里带了 {len(files) + len(image_keys)} 个附件，"
                f"但服务端未能读取其内容）"
            )
            logger.error(
                f"引用回查：该节点有附件标识但全部读取失败 "
                f"(message_id={message_id}, files={len(files)}, images={len(image_keys)})"
            )
            return [], None, notice

        logger.info(
            f"引用回查：链上 {len(candidates)} 个节点均无附件 "
            f"(parent_id={parent_id}, root_id={root_id})"
        )
        return [], None, ""

    def _collect_quoted_log_files(
        self,
        adapter,
        message_id: str,
        files: List[dict],
        chat_id: str,
    ) -> tuple:
        """
        下载被引用消息的日志附件，按 file_key 做进程内缓存

        同一条引用链会被反复追问（用户引用机器人回答继续问），每轮回查到的
        file_key 都一样，不缓存就是每轮重下一遍整包日志。

        缓存命中时必须校验落地文件仍存在（防回归）：LogFileManager 有
        retention_hours 定时清理，缓存住一个已被清掉的路径，会让 AI 拿着不存在
        的路径去用代码排查工具读文件，读不到还以为是日志本身有问题。

        Args:
            adapter: 飞书适配器（提供 _collect_log_files）
            message_id: 附件所属消息的 ID。**必须是被引用那条消息的 ID**，
                不是当前提问消息的 ID —— resources 下载接口按"附件挂在哪条消息"
                鉴权，传错 ID 会 404。
            files: [{"file_key","file_name"}] 列表
            chat_id: 当前会话 ID（落地分目录）

        Returns:
            (contents, failed_names)：成功的 log_file Content 列表，
            以及未能读取的文件名列表（供上层生成说明文本）
        """
        contents: List[Content] = []
        failed_names: List[str] = []
        pending: List[dict] = []

        manager = getattr(self, "log_file_manager", None)

        for item in files:
            file_key = item.get("file_key") or ""
            file_name = item.get("file_name") or "attachment"

            if not file_key:
                failed_names.append(file_name)
                continue

            # 扩展名白名单先于缓存/下载判断：不支持的类型连下载都不该发起
            if manager and not manager.is_allowed(file_name):
                logger.info(f"引用回查：跳过非日志类附件: {file_name}")
                failed_names.append(file_name)
                continue

            with self._quoted_cache_lock:
                cached = self._quoted_attachment_cache.get(file_key)
            if cached is not None:
                cached_path = (cached.data or {}).get("path") or ""
                # 路径为空是"超限/保存失败"的说明块，本身就有效，照常复用
                if not cached_path or os.path.exists(cached_path):
                    logger.info(f"引用回查：命中附件缓存，跳过下载: {file_name}")
                    contents.append(cached)
                    continue
                logger.info(
                    f"引用回查：缓存的落地文件已被定时清理，重新下载: "
                    f"{file_name} (path={cached_path})"
                )
                # 必须真的删掉失效条目，不能只是跳过它去下载（防回归）：
                # 留着旧条目的话，下面重下虽然会按同一个 file_key 覆盖写回，
                # 但一旦哪天改成"仅在 key 不存在时写入"，就会变成每轮追问都
                # 走一遍"命中→校验失败→重下"，缓存等于失效。
                with self._quoted_cache_lock:
                    self._quoted_attachment_cache.pop(file_key, None)

            pending.append(item)

        if pending:
            # 下载/落地/解压/截断全部复用适配器既有实现（含大小上限与 zip 处理）
            fetched = adapter._collect_log_files(message_id, pending, chat_id)
            if len(fetched) < len(pending):
                # _collect_log_files 对下载失败的条目直接跳过，数量差即失败数。
                # 无法一一对应回文件名，只能按尾部补齐，够用于生成说明文本。
                missing = len(pending) - len(fetched)
                failed_names.extend(
                    (it.get("file_name") or "attachment") for it in pending[-missing:]
                )
                logger.error(
                    f"引用回查：{missing} 个附件下载或落地失败 "
                    f"(message_id={message_id})"
                )
            # fetched 与 pending 顺序一致，按位回填 file_key 建缓存。
            # 直接赋值（不是 setdefault）：失效后重下的条目要覆盖掉旧的，
            # 否则重下的新路径进不了缓存，下一轮又得重下一次。
            for item, content in zip(pending, fetched):
                contents.append(content)
                with self._quoted_cache_lock:
                    self._quoted_attachment_cache[item.get("file_key")] = content
                    # 上限保护：超限丢弃最早插入的键（dict 保持插入序）
                    if len(self._quoted_attachment_cache) > self._quoted_cache_max_keys:
                        oldest = next(iter(self._quoted_attachment_cache))
                        del self._quoted_attachment_cache[oldest]

        return contents, failed_names

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

            # 引用/回复关系（引用链方案的地基）：
            # 飞书「回复某条消息」时，message 事件会带 parent_id（被直接引用的那条消息 ID）
            # 和 root_id（整条引用链的根消息 ID）。普通发言不带这两个字段。
            # 当前仅提取并打日志用于实测确认报文结构，暂不改变 session 逻辑。
            parent_id = message.get("parent_id", "")
            root_id = message.get("root_id", "")
            if parent_id or root_id:
                logger.info(f"🔗 引用消息: message_id={message_id}, parent_id={parent_id}, root_id={root_id}")

            # 提取发送者详细信息（用于运维模式鉴权）
            sender_name = sender.get("sender_id", {}).get("user_id", "")  # 飞书用户ID
            sender_display_name = ""  # 可以从 adapter 获取，这里先留空

            # 白名单检查
            if adapter:
                if adapter.allowed_chats and chat_id not in adapter.allowed_chats:
                    logger.info(f"❌ Chat {chat_id} not in whitelist, skipping")
                    return None
                if adapter.allowed_users and sender_id not in adapter.allowed_users:
                    logger.info(f"❌ User {sender_id} not in whitelist, skipping")
                    return None

            # @所有人 判定统一收口在类型分发之前：
            # 早期只在 text 分支里查文本中的 "@_all"，富文本(post)/图片/文件分支完全没查，
            # 而富文本的 @所有人 是独立的 at 节点、文本里没有 @_all 痕迹，
            # 导致"别人用富文本 @所有人"时机器人照常回复（本次修复的 bug）。
            if self.config.trigger_ignore_mention_all and self._is_mention_all(message):
                logger.info(f"忽略 @所有人 的消息（根据配置）: message_type={message_type}")
                return None

            # 处理文本消息
            if message_type == "text":
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
                    "parent_id": parent_id,
                    "root_id": root_id,
                    "sender_id": sender_id,
                    "sender_name": sender_name,
                    "sender_display_name": sender_display_name,
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
                    # 引用链字段必须带上，否则"引用上一轮 + 发截图"追问会退化成全新对话
                    "parent_id": parent_id,
                    "root_id": root_id,
                    "sender_id": sender_id,
                    "sender_name": sender_name,
                    "sender_display_name": sender_display_name,
                    "image_data": image_data
                }

            # 处理富文本消息（文字+图片）
            elif message_type == "post":
                content_str = message.get("content", "{}")
                content_data = json_mod.loads(content_str)

                text, image_keys, files = self._parse_post_content(content_data)

                # 解析不出任何内容时，跳过 AI 调用（否则会发出空消息，触发 400）
                if not text and not image_keys and not files:
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

                # 下载富文本内嵌的日志文件附件（用户常"文件 + 文字说明"一起发，
                # 飞书会打包成 post 消息，附件以 media/file 标签内嵌）
                log_contents = []
                # 附件未能处理时，往上下文里补一句说明，让 AI 能明确告知用户，
                # 而不是基于"只有文字"作答（项目原则：不静默丢弃附件）
                attachment_notice = ""
                if files:
                    manager = getattr(self, "log_file_manager", None)
                    if not adapter:
                        logger.error("富文本消息含文件但 adapter 为空，无法下载")
                        attachment_notice = f"（用户发送了 {len(files)} 个附件，但服务端无法下载）"
                    elif not manager or not manager.enabled:
                        logger.info(f"日志文件处理未启用，跳过富文本中的 {len(files)} 个附件")
                        attachment_notice = (
                            f"（用户发送了 {len(files)} 个附件，但服务端未启用日志文件处理，无法读取）"
                        )
                    else:
                        allowed_files = [
                            item for item in files
                            if manager.is_allowed(item.get("file_name") or "attachment")
                        ]
                        skipped = len(files) - len(allowed_files)
                        if skipped:
                            logger.info(f"富文本中跳过 {skipped} 个非日志类附件")
                            attachment_notice = (
                                f"（用户发送了 {skipped} 个附件，类型不在支持范围内"
                                f"——仅支持 .log/.txt/.out/.err/.zip）"
                            )
                        if allowed_files:
                            log_contents = adapter._collect_log_files(
                                message_id, allowed_files, chat_id
                            )
                            if not log_contents:
                                logger.error(
                                    f"富文本中 {len(allowed_files)} 个日志文件处理失败，"
                                    f"本轮将不带日志内容"
                                )
                                attachment_notice = (
                                    f"（用户发送了 {len(allowed_files)} 个日志文件，"
                                    f"但服务端读取失败）"
                                )

                # 附件读不到时把说明并进文本，让 AI 能据实告知用户
                if attachment_notice:
                    text = f"{text} {attachment_notice}".strip() if text else attachment_notice

                # 图片下载失败且无文本、无日志文件时无内容可发，跳过
                if not text and not image_data and not log_contents:
                    logger.error("富文本消息的图片/文件均不可用且无文本，跳过 AI 调用")
                    return None

                # 注意（防回归）：这里**不要**给无文字的附件消息补默认提问。
                # 补了 text 就非空，_process_event 的延迟等待判断 `not text` 永远为假，
                # "先发文件、紧接着发问题"这条主要用法会直接失效（第一条立即拿默认提问
                # 去问 AI，第二条问题另起一轮且手里没有日志）。
                # 默认提问统一由 _process_event 在延迟窗口超时后补（见 _DEFAULT_ATTACHMENT_QUESTION）。

                logger.info(
                    f"解析富文本消息: text_len={len(text)}, images={len(image_keys)}, "
                    f"image_ready={image_data is not None}, files={len(files)}, "
                    f"log_blocks={len(log_contents)}"
                )

                result = {
                    "chat_id": chat_id,
                    "chat_type": message.get("chat_type", "p2p"),
                    "text": text,
                    "message_id": message_id,
                    # 引用链字段必须带上：缺了这两个，带截图/日志的引用追问会被
                    # 下游判成「无引用→全新对话」，整条引用链历史全丢（本次修复的 bug）
                    "parent_id": parent_id,
                    "root_id": root_id,
                    "sender_id": sender_id,
                    "sender_name": sender_name,
                    "sender_display_name": sender_display_name,
                    "image_data": image_data  # 可能为 None
                }
                if log_contents:
                    result["log_contents"] = log_contents
                return result

            # 处理文件消息（用户上传日志 .log/.txt 或 zip 日志包）
            elif message_type == "file":
                content_str = message.get("content", "{}")
                content_data = json_mod.loads(content_str)
                file_key = content_data.get("file_key", "")
                file_name = content_data.get("file_name") or "attachment"

                if not file_key or not adapter:
                    logger.warning("文件消息但无 file_key 或 adapter")
                    return None

                # 附件读不到时不静默沉默：群聊只接收 @机器人 的消息、纯文件消息到不了
                # 这里，所以走到此处基本都是私聊——用户明确在跟机器人说话，一声不响
                # 会让人以为服务挂了。给 AI 一句说明，让它据实回复。
                manager = getattr(self, "log_file_manager", None)
                notice = ""
                log_contents = []
                if not manager or not manager.enabled:
                    logger.info(f"日志文件处理未启用，跳过文件消息: {file_name}")
                    notice = "（用户发送了一个附件，但服务端未启用日志文件处理，无法读取）"
                elif not manager.is_allowed(file_name):
                    logger.info(f"跳过非日志类附件: {file_name}")
                    notice = (
                        "（用户发送了一个附件，类型不在支持范围内"
                        "——仅支持 .log/.txt/.out/.err/.zip）"
                    )
                else:
                    log_contents = adapter._collect_log_files(
                        message_id, [{"file_key": file_key, "file_name": file_name}], chat_id
                    )
                    if not log_contents:
                        logger.error(f"日志文件处理失败: {file_name}")
                        notice = "（用户发送了一个日志文件，但服务端读取失败）"

                logger.info(
                    f"解析文件消息: name={file_name}, 内容块={len(log_contents)}, "
                    f"notice={'有' if notice else '无'}"
                )
                result = {
                    "chat_id": chat_id,
                    "chat_type": message.get("chat_type", "p2p"),
                    # 附件可读时 text 保持空：补默认提问会让延迟等待的 `not text`
                    # 判断失效，"先发文件后发问题"就走不通。默认提问在延迟窗口
                    # 超时后由主流程统一补上。
                    # 附件读不到时带上 notice，让 AI 能据实告知用户而不是沉默。
                    "text": notice,
                    "message_id": message_id,
                    "parent_id": parent_id,
                    "root_id": root_id,
                    "sender_id": sender_id,
                    "sender_name": sender_name,
                    "sender_display_name": sender_display_name,
                    "image_data": None,
                }
                if log_contents:
                    result["log_contents"] = log_contents
                return result

            else:
                content_preview = str(message.get("content", ""))[:200]
                logger.info(
                    f"跳过非文本/图片/文件消息: {message_type}, content 预览={content_preview}"
                )
                return None

        except Exception as e:
            logger.error(f"Failed to parse feishu event: {e}")
            return None

    def _take_pending_and_defer(
        self,
        session_id: str,
        image_data: Optional[dict],
        own_logs: List[Content],
        text: str,
        message_id: str,
    ) -> tuple:
        """
        取回该 session 暂存的纯附件，并在本条也无文字时进入等待窗口

        图片/文件与文字是两条独立飞书消息，各自 message_id 不同、不共享
        context_key，唯一稳定的桥是 session_id（群 chat_id）。这里把暂存的附件
        本体取出交给调用方，随本条消息一并并入 content_parts（归到当前
        context_key），从根上避免"写入键=session_id、读取键=context_key"
        不一致导致的丢图（488fac5 统一读取键后引入的回归）。

        pop 与 defer 必须成对且顺序正确，所以收口在这一个方法里（防回归）：
        pop 是无条件的，连发两个日志文件时第二条事件会先把第一条暂存的 a.log
        取出来；若只拿 own_logs 去 defer，a.log 就停在调用方的局部变量里而调用方
        紧接着 return —— a.log 被静默丢弃，用户随后提问时 AI 只看到 b.log。
        把 pop 出来的 pending 一起传进 defer，_defer_lone_attachment 内部的合并
        分支才真正生效（在此之前，那段合并代码因调用方总是先清空槽位而是死代码）。

        另一种写法「把 pop 延后到 defer 判断之后」是错的：defer 内部会把已暂存的
        a.log 与本条的 b.log 合并后写回槽位，超时时它 del 掉的是合并后的那份，
        而调用方手里只有 b.log —— a.log 又丢一次。

        三条路径都自洽：
        - 被接管 → 返回 deferred=True，合并后的附件留在槽位等后续文字消息 pop；
        - 超时 → defer 删掉的是它自己写回的那份拷贝，调用方手里的
          pending_image / pending_logs / own_logs 都还在，照常全部并入；
        - 等待期内第三条附件到达 → 它先 pop 走合并结果再 defer，本线程因 ts
          变化返回 True，正确移交。

        Args:
            session_id: 会话 ID（群 chat_id）
            image_data: 本条消息自带的图片数据，可为 None
            own_logs: 本条消息自带的日志文件内容块
            text: 本条消息的文字（为空才进入等待窗口）
            message_id: 飞书消息 ID

        Returns:
            (pending_image, pending_logs, deferred) 三元组：
            前两个是从暂存里取回的附件，deferred=True 表示本条已交由后续消息
            处理、调用方应直接返回
        """
        pending_image = None
        pending_logs: List[Content] = []
        with self._processing_lock:
            popped = self._pending_lone_images.pop(session_id, None)
        if popped is not None:
            pending_image = popped.get("image")
            pending_logs = popped.get("log_contents") or []

        # 纯附件无文字：用户几乎总是分两条发（先发截图/日志文件 → 再描述问题），
        # 飞书客户端拖入文件即发送，根本没法把文件和文字拼成一条消息。
        # 立即调 AI 只能反问，这里短暂等待后续文字消息合并处理。
        #
        # 条件里带上 pending 两项：手里有任何附件且无文字就该等。只看
        # image_data/own_logs 会让「上一条是纯文件、本条图片下载失败」这种情况
        # 直接跳过等待窗口，白白浪费一次合并机会。
        #
        # 图片仍是 `image_data or pending_image` 二选一：暂存结构只有一个 image
        # 槽位、content_parts 也只排两张图，连发两张纯图片本来就存不下第二张，
        # 这是既有设计边界，这里不扩大也不缩小。
        if (image_data or own_logs or pending_image or pending_logs) \
                and not text and self._lone_image_wait > 0:
            if self._defer_lone_attachment(
                session_id,
                image_data or pending_image,
                pending_logs + list(own_logs),
                message_id,
            ):
                return pending_image, pending_logs, True

        return pending_image, pending_logs, False

    def _defer_lone_attachment(
        self,
        session_id: str,
        image_data: Optional[dict],
        log_contents: Optional[List[Content]],
        message_id: str,
    ) -> bool:
        """
        纯附件消息延迟处理：等待用户紧随其后发送的文字描述

        用户几乎总是分两条发送："先发日志截图/日志文件 → 再描述问题"。
        图片可以和文字拼成一条消息，但**飞书客户端拖入文件即刻发送**，
        文件根本没法和文字凑成一条，所以"先文件后文字"是文件场景的主要用法。
        如果收到附件就立即调用 AI，AI 手里没有任何问题描述，只能反问，
        既慢又答不到点上。这里把附件暂存到 _pending_lone_images，等待一个短窗口：
        - 窗口内收到了文字消息 → 主流程 pop 出暂存附件，随那条文字消息一并注入
          content_parts（归到当前 context_key，_should_use_agentic_mode 因附件进入排查模式）
        - 窗口内没有文字 → 返回 False，调用方用手里的附件按纯附件正常处理

        注意（防回归）：不要在这里把附件写进 context_manager。附件与文字是两条独立
        飞书消息、context_key 不同，若在此用 session_id 写上下文，会与主流程用 context_key
        读取上下文不一致，导致附件读不到（历史回归 bug）。暂存附件本体、由文字消息
        取走并入是唯一稳定路径。

        Args:
            session_id: 会话 ID（群 chat_id，纯附件与后续文字唯一稳定的共享桥）
            image_data: 已下载的图片数据（dict: {"data": base64, "media_type": ...}），可为 None
            log_contents: 已落地的日志文件内容块列表，可为空
            message_id: 飞书消息 ID

        Returns:
            True 表示已交给后续文字消息处理，本次应直接返回；
            False 表示等待超时，调用方应继续按纯附件处理
        """
        import time as time_mod

        logs = list(log_contents or [])
        arrive_ts = time_mod.time()
        # 暂存附件本体（不写上下文）：值为 dict，含图片、日志文件与到达时间戳。
        # 等待循环靠比对 ts 判断是否被后续消息接管。
        #
        # 必须**合并**而不是覆盖赋值（防回归）：飞书客户端拖入多个文件会发出
        # 多条独立的 file 消息，覆盖会让先到的附件被静默丢弃——两条消息的等待
        # 线程都会因为 ts 变化而认为"已被接管"直接返回，谁都不去处理第一个文件，
        # 而"拖多个日志文件再提问"正是日志排查的典型用法。
        with self._processing_lock:
            existing = self._pending_lone_images.get(session_id)
            if existing:
                logs = (existing.get("log_contents") or []) + logs
                image_data = image_data or existing.get("image")
            self._pending_lone_images[session_id] = {
                "image": image_data,
                "log_contents": logs,
                "ts": arrive_ts,
            }

        kinds = []
        if image_data:
            kinds.append("图片")
        if logs:
            kinds.append(f"日志文件x{len(logs)}")
        logger.info(
            f"收到纯附件消息（{'+'.join(kinds)}），已暂存，"
            f"等待 {self._lone_image_wait}s 看是否有后续文字描述: session={session_id}"
        )

        # 轮询等待：一旦有新消息进入该 session，标记会被 pop 走或被新附件覆盖
        deadline = arrive_ts + self._lone_image_wait
        while time_mod.time() < deadline:
            time_mod.sleep(0.3)
            with self._processing_lock:
                current = self._pending_lone_images.get(session_id)
            # 标记被后续消息取走(None)或被新附件更新(ts 变化)，说明已有新消息接管
            if current is None or current.get("ts") != arrive_ts:
                logger.info(f"等待期内收到后续消息，纯附件交由后续消息一并处理: session={session_id}")
                return True

        # 超时：清除标记，按纯附件继续处理（附件仍在调用方手中）
        with self._processing_lock:
            current = self._pending_lone_images.get(session_id)
            if current is not None and current.get("ts") == arrive_ts:
                del self._pending_lone_images[session_id]
        logger.info(f"等待超时，未收到文字描述，按纯附件处理（AI 将主动询问）: session={session_id}")
        return False

    @staticmethod
    def _is_mention_all(message: dict) -> bool:
        """
        判断一条飞书消息是否 @所有人

        飞书对「@所有人」在不同消息类型下的表达完全不同，只查一种必然漏判：
        - text:  content.text 里带字面量占位符 "@_all"
        - post:  文本里没有任何 @_all 痕迹，@所有人 是独立的 at 节点
                 （{"tag":"at","user_id":"all"}），且可能嵌在任意层级
        - 两者都可能在 message.mentions 里出现 key="@_all" 的条目

        因此这里三路都查，任一命中即认定为 @所有人。

        Args:
            message: 飞书事件中的 message 对象

        Returns:
            True 表示该消息 @所有人
        """
        import json as json_mod

        # 路径1：mentions 数组（最可靠，飞书对 @所有人 给出 key="@_all"）
        mentions = message.get("mentions") or []
        if isinstance(mentions, list):
            for item in mentions:
                if not isinstance(item, dict):
                    continue
                if item.get("key") == "@_all":
                    return True
                mention_id = item.get("id")
                if isinstance(mention_id, dict):
                    if str(mention_id.get("open_id", "")).lower() == "all":
                        return True

        content_str = message.get("content", "") or ""

        # 路径2：text 类型的字面量占位符
        if "@_all" in content_str:
            return True

        # 路径3：post 类型的 at 节点（递归，兼容任意嵌套深度与语言包裹）
        if message.get("message_type") == "post":
            try:
                content_data = json_mod.loads(content_str)
            except (ValueError, TypeError):
                return False

            hit = False

            def walk(node):
                nonlocal hit
                if hit:
                    return
                if isinstance(node, list):
                    for item in node:
                        walk(item)
                    return
                if not isinstance(node, dict):
                    return
                if node.get("tag") == "at":
                    user_id = str(node.get("user_id", "")).lower()
                    user_name = str(node.get("user_name", ""))
                    if user_id in ("all", "@_all") or user_name in ("所有人", "all", "All"):
                        hit = True
                        return
                for value in node.values():
                    if isinstance(value, (list, dict)):
                        walk(value)

            walk(content_data)
            return hit

        return False

    @staticmethod
    def _parse_post_content(content_data: dict) -> tuple:
        """
        解析飞书富文本（post）消息内容，提取文字、图片 key 和文件附件

        兼容多种结构差异：
        - 语言包裹：{"zh_cn": {"content": [...]}}、{"en_us": ...}，或私有化部署下无语言包裹
        - 文字标签：text / md / a（链接）
        - 图片标签：img
        - 文件标签：media / file（飞书对富文本内嵌附件两种 tag 都用过）
        - 嵌套层级：content 可能是二维数组，也可能出现更深的嵌套

        注意（防回归）：早期版本只返回 (text, image_keys)，file/media 标签完全没解析，
        导致用户"文件 + 文字说明"一起发（飞书会打包成 post 消息）时日志附件被静默丢弃，
        机器人读不到日志内容。新增标签时务必同步 feishu_bot._parse_message_content。

        Args:
            content_data: message.content 反序列化后的字典

        Returns:
            (text, image_keys, files) 三元组；files 为 [{"file_key","file_name"}]
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
            return "", [], []

        text_parts = []
        image_keys = []
        files = []

        # 文字类标签：text 用 text 字段，a（超链接）取显示文字，md 取原始 markdown
        text_tags = {"text", "md", "a"}
        # 文件类标签：飞书富文本内嵌附件在不同版本/场景下 tag 为 media 或 file
        file_tags = {"media", "file"}

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
            elif tag in file_tags:
                file_key = node.get("file_key", "")
                if file_key:
                    files.append({
                        "file_key": file_key,
                        "file_name": node.get("file_name") or "attachment",
                    })

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

        return text, image_keys, files

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
