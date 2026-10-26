"""
Webhook 服务器

接收飞书事件回调，将事件放入队列由线程池并发处理
"""
import base64
import binascii
import queue
import re
from flask import Flask, request, jsonify, send_from_directory
from loguru import logger
from typing import Any, List, Optional, Tuple
import os
from ai_assistant.core.feedback_manager import FeedbackManager

# Web 端单次请求允许携带的日志文件数上限（防止一次塞几十个文件把上下文/磁盘打满）
_MAX_WEB_LOG_FILES = 5

# 文件消息常常只有附件没有文字，给一句默认提问，避免 AI 收到空 query
_DEFAULT_LOG_FILE_QUESTION = "请分析这个日志文件中的问题。"

# 原始报文调试日志的截断长度（加密报文动辄上千字符，整段打出来不可读）
_RAW_PAYLOAD_DEBUG_CHARS = 500

# 请求体上限：base64 编码膨胀约 1.37 倍，另留余量给 JSON 结构与文字字段。
# 不设上限时 request.get_json() 会把整个 body 读进内存，5 个 20MB 文件的
# 并发请求足以把内存打满。
_MAX_CONTENT_LENGTH = int(_MAX_WEB_LOG_FILES * 20 * 1024 * 1024 * 1.5)

# 会话 ID 白名单格式：仅字母数字、下划线、短横线、点，长度 1-64。
# session_id 会作为日志文件的落地子目录名（sanitize_filename 已挡住路径穿越），
# 但前端可传任意字符串，不限制格式就能在落地目录下无限创建子目录。
_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_.\-]{1,64}$")


class WebhookServer:
    """Webhook 服务器（生产者角色，只负责接收事件并放入队列）"""

    def __init__(self, host: str = "0.0.0.0", port: int = 8080, web_frontend_enabled: bool = False):
        """
        初始化 Webhook 服务器

        Args:
            host: 监听地址
            port: 监听端口
            web_frontend_enabled: 是否启用自有前端 Web（首页 / Web 聊天 / Web 反馈）。
                默认关闭，因当前前端无鉴权。关闭时不注册这些路由，使其真正不可访问；
                飞书 webhook 与健康检查始终可用。
        """
        # 前端禁用时传 static_folder=None，连 Flask 内置的 /static/<path> 静态路由
        # 也不挂载，避免 static/ 目录下的 index.html、js/css 等被直接 URL 访问。
        if web_frontend_enabled:
            self.app = Flask(__name__)
        else:
            self.app = Flask(__name__, static_folder=None)
        # 限制请求体大小：超限时 Flask 直接返回 413，不会把整个 body 读进内存
        self.app.config["MAX_CONTENT_LENGTH"] = _MAX_CONTENT_LENGTH
        self.host = host
        self.port = port
        self.web_frontend_enabled = web_frontend_enabled
        self.feishu_adapter = None
        self.event_queue: Optional[queue.Queue] = None  # 事件队列（由外部注入）
        self.server = None  # 用于存储 waitress 服务器实例

        # 静态文件目录（src/ai_assistant/static/）
        self.static_dir = os.path.join(os.path.dirname(__file__), "static")

        # 初始化 FeedbackManager
        self.feedback_manager = FeedbackManager()

        # ===== 始终注册：飞书 webhook 与健康检查（不受前端开关影响）=====
        self.app.add_url_rule(
            "/webhook/feishu",
            "feishu_webhook",
            self.handle_feishu_webhook,
            methods=["POST"]
        )

        self.app.add_url_rule(
            "/health",
            "health_check",
            self.health_check,
            methods=["GET"]
        )

        self.app.add_url_rule(
            "/webhook/feishu/card",
            "feishu_card",
            self.handle_feishu_card,
            methods=["POST"]
        )

        # ===== 按开关注册：自有前端 Web 路由（无鉴权，默认关闭）=====
        if self.web_frontend_enabled:
            self.app.add_url_rule(
                "/",
                "index",
                self.serve_index,
                methods=["GET"]
            )

            self.app.add_url_rule(
                "/api/chat",
                "chat",
                self.handle_chat,
                methods=["POST"]
            )

            self.app.add_url_rule(
                "/api/feedback",
                "feedback",
                self.handle_feedback,
                methods=["POST"]
            )
            logger.info("自有前端 Web 已启用（首页 / Web 聊天 / Web 反馈接口）")
        else:
            logger.info("自有前端 Web 已禁用（无鉴权，默认关闭）；仅飞书 webhook 与 /health 可用")

        # 用于存储 AI Provider 和 Context Manager 的引用
        self.ai_provider = None
        self.context_manager = None
        # 日志文件管理器（Web 端上传 .log/.txt/zip 时落地并注入 AI 上下文）。
        # 默认 None：未注入时 /api/chat 只拒绝带文件的请求并给出明确提示，
        # 纯文本/图片提问不受影响，保证不接线也能正常工作。
        self.log_file_manager = None

    def serve_index(self):
        """返回首页"""
        try:
            return send_from_directory(self.static_dir, "index.html")
        except Exception as e:
            logger.error(f"Error serving index.html: {e}")
            return jsonify({"error": "index.html not found"}), 404

    def set_feishu_adapter(self, adapter):
        """设置飞书适配器"""
        self.feishu_adapter = adapter

    def set_event_queue(self, event_queue: queue.Queue):
        """
        设置事件队列

        Args:
            event_queue: 事件队列，webhook 收到事件后放入队列
        """
        self.event_queue = event_queue

    def set_ai_components(self, ai_provider, context_manager, log_file_manager=None):
        """
        设置 AI 组件（用于 Web 聊天接口）

        Args:
            ai_provider: AI Provider 实例
            context_manager: Context Manager 实例
            log_file_manager: 日志文件管理器实例（可选，缺省时 Web 端不支持上传日志文件）
        """
        self.ai_provider = ai_provider
        self.context_manager = context_manager
        if log_file_manager is not None:
            self.log_file_manager = log_file_manager

    def set_log_file_manager(self, log_file_manager):
        """
        设置日志文件管理器（用于 Web 端上传日志文件）

        与 set_ai_components 的可选参数等价，单独提供 setter 便于在不改动
        既有调用的前提下接线。未调用时 Web 端收到 files 字段会返回明确错误，
        而不是静默丢弃附件。

        Args:
            log_file_manager: LogFileManager 实例
        """
        self.log_file_manager = log_file_manager

    def handle_feishu_webhook(self):
        """
        处理飞书 webhook 回调：立即返回 200，将原始事件放入队列异步处理

        飞书要求 3 秒内返回 HTTP 200，否则会认为推送失败。
        因此这里只做最基本的验证，立即返回 200，所有业务逻辑在后台处理。
        """
        from ai_assistant.core.trace_context import with_new_trace_id

        trace_id = with_new_trace_id()

        try:
            data = request.get_json()

            if not data:
                logger.warning("Empty webhook data received")
                return jsonify({"error": "Empty data"}), 400

            # 接收日志只打"是否加密 + 密文长度"：密文正文上千字符且不可读，
            # 整段 dump 会把日志文件刷爆。真实报文需要排查时看 debug 级截断日志。
            encrypted_text = data.get("encrypt") if isinstance(data, dict) else None
            if isinstance(encrypted_text, str):
                logger.info(f"📨 Webhook received: encrypted=yes, cipher_len={len(encrypted_text)}")
            else:
                logger.info("📨 Webhook received: encrypted=no")
            logger.debug(f"📨 Raw payload(截断): {str(data)[:_RAW_PAYLOAD_DEBUG_CHARS]}")

            # 如果是加密数据，先解密以判断事件类型（URL 验证必须同步返回，不能走异步队列）
            decrypted_data = data
            if "encrypt" in data and self.feishu_adapter and self.feishu_adapter.encrypt_key:
                try:
                    decrypted_data = self.feishu_adapter._decrypt(data["encrypt"])
                except Exception as e:
                    logger.error(f"Failed to decrypt webhook for type check: {e}")
                    # 解密失败也要返回 200 避免飞书重试
                    return jsonify({}), 200

            # 解密后（或本就明文）的报文才是信息源，打一条可读摘要
            logger.info(f"📨 {self._summarize_event(decrypted_data)}")

            # URL 验证请求需要立即返回 challenge（不放入队列）
            if decrypted_data.get("type") == "url_verification":
                challenge = decrypted_data.get("challenge", "")
                logger.info(f"✅ URL verification: returning challenge={challenge}")
                return jsonify({"challenge": challenge}), 200

            # 卡片交互事件需要同步处理并返回响应体（不能异步）
            event_type = decrypted_data.get("header", {}).get("event_type", "")
            if event_type == "card.action.trigger":
                logger.info(f"📝 Card action trigger, processing synchronously")
                if self.feishu_adapter:
                    response_body = self.feishu_adapter.process_card_action(decrypted_data) or {}
                    logger.info(f"Card action response: {response_body}")
                    return jsonify(response_body), 200
                else:
                    logger.error("Feishu adapter not set")
                    return jsonify({}), 200

            # 将原始事件数据放入队列，由后台线程异步处理
            if self.event_queue:
                try:
                    self.event_queue.put_nowait({
                        "trace_id": trace_id,
                        "adapter": self.feishu_adapter,
                        "raw_data": data  # 存储原始数据，不预处理
                    })
                    logger.info(f"✅ Event enqueued, queue size: {self.event_queue.qsize()}/{self.event_queue.maxsize}")
                except queue.Full:
                    logger.error(f"❌ Event queue full (size={self.event_queue.maxsize}), dropping event")
                    # 即使队列满了，也要返回 200，避免飞书重试

            # 立即返回 200，确保在 3 秒内响应
            return jsonify({}), 200

        except Exception as e:
            logger.error(f"Error handling webhook: {e}")
            # 即使出错也返回 200，避免飞书重试
            return jsonify({}), 200

    @staticmethod
    def _detect_attachment(message_type: str, content_str: Any) -> str:
        """
        从消息体判断是否带附件（图片/文件），用于日志摘要。

        只做字符串包含判断，不解析 JSON：content 为字符串化 JSON，解析失败
        在日志路径上没有任何收益，还会引入异常风险。

        Args:
            message_type: 飞书消息类型 text / image / file / post
            content_str: 消息 content 字段（字符串化 JSON）

        Returns:
            附件描述（"image" / "file" / "image+file" / "no"）
        """
        kinds = []
        text = content_str if isinstance(content_str, str) else ""
        if message_type == "image" or "image_key" in text:
            kinds.append("image")
        if message_type == "file" or "file_key" in text:
            kinds.append("file")
        return "+".join(kinds) if kinds else "no"

    def _summarize_event(self, event_data: Any) -> str:
        """
        把解密后的飞书报文压成一行可读摘要。

        字段全部 .get 容错：webhook 必须在 3 秒内返回 200，日志摘要绝不能
        因为某个字段缺失抛异常影响主流程。同时兼容 v2.0（header/event）与
        v1.0（type=event_callback）两种报文格式。

        Args:
            event_data: 解密后的报文 dict

        Returns:
            单行摘要字符串
        """
        try:
            if not isinstance(event_data, dict):
                return f"Event summary unavailable: payload type={type(event_data).__name__}"

            # URL 验证是唯一会带顶层 type 的常规报文，单独一行即可
            payload_type = event_data.get("type")
            if payload_type == "url_verification":
                return "Event: url_verification"

            queue_part = ""
            if self.event_queue is not None:
                queue_part = f", queue={self.event_queue.qsize()}/{self.event_queue.maxsize}"

            if payload_type == "event_callback":
                # v1.0 旧版格式：字段都在 event 顶层，命名也不同
                event = event_data.get("event") or {}
                msg_type = event.get("msg_type", "") or "-"
                attach = "image" if event.get("image_key") else "no"
                return (
                    f"Event(v1.0): msg_type={msg_type}, "
                    f"chat_type={event.get('chat_type', '') or '-'}, "
                    f"chat={event.get('open_chat_id', '') or '-'}, "
                    f"sender={event.get('open_id', '') or '-'}, "
                    f"message_id={event.get('open_message_id', '') or '-'}, "
                    f"attach={attach}{queue_part}"
                )

            header = event_data.get("header") or {}
            event = event_data.get("event") or {}
            event_type = header.get("event_type", "") or "-"
            message = event.get("message") or {}
            message_type = message.get("message_type", "") or "-"
            sender_id = (event.get("sender") or {}).get("sender_id") or {}
            attach = self._detect_attachment(message_type, message.get("content"))

            return (
                f"Event: event_type={event_type}, "
                f"chat_type={message.get('chat_type', '') or '-'}, "
                f"msg_type={message_type}, "
                f"chat={message.get('chat_id', '') or '-'}, "
                f"sender={sender_id.get('open_id', '') or '-'}, "
                f"message_id={message.get('message_id', '') or '-'}, "
                f"attach={attach}{queue_part}"
            )
        except Exception as e:
            # 摘要失败绝不能影响 webhook 返回 200
            return f"Event summary failed: {e}"

    def health_check(self):
        """健康检查"""
        status = {
            "status": "ok",
            "queue_size": self.event_queue.qsize() if self.event_queue else 0
        }
        return jsonify(status), 200

    def _decode_base64_payload(self, raw: Any) -> Optional[bytes]:
        """
        解码前端传来的 base64 文件内容（兼容 data URL 前缀）。

        Args:
            raw: base64 字符串，可能带 "data:...;base64," 前缀

        Returns:
            解码后的字节；输入非法或解码失败时返回 None
        """
        if not isinstance(raw, str) or not raw:
            return None
        payload = raw
        if payload.startswith("data:"):
            # data URL 形式：data:text/plain;base64,xxxx
            _, _, payload = payload.partition(",")
        try:
            return base64.b64decode(payload, validate=False)
        except (binascii.Error, ValueError):
            return None

    def _collect_web_log_files(
        self,
        session_id: str,
        files: Any,
    ) -> Tuple[List[Any], Optional[str]]:
        """
        把 Web 端上传的日志文件落地，产出 log_file 类型的 Content 块。

        产出结构与飞书侧 `FeishuBotAdapter._collect_log_files` 完全一致：
        小文本日志带正文，zip 解压后只给路径与文件清单，由 AI 用代码排查工具
        按需读取。落地失败/超限时写 note 告知 AI，不静默丢弃。

        Args:
            session_id: 会话 ID（作为落地子目录名，LogFileManager 内部会清洗）
            files: 请求体中的 files 字段，期望 [{"name","data"}]

        Returns:
            (log_file Content 列表, 错误信息)；错误信息非空时调用方应返回 400
        """
        from ai_assistant.core.models import Content
        from ai_assistant.utils.log_files import sanitize_filename

        if not files:
            return [], None

        if not isinstance(files, list):
            return [], "files 字段格式错误：应为数组"

        manager = self.log_file_manager
        if not manager or not getattr(manager, "enabled", False):
            return [], "服务端未启用日志文件处理，无法接收附件"

        if len(files) > _MAX_WEB_LOG_FILES:
            return [], f"一次最多上传 {_MAX_WEB_LOG_FILES} 个文件，当前 {len(files)} 个"

        max_size_bytes = getattr(manager, "max_size_bytes", 20 * 1024 * 1024)
        contents: List[Any] = []

        # 两阶段处理：先把所有文件校验完，再统一落地。
        # 不能边校验边落地——第 2 个文件非法时整个请求返回 400，但第 1 个已经写进
        # 磁盘且不会回滚，只能等每日定时清理，等于给了一条未授权写盘的路径。
        validated: List[tuple] = []
        for item in files:
            if not isinstance(item, dict):
                return [], "files 元素格式错误：应为 {name, data} 对象"

            raw_name = item.get("name") or "attachment"
            # 日志与回显一律用清洗后的名字，不直接拼原始名
            safe_name = sanitize_filename(raw_name, fallback="attachment.log")

            if not manager.is_allowed(safe_name):
                return [], f"不支持的文件类型：{safe_name}（仅支持 .log/.txt/.out/.err/.zip）"

            data = self._decode_base64_payload(item.get("data"))
            if data is None:
                return [], f"文件内容 base64 解码失败：{safe_name}"
            if not data:
                return [], f"文件内容为空：{safe_name}"

            # 后端独立判定大小，不依赖前端校验
            if len(data) > max_size_bytes:
                return [], (
                    f"文件超过大小上限：{safe_name}（{len(data) / 1024 / 1024:.1f}MB，"
                    f"上限 {max_size_bytes / 1024 / 1024:.0f}MB）"
                )

            validated.append((safe_name, data))

        for safe_name, data in validated:
            logger.info(f"Web 上传日志文件: name={safe_name}, size={len(data) / 1024:.1f}KB")

            path = manager.save_bytes(session_id, safe_name, data)
            if not path:
                # 落地失败：告知 AI 这一情况，不静默丢弃
                contents.append(Content(type="log_file", data={
                    "name": safe_name,
                    "path": "",
                    "text": "",
                    "note": "文件超过大小上限或保存失败，未能读取内容",
                }))
                continue

            if path.lower().endswith(".zip"):
                out_dir, extracted = manager.extract_zip(path)
                listing = "\n".join(f"- {p}" for p in extracted[:50])
                contents.append(Content(type="log_file", data={
                    "name": safe_name,
                    "path": out_dir or path,
                    "text": "",
                    "note": (
                        f"这是一个日志压缩包，已解压到本地目录 {out_dir or path}。"
                        f"其中文本日志 {len(extracted)} 个：\n{listing}"
                        if extracted else
                        f"这是一个压缩包，已解压到 {out_dir or path}，未发现文本日志文件。"
                    ),
                }))
            else:
                contents.append(Content(type="log_file", data={
                    "name": safe_name,
                    "path": path,
                    "text": manager.read_text_head(path),
                    "note": "",
                }))

        return contents, None

    def handle_chat(self):
        """
        处理 Web 聊天请求

        请求格式：
        {
            "message": "用户消息",
            "session_id": "会话ID（可选）",
            "image": {              // 可选
                "data": "base64...",
                "media_type": "image/png"
            },
            "files": [              // 可选，日志文件附件（最多 5 个）
                {"name": "app.log", "data": "base64..."}
            ]
        }

        files 仅接受 .log/.txt/.out/.err/.zip，单文件大小上限由 LogFileManager
        配置决定（默认 20MB）。带日志文件的提问会自动进入代码排查（Agentic）模式。

        响应格式：
        {
            "reply": "AI 回复",
            "session_id": "会话ID",
            "record_id": "对话历史记录主键（用于关联反馈）"
        }
        """
        from ai_assistant.core.models import Message, Content
        from datetime import datetime

        try:
            data = request.get_json()
            if not data:
                return jsonify({"error": "Empty request"}), 400

            user_text = data.get("message", "")
            session_id = data.get("session_id", "web-default")
            image_data = data.get("image")  # {"data": base64, "media_type": "image/png"}
            files = data.get("files")  # [{"name": "app.log", "data": base64}]

            # session_id 会作为日志文件落地的子目录名，限制格式避免在落地目录下
            # 被任意构造的字符串刷出大量子目录（路径穿越本身已由 sanitize_filename 挡住）
            if not isinstance(session_id, str) or not _SESSION_ID_RE.match(session_id):
                return jsonify({"error": "Invalid session_id"}), 400

            if not user_text and not image_data and not files:
                return jsonify({"error": "Missing message, image or files"}), 400

            if not self.ai_provider or not self.context_manager:
                return jsonify({"error": "AI components not initialized"}), 500

            # 日志文件落地（校验失败直接 400，给出可读原因，不抛 500）
            log_contents, file_error = self._collect_web_log_files(session_id, files)
            if file_error:
                logger.warning(f"Web 上传文件校验失败: session={session_id}, reason={file_error}")
                return jsonify({"error": file_error}), 400

            # 文件消息常无文字，补一句默认提问，避免 AI 收到空 query
            if not user_text and log_contents:
                user_text = _DEFAULT_LOG_FILE_QUESTION

            # 构建用户消息内容（顺序与飞书侧一致：image → log_file → text）
            contents = []
            if image_data:
                contents.append(Content(type="image", data=image_data))
            contents.extend(log_contents)
            if user_text:
                contents.append(Content(type="text", data=user_text))

            user_message = Message(
                role="user",
                content=contents,
                timestamp=datetime.now()
            )

            # 添加到上下文
            self.context_manager.add_message(session_id, user_message)

            # 获取上下文消息
            context_messages = self.context_manager.get_context(session_id)

            # 调用 AI 生成回复（返回回复文本 + chat_history 记录主键）
            reply, record_id, metadata = self.ai_provider.call(context_messages, session_id=session_id, source="web")

            # 将 AI 回复添加到上下文
            # metadata 必须带上：_inherited_chain_mode() 靠 assistant 消息的
            # metadata.mode 判断本轮该不该继承上一轮的模式（排查/运维）。
            # 漏了这个字段，Web 端的模式继承会永久失效——用户上传日志进了排查模式后，
            # 下一句追问又退回普通 RAG（飞书侧 main.py:686 一直是带的，此处曾遗漏）。
            ai_message = Message(
                role="assistant",
                content=[Content(type="text", data=reply)],
                timestamp=datetime.now(),
                metadata=metadata
            )
            self.context_manager.add_message(session_id, ai_message)

            return jsonify({
                "reply": reply,
                "session_id": session_id,
                "record_id": record_id
            }), 200

        except Exception as e:
            logger.error(f"Error handling chat request: {e}")
            return jsonify({"error": str(e)}), 500

    def handle_feishu_card(self):
        """
        处理飞书卡片交互事件（按钮点击）

        处理 card.action.trigger 事件，提取反馈信息并调用 feishu_bot 的处理方法
        """
        try:
            data = request.get_json()
            if not data:
                logger.warning("Empty card action data received")
                return jsonify({}), 200

            # 同样不 dump 密文正文（上千字符不可读），只记录是否加密与长度
            card_cipher = data.get("encrypt") if isinstance(data, dict) else None
            if isinstance(card_cipher, str):
                logger.info(f"📨 Card action received: encrypted=yes, cipher_len={len(card_cipher)}")
            else:
                logger.info("📨 Card action received: encrypted=no")
            logger.debug(f"📨 Card raw payload(截断): {str(data)[:_RAW_PAYLOAD_DEBUG_CHARS]}")

            # 如果配置了加密，先解密
            if self.feishu_adapter and self.feishu_adapter.encrypt_key and "encrypt" in data:
                try:
                    data = self.feishu_adapter._decrypt(data["encrypt"])
                    logger.info("Card action decrypted successfully")
                except Exception as e:
                    logger.error(f"Failed to decrypt card action: {e}")
                    return jsonify({}), 200

            # 验证事件类型
            event_type = data.get("header", {}).get("event_type", "")
            if event_type != "card.action.trigger":
                logger.warning(f"Unexpected event type: {event_type}")
                return jsonify({}), 200

            # 调用 feishu_bot 处理卡片交互，拿到响应体（toast 或表单卡片）
            if self.feishu_adapter:
                response_body = self.feishu_adapter.process_card_action(data) or {}
            else:
                logger.error("Feishu adapter not set")
                response_body = {}

            # 返回响应体给飞书（toast 提示 / 表单卡片 / 空 dict）
            return jsonify(response_body), 200

        except Exception as e:
            logger.error(f"Error handling card action: {e}", exc_info=True)
            return jsonify({}), 200

    def handle_feedback(self):
        """
        处理 Web 端反馈提交

        请求格式：
        {
            "session_id": "xxx",
            "record_id": "chat_history 记录主键",
            "feedback_type": "like|dislike",
            "feedback_text": "用户反馈文字（可选）"
        }

        响应格式：
        {
            "success": true,
            "feedback_id": "uuid"
        }
        """
        try:
            data = request.get_json()
            if not data:
                return jsonify({"error": "Empty request"}), 400

            # 提取必填字段
            session_id = data.get("session_id")
            record_id = data.get("record_id")
            feedback_type = data.get("feedback_type")

            # 验证必填字段
            required_fields = {
                "session_id": session_id,
                "record_id": record_id,
                "feedback_type": feedback_type,
            }

            for field_name, field_value in required_fields.items():
                if not field_value:
                    return jsonify({"error": f"Missing required field: {field_name}"}), 400

            # 验证 feedback_type 枚举值
            if feedback_type not in ["like", "dislike"]:
                return jsonify({"error": f"Invalid feedback_type: {feedback_type}, must be 'like' or 'dislike'"}), 400

            # 提取可选字段
            feedback_text = data.get("feedback_text")

            # 调用 FeedbackManager 保存反馈
            feedback_id = self.feedback_manager.save_feedback(
                record_id=record_id,
                session_id=session_id,
                source="web",
                feedback_type=feedback_type,
                feedback_text=feedback_text if feedback_text else None
            )

            logger.info(f"Web 反馈提交: session={session_id}, type={feedback_type}, feedback_id={feedback_id}")

            return jsonify({
                "success": True,
                "feedback_id": feedback_id
            }), 200

        except ValueError as e:
            # FeedbackManager 参数验证失败
            logger.warning(f"反馈参数验证失败: {e}")
            return jsonify({"error": str(e)}), 400
        except Exception as e:
            # 其他异常
            logger.error(f"处理反馈请求失败: {e}", exc_info=True)
            return jsonify({"error": "Internal server error"}), 500

    def run(self, debug: bool = False):
        """启动服务器（开发模式，使用 Flask 内置服务器）"""
        logger.info(f"Starting webhook server on {self.host}:{self.port} (development mode)")
        self.app.run(host=self.host, port=self.port, debug=debug)

    def run_production(self):
        """启动服务器（生产模式，使用 waitress）"""
        try:
            from waitress import serve
            logger.info(f"Starting webhook server on {self.host}:{self.port} (production mode with waitress)")
            # waitress 是阻塞调用，会一直运行直到被停止
            serve(self.app, host=self.host, port=self.port, threads=4)
        except ImportError:
            logger.warning("waitress not installed, falling back to Flask development server")
            logger.warning("Install waitress with: pip install waitress")
            self.run(debug=False)
        except Exception as e:
            logger.error(f"Error starting production server: {e}", exc_info=True)

    def shutdown(self):
        """停止服务器"""
        # waitress 没有提供优雅停止的 API，只能通过线程退出来停止
        # 这里主要是为了日志记录
        logger.info("Webhook server shutdown requested")
